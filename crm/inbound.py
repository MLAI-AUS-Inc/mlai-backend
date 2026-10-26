"""Replies and keywords sent to MLAI's number by SMS or WhatsApp.

An opt-out keyword opts out every contact with that number on the channel it
arrived on; ``START`` / ``UNSTOP`` (and ``JOIN`` on WhatsApp) turn that
channel back on. Any other text is kept as an ``InboundMessage`` linked to the
contact and to the blast they most recently got on that channel.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import BlastDelivery, Channel, Contact, ContactConsentEvent, InboundMessage
from .phones import normalize_contact_phone, sms_status_for

logger = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "PAUSE"})
OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP"})
WHATSAPP_JOIN_KEYWORDS = frozenset({"JOIN"})
REPLY_WINDOW = timedelta(days=7)
_REACHED = (
    BlastDelivery.Status.SENT,
    BlastDelivery.Status.DELIVERED,
    BlastDelivery.Status.READ,
    BlastDelivery.Status.UNKNOWN,
)


def handle_inbound(
    channel: str,
    from_address: str,
    body: str,
    provider_id: str,
    received_at=None,
    *,
    opt_out_type: str = "",
) -> dict[str, Any]:
    """Apply one inbound message and return a small summary; never raises.

    ``opt_out_type`` is Twilio's Advanced Opt-Out classification (``STOP`` /
    ``START``), so keywords configured on the Messaging Service also count.
    """

    channel = str(channel or "")
    try:
        if channel not in (Channel.SMS, Channel.WHATSAPP):
            return {"channel": channel, "action": "ignored", "reason": "unsupported_channel"}
        with transaction.atomic():
            return _handle(
                channel,
                sender_e164(from_address),
                str(body or ""),
                str(provider_id or "").strip()[:64],
                received_at or timezone.now(),
                str(opt_out_type or "").strip().upper(),
            )
    except Exception as exc:
        logger.warning("crm inbound %s message was not handled: %s", channel, exc.__class__.__name__)
        return {"channel": channel, "action": "error"}


def sender_e164(from_address: str) -> str:
    """The sender as E.164; Twilio sends ``whatsapp:+61…`` or bare WaId digits."""

    text = str(from_address or "").strip().removeprefix("whatsapp:").strip()
    if text.isdigit():
        text = "+" + text
    return normalize_contact_phone(text).e164


def keyword_action(channel: str, body: str, opt_out_type: str = "") -> str:
    """``opt_out``, ``opt_in`` or ``reply`` for the whole message, trimmed."""

    keyword = str(body or "").strip().upper()
    if keyword in OPT_OUT_KEYWORDS or opt_out_type == "STOP":
        return "opt_out"
    if keyword in OPT_IN_KEYWORDS or opt_out_type == "START":
        return "opt_in"
    if channel == Channel.WHATSAPP and keyword in WHATSAPP_JOIN_KEYWORDS:
        return "opt_in"
    return "reply"


def _handle(channel, e164, body, provider_id, at, opt_out_type) -> dict[str, Any]:
    summary: dict[str, Any] = {"channel": channel}
    if not e164:
        return {**summary, "action": "ignored", "reason": "unreadable_sender"}
    action = keyword_action(channel, body, opt_out_type)
    if action == "reply":
        return {**summary, **_store_reply(channel, e164, body, provider_id, at)}
    contacts = list(Contact.objects.select_for_update().filter(phone_e164=e164).order_by("created_at"))
    if not contacts:
        return {**summary, "action": "ignored", "reason": "unknown_sender"}
    keyword = body.strip().upper()
    known = OPT_OUT_KEYWORDS | OPT_IN_KEYWORDS | WHATSAPP_JOIN_KEYWORDS
    # Keep only the keyword itself, never free text matched by OptOutType.
    detail = {"keyword": keyword if keyword in known else opt_out_type, "provider_id": provider_id}
    change = _opt_out if action == "opt_out" else _opt_in
    events = [event for event in (change(channel, contact, at, detail) for contact in contacts) if event]
    ContactConsentEvent.objects.bulk_create(events)
    return {**summary, "action": "opted_out" if action == "opt_out" else "opted_in", "contacts": len(events)}


def _opt_out(channel, contact: Contact, at, detail) -> Optional[ContactConsentEvent]:
    source = f"{channel}:inbound"
    if channel == Channel.SMS:
        if contact.sms_status == Contact.SmsStatus.OPTED_OUT:
            return None
        contact.sms_status = Contact.SmsStatus.OPTED_OUT
        contact.sms_status_changed_at = at
        contact.save(update_fields=["sms_status", "sms_status_changed_at", "updated_at"])
        kind = ContactConsentEvent.Kind.UNSUBSCRIBED
    else:
        if contact.whatsapp_opt_in == Contact.WhatsAppOptIn.WITHDRAWN:
            return None
        _set_whatsapp(contact, Contact.WhatsAppOptIn.WITHDRAWN, source, at)
        kind = ContactConsentEvent.Kind.WITHDRAWN
    return ContactConsentEvent(
        contact=contact, channel=channel, kind=kind, source=source, detail=detail, occurred_at=at
    )


def _opt_in(channel, contact: Contact, at, detail) -> Optional[ContactConsentEvent]:
    source = f"{channel}:inbound"
    if channel == Channel.SMS:
        # Only lifts an opt-out, and only for a number that can take texts.
        if contact.sms_status != Contact.SmsStatus.OPTED_OUT:
            return None
        phone = normalize_contact_phone(contact.phone_e164)
        if sms_status_for(phone, Contact.SmsStatus.NONE) != Contact.SmsStatus.ACTIVE:
            return None
        contact.sms_status = Contact.SmsStatus.ACTIVE
        contact.sms_status_changed_at = at
        contact.save(update_fields=["sms_status", "sms_status_changed_at", "updated_at"])
    else:
        if contact.whatsapp_opt_in == Contact.WhatsAppOptIn.GRANTED:
            return None
        _set_whatsapp(contact, Contact.WhatsAppOptIn.GRANTED, source, at)
    return ContactConsentEvent(
        contact=contact,
        channel=channel,
        kind=ContactConsentEvent.Kind.GRANTED,
        source=source,
        detail=detail,
        occurred_at=at,
    )


def _set_whatsapp(contact: Contact, state: str, source: str, at) -> None:
    contact.whatsapp_opt_in = state
    contact.whatsapp_opt_in_source = source
    contact.whatsapp_opt_in_at = at
    contact.save(
        update_fields=["whatsapp_opt_in", "whatsapp_opt_in_source", "whatsapp_opt_in_at", "updated_at"]
    )


def _store_reply(channel, e164, body, provider_id, at) -> dict[str, Any]:
    if not provider_id:
        return {"action": "ignored", "reason": "missing_provider_id"}
    if not body.strip():
        return {"action": "ignored", "reason": "empty"}
    if InboundMessage.objects.filter(provider_id=provider_id).exists():
        return {"action": "duplicate"}
    delivery = (
        BlastDelivery.objects.filter(
            channel=channel,
            status__in=_REACHED,
            created_at__gte=at - REPLY_WINDOW,
            created_at__lte=at,
        )
        .filter(Q(contact__phone_e164=e164) | Q(address__in=(e164, f"whatsapp:{e164}")))
        .select_related("contact")
        .order_by("-created_at", "-id")
        .first()
    )
    contact = delivery.contact if delivery else (
        Contact.objects.filter(phone_e164=e164).order_by("-updated_at").first()
    )
    if contact is None:
        # Nothing reads a reply without a contact, so it is not kept.
        return {"action": "ignored", "reason": "unknown_sender"}
    _, created = InboundMessage.objects.get_or_create(
        provider_id=provider_id,
        defaults={
            "contact": contact,
            "channel": channel,
            "from_address": e164,
            "body": body.strip(),
            "blast_id": delivery.blast_id if delivery else None,
            "received_at": at,
        },
    )
    return {"action": "stored" if created else "duplicate", "linked_to_blast": bool(delivery)}
