"""Blasts: audiences, queueing, dispatch, test sends and cancel.

A blast stays a draft until ``queue_blast`` checks it, recounts its audience
against the counts the sender confirmed, snapshots one ``BlastDelivery`` per
person per channel and queues it. The ``crm_blast_dispatch`` runner then sends
email through listmonk (``crm/blast_email.py``) and SMS and WhatsApp through
Twilio (``crm/messaging.py``), and rolls the blast's status up from its rows.

Channel rules: email follows the segment's suppression and purpose rules; SMS
and WhatsApp are for event blasts only, to people registered for the event.
With ``sms_only_without_whatsapp`` anyone WhatsApp reaches gets no SMS.

Queueing, test sends and cancelling refuse an actor who isn't an MLAI admin,
and dispatch fails a live blast whose creator has since lost admin. The
dispatcher itself has no actor and isn't gated.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import reduce
from operator import or_
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from roo.permissions import is_mlai_admin_user

from . import blast_email, listmonk, merge_tags, twilio
from .access import channel_availability
from .emails import normalize_contact_email
from .listmonk import ListmonkError
from .messaging import (
    DeliveryRefused,
    channel_block,
    deliver_twilio_deliveries,
    estimate_whatsapp_cost,
    remaining_today,
    whatsapp_message,
)
from .models import Blast, BlastDelivery, Channel, Contact, CrmAuditEvent, EventRegistration
from .phones import normalize_contact_phone, sms_allowed
from .queries import (
    banned,
    event_has_ended,
    mailbox_remaining_today,
    registered_q,
    sms_allowed_prefixes,
)
from .segments import definition_q, evaluate, quick_segment_definition, validate_definition
from .sms import GSM7, UCS2, compose_sms, estimate_cost, sms_segments

logger = logging.getLogger(__name__)

CHANNELS = (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP)
TWILIO_CHANNELS = (Channel.SMS, Channel.WHATSAPP)
QUICK_SEGMENTS = ("going", "checked_in", "no_show", "waitlist")
EXCLUDED = {
    Channel.EMAIL: ("unsubscribed", "bounced", "banned", "no_consent", "not_registered"),
    Channel.SMS: ("banned", "opted_out", "no_mobile", "not_allowed_country", "covered_by_whatsapp"),
    Channel.WHATSAPP: ("banned", "no_phone", "not_opted_in"),
}
DISPATCH_THROTTLE_KEY = "crm:blast-dispatch:throttle"
MAX_BLASTS_PER_TICK = 10
LIVE = (Blast.Status.QUEUED, Blast.Status.SENDING)
_NEVER = Q(pk__isnull=True)
# Rows that reached (or may have reached) the provider.
_REACHED = (
    BlastDelivery.Status.SENT,
    BlastDelivery.Status.DELIVERED,
    BlastDelivery.Status.READ,
    BlastDelivery.Status.UNDELIVERED,
    BlastDelivery.Status.BOUNCED,
    BlastDelivery.Status.UNKNOWN,
)
IN_FLIGHT, DONE, FAILED, BLOCKED = "in_flight", "done", "failed", "blocked"
NOT_AUTHORISED = "not_authorised"
_LISTMONK_OUTAGES = ("listmonk_timeout", "listmonk_connection_error", "listmonk_unreadable_response") + tuple(
    f"listmonk_http_{code}" for code in (429, 500, 502, 503, 504)
)


class BlastError(ValueError):
    """A blast request that can't go ahead: a stable code, its HTTP status and safe extras."""

    def __init__(self, code: str, status: int = 400, **extra):
        self.code, self.status, self.extra = code, status, extra
        super().__init__(code)


# Pure pieces


def channel_state(counts: dict, *, blocked: bool = False, provider_failed: bool = False) -> str:
    """``in_flight``, ``blocked``, ``done`` or ``failed`` for one channel's row counts by status."""

    if provider_failed:
        return FAILED
    if counts.get(BlastDelivery.Status.QUEUED, 0) + counts.get(BlastDelivery.Status.SUBMITTING, 0):
        return BLOCKED if blocked else IN_FLIGHT
    reached = sum(counts.get(status, 0) for status in _REACHED)
    # Rows dropped at send time (opted out) are neither sent nor a failure.
    if reached or not counts.get(BlastDelivery.Status.FAILED, 0):
        return DONE
    return FAILED


def rollup_status(states: Iterable[str]) -> str:
    """``sending`` while any channel is in flight; then ``sent``, ``partially_sent`` or ``failed``."""

    states = list(states)
    if IN_FLIGHT in states:
        return Blast.Status.SENDING
    if states and all(state == DONE for state in states):
        return Blast.Status.SENT
    if DONE in states:
        return Blast.Status.PARTIALLY_SENT
    return Blast.Status.FAILED


def money(amount) -> dict:
    total = Decimal(amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"amount": f"{total}", "currency": "AUD"}


def audience_body(*, people: int, reachable: dict, excluded: dict, sms: dict, whatsapp_cost, limits: list) -> dict:
    """The ``audience/`` response, from plain counts."""

    def excluded_for(channel):
        counts = excluded.get(channel) or {}
        return {reason: int(counts.get(reason) or 0) for reason in EXCLUDED[channel]}

    return {
        "people": people,
        "channels": {
            "email": {"reachable": reachable.get("email", 0), "excluded": excluded_for(Channel.EMAIL)},
            "sms": {
                "reachable": reachable.get("sms", 0),
                "excluded": excluded_for(Channel.SMS),
                "segments_per_message": sms.get("segments_per_message", 0),
                "encoding": sms.get("encoding", GSM7),
                "estimated_cost": money(sms.get("estimated_cost")),
            },
            "whatsapp": {
                "reachable": reachable.get("whatsapp", 0),
                "excluded": excluded_for(Channel.WHATSAPP),
                "estimated_cost": money(whatsapp_cost),
            },
        },
        "limits": limits,
    }


def mask_address(channel: str, address: str) -> str:
    """``s•••@mlai.au`` or ``+61•••678``."""

    address = str(address or "")
    if channel == Channel.EMAIL:
        local, _, domain = address.partition("@")
        return f"{local[:1]}•••@{domain}" if local and domain else "•••"
    return f"{address[:3]}•••{address[-3:]}" if len(address) > 6 else "•••"


# Validation


def twilio_allowed(blast) -> bool:
    return blast.purpose == Blast.Purpose.EVENT_UPDATE and blast.event_id is not None


def check_merge_tags(text: str, field: str, *, has_event: bool) -> None:
    try:
        merge_tags.validate(text)
    except merge_tags.UnsupportedMergeTag as exc:
        raise BlastError(exc.code, tags=list(exc.tags), field=field) from None
    if not has_event and set(merge_tags.tags_in(text)) & set(merge_tags.EVENT_TAGS):
        raise BlastError("merge_tag_needs_event", field=field)


def check_channel_rules(blast) -> None:
    """Cross-field rules a draft must always meet."""

    channels = list(blast.channels or [])
    for channel in channels:
        if channel not in CHANNELS:
            raise BlastError("invalid_channel", channel=channel)
        if channel in TWILIO_CHANNELS and not twilio_allowed(blast):
            raise BlastError("channel_not_allowed", channel=channel)
    if blast.purpose == Blast.Purpose.EVENT_UPDATE and blast.event_id is None:
        raise BlastError("event_required")
    if blast.quick_segment and blast.event_id is None:
        raise BlastError("event_required")
    has_event = blast.event_id is not None
    check_merge_tags(blast.subject, "subject", has_event=has_event)
    check_merge_tags(blast.body_markdown, "body_markdown", has_event=has_event)
    check_merge_tags(blast.sms_body, "sms_body", has_event=has_event)


def check_channels_available(channels: Iterable[str], *, status: int) -> None:
    availability = channel_availability()
    for channel in channels:
        entry = availability.get(channel) or {}
        if not entry.get("available"):
            raise BlastError("channel_disabled", status, channel=channel, reason=entry.get("reason"))


def check_ready(blast, channels: Optional[Iterable[str]] = None) -> None:
    """Everything sending needs on the chosen channels; raises ``BlastError``."""

    check_channel_rules(blast)
    channels = list(blast.channels or []) if channels is None else list(channels)
    if not channels:
        raise BlastError("channels_required")
    if Channel.EMAIL in channels:
        if blast.mailbox_id is None:
            raise BlastError("mailbox_required")
        if not blast.mailbox.enabled:
            raise BlastError("mailbox_disabled")
        if not blast.subject.strip():
            raise BlastError("subject_required")
        if not blast.body_markdown.strip():
            raise BlastError("body_required")
    if Channel.SMS in channels and not blast.sms_body.strip():
        raise BlastError("sms_body_required")
    if Channel.WHATSAPP in channels:
        _check_template(blast)


def _check_template(blast) -> None:
    template = blast.whatsapp_template if blast.whatsapp_template_id else None
    if template is None:
        raise BlastError("template_required")
    if not (template.approved and template.enabled and template.channel == Channel.WHATSAPP):
        raise BlastError("template_unavailable")
    custom = blast.whatsapp_variables if isinstance(blast.whatsapp_variables, dict) else {}
    for slot in template.variables or []:
        if not isinstance(slot, dict):
            continue
        index = str(slot.get("index") or "").strip()
        source = str(slot.get("source") or "custom")
        if source == "custom":
            if not " ".join(str(custom.get(index) or "").split()):
                raise BlastError("template_variable_missing", index=index)
        elif source not in merge_tags.SUPPORTED_TAGS:
            raise BlastError("template_unavailable")
        elif source in merge_tags.EVENT_TAGS and blast.event_id is None:
            raise BlastError("merge_tag_needs_event", field="whatsapp")


# Audience


@dataclass
class Audience:
    """Reachable contacts and exclusion counts per channel."""

    querysets: dict
    excluded: dict


def audience_definition(blast, *, now) -> Optional[dict]:
    """The blast's rules; ``None`` when nobody can match yet (no-shows before the event ends)."""

    if blast.quick_segment:
        if blast.quick_segment not in QUICK_SEGMENTS:
            raise BlastError("invalid_quick_segment")
        if blast.event_id is None:
            raise BlastError("event_required")
        if blast.quick_segment == "no_show" and not event_has_ended(blast.event, now):
            return None
        return quick_segment_definition(blast.quick_segment, blast.event)
    if blast.status == Blast.Status.DRAFT and blast.segment_id and blast.segment:
        # A draft follows its saved segment; queueing snapshots the rules it used.
        return validate_definition(blast.segment.definition)
    if blast.segment_definition:
        return blast.segment_definition
    raise BlastError("audience_required")


def _ordered_counts(queryset, reasons: list) -> dict:
    """Count each person once, under the first reason that applies."""

    aggregates, remaining = {}, Q()
    for name, condition in reasons:
        aggregates[name] = Count("pk", filter=remaining & condition)
        remaining &= ~condition
    return queryset.aggregate(**aggregates) if aggregates else {}


def resolve_audience(blast, *, now) -> Audience:
    definition = audience_definition(blast, now=now)
    empty = Contact.objects.none()
    querysets = {channel: empty for channel in CHANNELS}
    excluded = {channel: {} for channel in CHANNELS}
    if definition is None:
        return Audience(querysets, excluded)

    querysets[Channel.EMAIL], excluded[Channel.EMAIL] = evaluate(
        definition, blast.purpose, blast.event, now=now
    )
    if not twilio_allowed(blast):
        return Audience(querysets, excluded)

    registered = EventRegistration.objects.filter(
        registered_q(), event_id=blast.event_id, contact=OuterRef("pk")
    )
    matched = (
        Contact.objects.filter(definition_q(validate_definition(definition), now=now))
        .filter(Exists(registered))
        .annotate(is_banned=banned())
    )
    allowed = reduce(
        or_,
        [Q(phone_e164__startswith=prefix) for prefix in sms_allowed_prefixes()],
        _NEVER,
    )
    whatsapp = Q(is_banned=False, whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED) & ~Q(phone_e164="")
    sms_reasons = [
        ("banned", Q(is_banned=True)),
        ("opted_out", Q(sms_status=Contact.SmsStatus.OPTED_OUT)),
        ("no_mobile", Q(phone_e164="") | ~Q(sms_status=Contact.SmsStatus.ACTIVE)),
        ("not_allowed_country", ~allowed),
    ]
    sms = Q(is_banned=False, sms_status=Contact.SmsStatus.ACTIVE) & ~Q(phone_e164="") & allowed
    if Channel.WHATSAPP in (blast.channels or []) and blast.sms_only_without_whatsapp:
        sms_reasons.append(("covered_by_whatsapp", whatsapp))
        sms &= ~whatsapp
    excluded[Channel.SMS] = _ordered_counts(matched, sms_reasons)
    excluded[Channel.WHATSAPP] = _ordered_counts(
        matched,
        [
            ("banned", Q(is_banned=True)),
            ("no_phone", Q(phone_e164="")),
            ("not_opted_in", ~Q(whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED)),
        ],
    )
    querysets[Channel.SMS] = matched.filter(sms)
    querysets[Channel.WHATSAPP] = matched.filter(whatsapp)
    return Audience(querysets, excluded)


def people_count(audience: Audience, channels: Iterable[str]) -> int:
    """Unique people across the chosen channels."""

    chosen = [
        audience.querysets[channel]
        for channel in channels
        if channel in audience.querysets and not audience.querysets[channel].query.is_empty()
    ]
    if not chosen:
        return 0
    if len(chosen) == 1:
        return chosen[0].count()
    return Contact.objects.filter(reduce(or_, [Q(pk__in=qs.values("pk")) for qs in chosen])).count()


def sms_estimate(blast, queryset) -> dict:
    """Segments and cost with merge tags rendered for each recipient."""

    def segments_for(person):
        try:
            text = compose_sms(merge_tags.render(blast.sms_body, person, blast.event))
        except merge_tags.UnsupportedMergeTag:
            text = compose_sms(blast.sms_body)
        return sms_segments(text)

    if not blast.sms_body.strip():
        return {"segments_per_message": 0, "encoding": GSM7, "estimated_cost": Decimal("0")}
    encoding, worst, total, count = GSM7, 0, 0, 0
    rows = queryset.values("first_name", "last_name", "display_name").order_by()
    for row in rows.iterator(chunk_size=2000):
        row_encoding, segments = segments_for(SimpleNamespace(**row))
        total += segments
        worst = max(worst, segments)
        count += 1
        if row_encoding == UCS2:
            encoding = UCS2
    if not count:
        encoding, worst = segments_for(None)
    return {"segments_per_message": worst, "encoding": encoding, "estimated_cost": estimate_cost(total, 1)}


def channel_limits(blast, counts: dict, *, now) -> list:
    """Chosen channels whose recipients exceed what is left of today's cap."""

    limits = []
    for channel in blast.channels or []:
        if channel == Channel.EMAIL:
            if blast.mailbox_id is None:
                continue
            remaining = mailbox_remaining_today(blast.mailbox, now)
        elif channel in TWILIO_CHANNELS:
            remaining = remaining_today(channel, now)
        else:
            continue
        if counts.get(channel, 0) > remaining:
            limits.append({"channel": channel, "code": "daily_limit", "remaining_today": remaining})
    return limits


def audience_summary(blast, audience: Audience, *, now) -> dict:
    reachable = {channel: audience.querysets[channel].count() for channel in CHANNELS}
    chosen = [channel for channel in blast.channels or [] if channel in CHANNELS]
    return audience_body(
        people=people_count(audience, chosen),
        reachable=reachable,
        excluded=audience.excluded,
        sms=sms_estimate(blast, audience.querysets[Channel.SMS]),
        whatsapp_cost=estimate_whatsapp_cost(reachable[Channel.WHATSAPP]),
        limits=channel_limits(blast, reachable, now=now),
    )


def blast_audience(blast, *, now=None) -> dict:
    """The ``audience/`` response for a blast in any state."""

    now = now or timezone.now()
    check_channel_rules(blast)
    return audience_summary(blast, resolve_audience(blast, now=now), now=now)


# Queueing


def _snapshot(blast, recipients: dict) -> list:
    deliveries = []
    for channel, rows in recipients.items():
        for row in rows:
            if channel == Channel.EMAIL:
                contact_id, address, consent = row
                if blast.purpose == Blast.Purpose.EVENT_UPDATE:
                    basis = BlastDelivery.ConsentBasis.EVENT_REGISTRATION
                elif consent == Contact.MarketingConsent.GRANTED:
                    basis = BlastDelivery.ConsentBasis.EXPLICIT
                else:
                    basis = BlastDelivery.ConsentBasis.INFERRED_ATTENDANCE
                address = address.lower()
            else:
                contact_id, address = row
                basis = BlastDelivery.ConsentBasis.EVENT_REGISTRATION
            deliveries.append(
                BlastDelivery(
                    blast=blast,
                    contact_id=contact_id,
                    channel=channel,
                    address=address,
                    consent_basis=basis,
                )
            )
    return deliveries


def _confirmed(confirm, channel) -> Optional[int]:
    value = confirm.get(channel) if isinstance(confirm, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def queue_blast(blast_id, *, confirm: dict, idempotency_key: str, actor, now=None) -> Blast:
    """Check, recount and snapshot a draft, then queue it. The same key returns the same blast.

    Raises ``BlastError``: 403 ``channel_disabled``, 400 for an incomplete
    draft, 409 ``count_changed`` (with fresh counts) or ``daily_limit``.
    """

    require_mlai_admin(actor)
    now = now or timezone.now()
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise BlastError("invalid_idempotency_key")
    replay = Blast.objects.filter(idempotency_key=key).only("pk").first()
    if replay is not None:
        if str(replay.pk) != str(blast_id):
            raise BlastError("idempotency_key_reused", 409)
        return Blast.objects.get(pk=replay.pk)
    try:
        with transaction.atomic():
            blast = (
                Blast.objects.select_for_update(of=("self",))
                .select_related("event", "mailbox", "segment", "whatsapp_template")
                .get(pk=blast_id)
            )
            if blast.idempotency_key == key:
                return blast
            if blast.status != Blast.Status.DRAFT:
                raise BlastError("blast_not_draft", 409, blast_status=blast.status)
            check_channels_available(blast.channels or [], status=403)
            check_ready(blast)
            audience = resolve_audience(blast, now=now)
            columns = {
                Channel.EMAIL: ("pk", "email", "marketing_consent"),
                Channel.SMS: ("pk", "phone_e164"),
                Channel.WHATSAPP: ("pk", "phone_e164"),
            }
            recipients = {
                channel: list(audience.querysets[channel].values_list(*columns[channel]))
                for channel in blast.channels
            }
            counts = {channel: len(rows) for channel, rows in recipients.items()}
            if any(_confirmed(confirm, channel) != count for channel, count in counts.items()):
                raise BlastError("count_changed", 409, **audience_summary(blast, audience, now=now))
            people = {row[0] for rows in recipients.values() for row in rows}
            if not people:
                raise BlastError("no_recipients")
            limits = channel_limits(blast, counts, now=now)
            if limits:
                raise BlastError("daily_limit", 409, limits=limits)
            BlastDelivery.objects.bulk_create(_snapshot(blast, recipients), batch_size=1000)
            blast.segment_definition = audience_definition(blast, now=now) or {}
            blast.status = Blast.Status.QUEUED
            blast.queued_at = now
            blast.recipient_count = len(people)
            blast.idempotency_key = key
            blast.last_error = ""
            blast.save(
                update_fields=[
                    "segment_definition",
                    "status",
                    "queued_at",
                    "recipient_count",
                    "idempotency_key",
                    "last_error",
                    "updated_at",
                ]
            )
            CrmAuditEvent.objects.create(
                actor=actor,
                action=CrmAuditEvent.Action.SEND,
                target_type="blast",
                target_id=str(blast.pk),
                detail={"operation": "queue", "channels": counts, "recipient_count": len(people)},
            )
    except IntegrityError:
        raise BlastError("idempotency_key_reused", 409) from None
    return blast


# Status


def refresh_status(blast_id, *, now=None) -> str:
    """Roll a live blast's status up from its delivery rows."""

    now = now or timezone.now()
    blast = Blast.objects.select_related("mailbox").get(pk=blast_id)
    if blast.status not in LIVE:
        return blast.status
    counts: dict = defaultdict(Counter)
    rows = (
        BlastDelivery.objects.filter(blast=blast)
        .values("channel", "status")
        .annotate(total=Count("id"))
        .order_by()
    )
    for row in rows:
        counts[row["channel"]][row["status"]] = row["total"]
    states = {}
    for channel in blast.channels or []:
        if channel == Channel.EMAIL:
            started = blast_email.email_started(blast)
            blocked = not started and bool(blast_email.email_block(blast))
            provider_failed = (
                blast_email.email_stats(blast).get("listmonk_status") == listmonk.CANCELLED
            )
        else:
            blocked = channel_block(channel) in ("channel_disabled", "not_configured")
            provider_failed = False
        states[channel] = channel_state(counts[channel], blocked=blocked, provider_failed=provider_failed)
    status = rollup_status(states.values())
    if status == blast.status:
        return status
    fields: dict[str, Any] = {"status": status, "updated_at": now}
    if status in (Blast.Status.SENT, Blast.Status.PARTIALLY_SENT) and blast.sent_at is None:
        fields["sent_at"] = now
    if status in (Blast.Status.PARTIALLY_SENT, Blast.Status.FAILED) and not blast.last_error:
        fields["last_error"] = ", ".join(
            f"{channel} {state}" for channel, state in states.items() if state != DONE
        )
    if Blast.objects.filter(pk=blast.pk, status__in=LIVE).update(**fields):
        return status
    return Blast.objects.values_list("status", flat=True).get(pk=blast.pk)


# Dispatch


def fail_unauthorised(blast_id, *, now) -> str:
    """Stop a live blast whose creator is no longer an MLAI admin, and return its status.

    The listmonk campaign is cancelled as ``cancel_blast`` does, rows not yet
    sent fail with ``not_authorised`` and the stop is audited.
    """

    with transaction.atomic():
        blast = Blast.objects.select_for_update().get(pk=blast_id)
        if blast.status not in LIVE or is_mlai_admin_user(blast.created_by):
            return blast.status
        previous = blast.status
        if Channel.EMAIL in (blast.channels or []):
            values = blast_email.cancel_campaign(blast, now=now)
            if values:
                blast.stats = blast_email.with_email_stats(blast.stats, **values)
        BlastDelivery.objects.filter(blast=blast, status=BlastDelivery.Status.QUEUED).update(
            status=BlastDelivery.Status.FAILED, error_code=NOT_AUTHORISED, updated_at=now
        )
        blast.status = Blast.Status.FAILED
        blast.last_error = NOT_AUTHORISED
        blast.save(update_fields=["status", "stats", "last_error", "updated_at"])
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.SEND,
            target_type="blast",
            target_id=str(blast.pk),
            detail={"operation": "refuse", "reason": NOT_AUTHORISED, "previous_status": previous},
        )
    logger.warning("crm blast %s stopped: its creator is no longer an MLAI admin", blast_id)
    return Blast.Status.FAILED


def dispatch_blast(blast_id, *, now=None, twilio_budget: Optional[int] = None) -> dict:
    """One tick for one blast. Call outside any transaction: Twilio claims commit per send."""

    now = now or timezone.now()
    blast = Blast.objects.select_related("event", "mailbox", "created_by").get(pk=blast_id)
    if blast.status not in LIVE:
        return {"status": blast.status, "twilio_sends": 0}
    if not is_mlai_admin_user(blast.created_by):
        return {"status": fail_unauthorised(blast.pk, now=now), "twilio_sends": 0, "refused": NOT_AUTHORISED}
    outcome: dict[str, Any] = {"twilio_sends": 0}
    if Channel.EMAIL in (blast.channels or []):
        try:
            outcome["email"] = blast_email.dispatch_email(blast, now=now)
            # listmonk answered again, so an earlier outage note no longer applies.
            Blast.objects.filter(pk=blast.pk, last_error__in=_LISTMONK_OUTAGES).update(last_error="")
        except ListmonkError as exc:
            if exc.credentials_rejected:
                raise
            if exc.transient:
                logger.warning("crm blast %s email deferred: %s", blast.pk, exc.code)
                Blast.objects.filter(pk=blast.pk).update(last_error=f"listmonk_{exc.code}"[:200])
                outcome["deferred"] = exc.code
            else:
                logger.warning("crm blast %s email refused by listmonk: %s", blast.pk, exc.code)
                blast_email.fail_email(blast.pk, f"listmonk_{exc.code}", now=now)
                outcome["email"] = {"refused": exc.code}
    if any(channel in TWILIO_CHANNELS for channel in blast.channels or []):
        limit = settings.CRM_TWILIO_SENDS_PER_TICK if twilio_budget is None else twilio_budget
        summary = deliver_twilio_deliveries(blast, limit=max(0, int(limit)), now=now)
        outcome.update(summary)
        outcome["twilio_sends"] = sum(
            value
            for counts in summary.values()
            for value in counts.values()
            if isinstance(value, int) and not isinstance(value, bool)
        )
    outcome["status"] = refresh_status(blast.pk, now=now)
    return outcome


def dispatch_enabled() -> bool:
    return any(
        getattr(settings, name, False)
        for name in ("CRM_SENDING_ENABLED", "CRM_SMS_ENABLED", "CRM_WHATSAPP_ENABLED")
    )


def claim_blasts(*, now, limit: int = MAX_BLASTS_PER_TICK) -> list:
    """Live blasts, oldest first, skipping any another dispatcher holds; queued ones become sending."""

    with transaction.atomic():
        rows = list(
            Blast.objects.select_for_update(skip_locked=True)
            .filter(status__in=LIVE)
            .order_by("queued_at", "pk")
            .values_list("pk", "status")[:limit]
        )
        queued = [pk for pk, status in rows if status == Blast.Status.QUEUED]
        if queued:
            Blast.objects.filter(pk__in=queued, status=Blast.Status.QUEUED).update(
                status=Blast.Status.SENDING, updated_at=now
            )
    return [pk for pk, _ in rows]


def run_blast_dispatch_scheduler(*, now=None) -> dict:
    """Scheduler runner ``crm_blast_dispatch``: self-throttled and quiet about outages.

    A failed runner stops the shared scheduler, so only listmonk refusing the
    API user's credentials is reported as ``failed``. A listmonk outage is
    ``deferred``; one blast's unexpected error is logged and the rest go on.
    """

    if not dispatch_enabled():
        return {"status": "skipped", "reason": "disabled"}
    if not cache.add(DISPATCH_THROTTLE_KEY, 1, timeout=settings.CRM_BLAST_DISPATCH_INTERVAL_SECONDS):
        return {"status": "skipped", "reason": "throttled"}
    now = now or timezone.now()
    blast_ids = claim_blasts(now=now)
    budget = settings.CRM_TWILIO_SENDS_PER_TICK
    outcomes: dict[str, str] = {}
    deferred: list[str] = []
    errors: list[str] = []
    for blast_id in blast_ids:
        try:
            outcome = dispatch_blast(blast_id, now=now, twilio_budget=budget)
        except ListmonkError as exc:
            logger.error("crm_blast_dispatch listmonk refused the API credentials: %s", exc.code)
            return {
                "status": "failed",
                "reason": "listmonk_credentials_rejected",
                "status_code": exc.status_code,
            }
        except Exception as exc:
            # The class only: a database error's message can quote an address.
            logger.error("crm_blast_dispatch blast %s raised %s", blast_id, exc.__class__.__name__)
            Blast.objects.filter(pk=blast_id).update(last_error="dispatch_error")
            errors.append(str(blast_id))
            continue
        budget = max(0, budget - outcome["twilio_sends"])
        outcomes[str(blast_id)] = outcome["status"]
        if outcome.get("deferred"):
            deferred.append(str(blast_id))
    if errors:
        status = "partial"
    elif deferred and len(deferred) == len(blast_ids):
        status = "deferred"
    else:
        status = "completed"
    return {
        "status": status,
        "blasts": len(blast_ids),
        "outcomes": outcomes,
        "deferred": deferred,
        "errors": errors,
    }


# Cancel and test


def cancel_blast(blast_id, *, actor, now=None) -> Blast:
    """Cancel a draft or live blast; listmonk's campaign is cancelled if it started."""

    require_mlai_admin(actor)
    now = now or timezone.now()
    with transaction.atomic():
        blast = Blast.objects.select_for_update().get(pk=blast_id)
        if blast.status == Blast.Status.CANCELLED:
            return blast
        if blast.status not in (Blast.Status.DRAFT, *LIVE):
            raise BlastError("blast_not_cancellable", 409, blast_status=blast.status)
        previous = blast.status
        if previous in LIVE and Channel.EMAIL in (blast.channels or []):
            values = blast_email.cancel_campaign(blast, now=now)
            if values:
                blast.stats = blast_email.with_email_stats(blast.stats, **values)
            if values.get("cancel_pending"):
                blast.last_error = "listmonk_cancel_pending"
        # Twilio sends stop between two messages: each claim skips a cancelled blast.
        blast.status = Blast.Status.CANCELLED
        blast.save(update_fields=["status", "stats", "last_error", "updated_at"])
        CrmAuditEvent.objects.create(
            actor=actor,
            action=CrmAuditEvent.Action.SEND,
            target_type="blast",
            target_id=str(blast.pk),
            detail={"operation": "cancel", "previous_status": previous},
        )
    return blast


def _test_person(user):
    email = normalize_contact_email(getattr(user, "email", ""))
    return Contact.objects.filter(email=email).first() if email else None


def send_tests(blast_id, channels: list, *, user, now=None) -> dict:
    """Send a draft to the caller only: their own email and their own ``User.phone``."""

    require_mlai_admin(user)
    blast = Blast.objects.select_related("event", "mailbox", "whatsapp_template").get(pk=blast_id)
    if blast.status != Blast.Status.DRAFT:
        raise BlastError("blast_not_draft", 409, blast_status=blast.status)
    channels = list(dict.fromkeys(channels))
    for channel in channels:
        if channel not in (blast.channels or []):
            raise BlastError("channel_not_selected", channel=channel)
    check_channels_available(channels, status=409)
    check_ready(blast, channels)
    person = _test_person(user) or user
    phone = ""
    if any(channel in TWILIO_CHANNELS for channel in channels):
        phone = normalize_contact_phone(getattr(user, "phone", "") or "").e164
        if not phone:
            raise BlastError("test_phone_missing")
    if Channel.SMS in channels and not sms_allowed(phone, sms_allowed_prefixes()):
        raise BlastError("test_phone_not_allowed")
    template = None
    if Channel.WHATSAPP in channels:
        try:
            template = whatsapp_message(blast, person)
        except DeliveryRefused as exc:
            raise BlastError(exc.code) from None

    sent, failed = [], []
    for channel in channels:
        if channel == Channel.EMAIL:
            address = blast_email.send_test_email(blast.pk, user)
            sent.append({"channel": channel, "to": mask_address(channel, address)})
            continue
        if channel == Channel.SMS:
            body = compose_sms(merge_tags.render(blast.sms_body, person, blast.event))
            result = twilio.send_sms(phone, body, "")
        else:
            content_sid, variables = template
            result = twilio.send_whatsapp_template(phone, content_sid, variables, "")
        if result.outcome == twilio.SENT:
            sent.append({"channel": channel, "to": mask_address(channel, phone)})
        else:
            failed.append({"channel": channel, "error_code": result.error_code or result.outcome})
    CrmAuditEvent.objects.create(
        actor=user,
        action=CrmAuditEvent.Action.SEND,
        target_type="blast",
        target_id=str(blast.pk),
        detail={"operation": "test", "channels": channels, "sent": len(sent)},
    )
    return {"sent": sent, "failed": failed}
