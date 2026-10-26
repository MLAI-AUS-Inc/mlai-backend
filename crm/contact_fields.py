"""Field rules shared by the Luma sync, CSV import and contact edits."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import Channel, Contact, ContactConsentEvent
from .phones import normalize_contact_phone, sms_status_for


def fill_phone(contact: Contact, raw_phone: str, *, now: datetime) -> bool:
    """Fill a blank phone and derive its SMS state. Returns True when changed."""

    if contact.phone_e164 or not raw_phone:
        return False
    changed = False
    if not contact.phone:
        contact.phone = str(raw_phone)[:64]
        changed = True
    phone = normalize_contact_phone(raw_phone)
    if not phone.e164:
        return changed
    contact.phone_e164 = phone.e164
    status = sms_status_for(phone, contact.sms_status)
    if status != contact.sms_status:
        contact.sms_status = status
        contact.sms_status_changed_at = now
    return True


def grant_whatsapp_opt_in(
    contact: Contact, *, source: str, at: datetime
) -> Optional[ContactConsentEvent]:
    """Record WhatsApp opt-in unless the person already answered either way."""

    if contact.whatsapp_opt_in != Contact.WhatsAppOptIn.UNKNOWN:
        return None
    contact.whatsapp_opt_in = Contact.WhatsAppOptIn.GRANTED
    contact.whatsapp_opt_in_source = source[:255]
    contact.whatsapp_opt_in_at = at
    return ContactConsentEvent(
        contact=contact,
        channel=Channel.WHATSAPP,
        kind=ContactConsentEvent.Kind.GRANTED,
        source=contact.whatsapp_opt_in_source,
        occurred_at=at,
    )
