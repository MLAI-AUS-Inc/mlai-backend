"""Send a blast's SMS and WhatsApp deliveries through Twilio, and track status.

The blast dispatcher creates ``queued`` delivery rows. ``deliver_twilio_deliveries``
claims them one at a time, marks each ``submitting`` and commits before
calling Twilio, then stores the SID or the failure. Nothing is retried: a
timeout leaves the row ``unknown`` for a person to decide.

No acting account reaches this module: the dispatcher (which re-checks the
blast's creator first) and the signature-verified Twilio webhooks call it, so
it has no MLAI admin gate of its own.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import merge_tags, twilio
from .models import Blast, BlastDelivery, Channel, Contact
from .phones import sms_allowed
from .sms import compose_sms, cost_setting, sms_segments

logger = logging.getLogger(__name__)

STATUS_CALLBACK_PATH = "/api/v1/crm/webhooks/twilio/status/"
TWILIO_CHANNELS = (Channel.SMS, Channel.WHATSAPP)
DAILY_WINDOW = timedelta(hours=24)
# Rows are claimed one at a time, so a row still ``submitting`` this long
# after its claim belongs to a dispatcher that stopped mid-call.
INTERRUPTED_AFTER = timedelta(minutes=15)

Status = BlastDelivery.Status
# Rows that may have reached Twilio, so they count towards the daily cap.
_ATTEMPTED = (
    Status.SUBMITTING,
    Status.SENT,
    Status.DELIVERED,
    Status.READ,
    Status.UNDELIVERED,
    Status.UNKNOWN,
)
_RANK = {
    Status.QUEUED: 0,
    Status.SUBMITTING: 1,
    Status.UNKNOWN: 1,
    Status.SENT: 2,
    Status.DELIVERED: 3,
    Status.READ: 4,
}
_FAILURES = (Status.FAILED, Status.UNDELIVERED)
# Twilio's accepted / queued / sending states come before our ``sent`` and
# are ignored.
TWILIO_STATUSES = {
    "sent": Status.SENT,
    "delivered": Status.DELIVERED,
    "read": Status.READ,
    "failed": Status.FAILED,
    "undelivered": Status.UNDELIVERED,
    "canceled": Status.FAILED,
}


class DeliveryRefused(Exception):
    """The delivery can't be sent; ``status`` and ``code`` are stored on it."""

    def __init__(self, code: str, status: str = Status.FAILED):
        self.code, self.status = code, status
        super().__init__(code)


def status_callback_url() -> str:
    base_url = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").rstrip("/")
    return f"{base_url}{STATUS_CALLBACK_PATH}"


def channel_enabled(channel: str) -> bool:
    if channel == Channel.SMS:
        return bool(getattr(settings, "CRM_SMS_ENABLED", False))
    if channel == Channel.WHATSAPP:
        return bool(getattr(settings, "CRM_WHATSAPP_ENABLED", False))
    return False


def channel_block(channel: str) -> Optional[str]:
    """Why Twilio sends on this channel can't go out now, or ``None``."""

    if not channel_enabled(channel):
        return "channel_disabled"
    configured = twilio.sms_configured() if channel == Channel.SMS else twilio.whatsapp_configured()
    return None if configured else "not_configured"


def daily_limit(channel: str) -> int:
    name = "CRM_SMS_DAILY_LIMIT" if channel == Channel.SMS else "CRM_WHATSAPP_DAILY_LIMIT"
    return max(0, int(getattr(settings, name, 500)))


def remaining_today(channel: str, now=None) -> int:
    """Sends left on this channel in the rolling 24 hours before ``now``.

    Counts every row that may have reached Twilio since it was queued: a 4xx
    refusal without a SID or an opt-out found at send time does not count.
    """

    now = now or timezone.now()
    used = (
        BlastDelivery.objects.filter(channel=channel, created_at__gte=now - DAILY_WINDOW)
        .filter(Q(status__in=_ATTEMPTED) | (Q(status=Status.FAILED) & ~Q(provider_id="")))
        .count()
    )
    return max(0, daily_limit(channel) - used)


def estimate_whatsapp_cost(count) -> Decimal:
    """Estimated AUD for ``count`` WhatsApp template messages."""

    rate = cost_setting("CRM_WHATSAPP_COST_PER_MESSAGE_AUD")
    return (rate * max(0, int(count or 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def deliver_twilio_deliveries(blast: Blast, *, limit: int, now=None) -> dict[str, Any]:
    """Send up to ``limit`` of this blast's queued SMS and WhatsApp deliveries.

    Call it outside any transaction, so each claim commits before its HTTP
    call. Rows for a disabled or unconfigured channel, or beyond the daily
    cap, stay ``queued``. Returns counts per channel.
    """

    now = now or timezone.now()
    blast = Blast.objects.select_related("event", "whatsapp_template").get(pk=blast.pk)
    _expire_interrupted(blast, now)
    budget = max(0, int(limit or 0))
    summary: dict[str, Any] = {}
    for channel in TWILIO_CHANNELS:
        queued = BlastDelivery.objects.filter(blast=blast, channel=channel, status=Status.QUEUED)
        if not queued.exists():
            continue
        counts: Counter = Counter()
        block = "cancelled" if blast.status == Blast.Status.CANCELLED else channel_block(channel)
        allowance = 0 if block else min(budget, remaining_today(channel, now))
        while allowance > 0:
            delivery = _claim_next(blast, channel)
            if delivery is None:
                break
            allowance -= 1
            budget -= 1
            counts[_deliver(blast, delivery)] += 1
        result: dict[str, Any] = dict(counts)
        if block:
            result["blocked"] = block
        elif queued.exists() and remaining_today(channel, now) == 0:
            result["blocked"] = "daily_limit"
        summary[str(channel)] = result
    return summary


def _expire_interrupted(blast: Blast, now) -> int:
    return BlastDelivery.objects.filter(
        blast=blast,
        channel__in=TWILIO_CHANNELS,
        status=Status.SUBMITTING,
        updated_at__lt=now - INTERRUPTED_AFTER,
    ).update(status=Status.UNKNOWN, error_code="interrupted", updated_at=timezone.now())


def _claim_next(blast: Blast, channel: str) -> Optional[BlastDelivery]:
    with transaction.atomic():
        delivery = (
            BlastDelivery.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("contact")
            .filter(blast=blast, channel=channel, status=Status.QUEUED)
            # A cancel takes effect between two sends, not only between ticks.
            .exclude(blast__status=Blast.Status.CANCELLED)
            .order_by("created_at", "id")
            .first()
        )
        if delivery is None:
            return None
        delivery.status = Status.SUBMITTING
        delivery.save(update_fields=["status", "updated_at"])
    return delivery


def _deliver(blast: Blast, delivery: BlastDelivery) -> str:
    segments = None
    try:
        _check_still_reachable(delivery)
        if delivery.channel == Channel.SMS:
            body = compose_sms(merge_tags.render(blast.sms_body, delivery.contact, blast.event))
            _, segments = sms_segments(body)
            result = twilio.send_sms(delivery.address, body, status_callback_url())
        else:
            content_sid, variables = whatsapp_message(blast, delivery.contact)
            result = twilio.send_whatsapp_template(
                delivery.address, content_sid, variables, status_callback_url()
            )
    except DeliveryRefused as exc:
        result = twilio.SendResult(exc.status, error_code=exc.code)
    except merge_tags.UnsupportedMergeTag as exc:
        result = twilio.SendResult(twilio.FAILED, error_code=exc.code)
    except Exception as exc:
        # The call may have reached Twilio, so this row must not be retried.
        logger.warning(
            "crm delivery %s ended unknown after %s", delivery.pk, exc.__class__.__name__
        )
        result = twilio.SendResult(twilio.UNKNOWN, error_code="internal_error")
    outcome = str(result.outcome)
    BlastDelivery.objects.filter(
        pk=delivery.pk, status__in=(Status.SUBMITTING, Status.UNKNOWN)
    ).update(
        status=outcome,
        provider_id=result.sid,
        error_code=result.error_code[:32],
        segments=segments,
        updated_at=timezone.now(),
    )
    return outcome


def _check_still_reachable(delivery: BlastDelivery) -> None:
    """Re-read consent just before the call, so a STOP since queueing wins."""

    state = (
        Contact.objects.filter(pk=delivery.contact_id)
        .values("sms_status", "whatsapp_opt_in")
        .first()
    )
    if state is None:
        raise DeliveryRefused("contact_missing")
    if delivery.channel == Channel.SMS:
        if state["sms_status"] == Contact.SmsStatus.OPTED_OUT:
            raise DeliveryRefused("opted_out", Status.UNSUBSCRIBED)
        if state["sms_status"] != Contact.SmsStatus.ACTIVE:
            raise DeliveryRefused("not_textable")
        if not sms_allowed(delivery.address, getattr(settings, "CRM_SMS_ALLOWED_PREFIXES", ["+614"])):
            raise DeliveryRefused("not_allowed_country")
    elif state["whatsapp_opt_in"] != Contact.WhatsAppOptIn.GRANTED:
        raise DeliveryRefused("not_opted_in", Status.UNSUBSCRIBED)


def whatsapp_message(blast: Blast, contact) -> tuple[str, dict[str, str]]:
    """The template SID and ``{"1": …}`` values for one person.

    Each template slot is mapped to a merge-tag source or to the blast's
    custom text for that slot. WhatsApp rejects empty values and line
    breaks, so values are collapsed to one line and an empty one refuses.
    """

    template = blast.whatsapp_template
    if template is None or not template.approved or not template.enabled:
        raise DeliveryRefused("template_unavailable")
    custom = blast.whatsapp_variables if isinstance(blast.whatsapp_variables, dict) else {}
    values: dict[str, str] = {}
    for slot in template.variables or []:
        if not isinstance(slot, dict):
            continue
        index = str(slot.get("index") or "").strip()
        if not index:
            continue
        source = str(slot.get("source") or "custom")
        if source == "custom":
            value = custom.get(index, "")
        else:
            value = merge_tags.value_for(source, contact, blast.event)
        value = " ".join(str(value or "").split())
        if not value:
            raise DeliveryRefused("missing_template_variable")
        values[index] = value
    return template.content_sid, values


def next_status(current: str, incoming: Optional[str]) -> Optional[str]:
    """The status a Twilio callback moves a delivery to, or ``None``.

    queued < submitting < sent < delivered < read. ``failed`` and
    ``undelivered`` are terminal and override ``sent`` but never
    ``delivered`` or ``read``. Nothing moves backwards.
    """

    if incoming is None or current not in _RANK:
        return None
    if incoming in _FAILURES:
        return incoming if _RANK[current] <= _RANK[Status.SENT] else None
    return incoming if _RANK.get(incoming, -1) > _RANK[current] else None


def record_status_callback(payload: dict[str, Any], *, now=None) -> dict[str, str]:
    """Apply one Twilio StatusCallback; unknown SIDs and replays are no-ops."""

    sid = str(payload.get("MessageSid") or payload.get("SmsSid") or "").strip()
    if not sid:
        return {"status": "ignored", "reason": "missing_sid"}
    raw_status = str(payload.get("MessageStatus") or payload.get("SmsStatus") or "").strip().lower()
    with transaction.atomic():
        delivery = BlastDelivery.objects.select_for_update().filter(provider_id=sid).first()
        if delivery is None:
            return {"status": "ignored", "reason": "unknown_sid"}
        fields = []
        new_status = next_status(delivery.status, TWILIO_STATUSES.get(raw_status))
        if new_status:
            delivery.status = new_status
            fields.append("status")
            if new_status in _FAILURES:
                delivery.error_code = str(payload.get("ErrorCode") or "").strip()[:32]
                fields.append("error_code")
            if new_status in (Status.DELIVERED, Status.READ) and delivery.delivered_at is None:
                delivery.delivered_at = now or timezone.now()
                fields.append("delivered_at")
        price = _price(payload.get("Price"))
        if price is not None and delivery.price is None:
            delivery.price = price
            delivery.price_unit = str(payload.get("PriceUnit") or "").strip().upper()[:3]
            fields += ["price", "price_unit"]
        if fields:
            delivery.save(update_fields=fields + ["updated_at"])
    return {"status": "updated" if new_status else "unchanged", "delivery_status": delivery.status}


def _price(value) -> Optional[Decimal]:
    """Twilio reports prices as negative strings, e.g. ``-0.05150``."""

    if value in (None, ""):
        return None
    try:
        price = abs(Decimal(str(value)))
    except InvalidOperation:
        return None
    return price.quantize(Decimal("0.00001")) if price.is_finite() and price < 10**5 else None
