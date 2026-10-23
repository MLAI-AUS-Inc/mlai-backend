"""Contact edits, opt-outs and saved segments; every write leaves a ``CrmAuditEvent``.

Audit rows name the changed fields, never their values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models.functions import Lower

from core.models import AccountBan
from crm.luma_sync import users_by_email
from crm.models import Channel, Contact, ContactConsentEvent, CrmAuditEvent, Segment
from crm.phones import NO_PHONE, normalize_contact_phone, sms_status_for
from crm.segments import SegmentError, evaluate, validate_definition

from .errors import CrmError
from .serializers import EDITABLE_FIELDS, PROFILE_TEXT_FIELDS

ADMIN_SOURCE = "mlai_chat:admin"
_PHONE_COLUMNS = ("phone", "phone_e164", "sms_status", "sms_status_changed_at")


def audit(actor, action: str, target_type: str, target_id, detail: dict) -> None:
    CrmAuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        detail=detail,
    )


def set_phone(contact: Contact, raw: str, *, now: datetime) -> bool:
    """Store an edited number; a new number resets the SMS state, but an opt-out sticks."""

    raw = " ".join(str(raw or "").split())[:64]
    phone = normalize_contact_phone(raw) if raw else NO_PHONE
    changed = contact.phone != raw
    contact.phone = raw
    if phone.e164 != contact.phone_e164:
        contact.phone_e164 = phone.e164
        status = sms_status_for(phone, contact.sms_status)
        if status != contact.sms_status:
            contact.sms_status = status
            contact.sms_status_changed_at = now
        changed = True
    return changed


def _apply_fields(contact: Contact, data: dict, *, now: datetime) -> list[str]:
    changed = []
    for name in PROFILE_TEXT_FIELDS:
        if name in data and getattr(contact, name) != data[name]:
            setattr(contact, name, data[name])
            changed.append(name)
    if "linkedin_url" in data and contact.linkedin_url != data["linkedin_url"]:
        contact.linkedin_url = data["linkedin_url"]
        changed.append("linkedin_url")
    if "phone" in data and set_phone(contact, data["phone"], now=now):
        changed.append("phone")
    if "tags" in data and list(contact.tags or []) != data["tags"]:
        contact.tags = data["tags"]
        changed.append("tags")
    if "attributes" in data and dict(contact.attributes or {}) != data["attributes"]:
        contact.attributes = data["attributes"]
        changed.append("attributes")
    return changed


def _columns(changed: list[str]) -> list[str]:
    columns = []
    for name in changed:
        columns.extend(_PHONE_COLUMNS if name == "phone" else (name,))
    return [*columns, "updated_at"]


def _is_banned(email: str) -> bool:
    return (
        AccountBan.objects.annotate(email_key=Lower("email"))
        .filter(email_key=email, revoked_at__isnull=True)
        .exists()
    )


def _existing(email: str) -> CrmError:
    contact_id = Contact.objects.filter(email=email).values_list("pk", flat=True).first()
    return CrmError("contact_exists", 409, id=str(contact_id) if contact_id else None)


def create_contact(data: dict, *, actor, now: datetime) -> Contact:
    """Add a person by hand; banned emails are refused, as in imports."""

    email = data["email"]
    if Contact.objects.filter(email=email).exists():
        raise _existing(email)
    if _is_banned(email):
        raise CrmError("email_banned", 409)
    contact = Contact(
        email=email,
        source=Contact.Source.MANUAL,
        first_seen_at=now,
        user=users_by_email([email]).get(email),
    )
    changed = _apply_fields(contact, {key: data[key] for key in EDITABLE_FIELDS if key in data}, now=now)
    try:
        with transaction.atomic():
            contact.save()
            audit(
                actor,
                CrmAuditEvent.Action.EDIT,
                "contact",
                contact.pk,
                {"operation": "create", "fields": changed},
            )
    except IntegrityError:
        raise _existing(email) from None
    return contact


def update_contact(contact_id, data: dict, *, actor, now: datetime) -> None:
    with transaction.atomic():
        contact = Contact.objects.select_for_update().get(pk=contact_id)
        changed = _apply_fields(contact, data, now=now)
        if not changed:
            return
        contact.save(update_fields=_columns(changed))
        audit(
            actor,
            CrmAuditEvent.Action.EDIT,
            "contact",
            contact.pk,
            {"operation": "update", "fields": changed},
        )


def stop_messaging(contact_id, channels: list[str], *, reason: str, actor, now: datetime) -> None:
    """Opt a contact out of each listed channel and record the evidence; never reversed here."""

    with transaction.atomic():
        contact = Contact.objects.select_for_update().get(pk=contact_id)
        columns, events = {"updated_at"}, []
        for channel in channels:
            if channel == Channel.EMAIL:
                kind = ContactConsentEvent.Kind.UNSUBSCRIBED
                # A bounce or spam complaint is the stronger record, so it stays.
                if contact.email_status == Contact.EmailStatus.ACTIVE:
                    contact.email_status = Contact.EmailStatus.UNSUBSCRIBED
                    contact.email_status_changed_at = now
                    columns |= {"email_status", "email_status_changed_at"}
                if contact.marketing_consent != Contact.MarketingConsent.WITHDRAWN:
                    contact.marketing_consent = Contact.MarketingConsent.WITHDRAWN
                    contact.marketing_consent_source = ADMIN_SOURCE
                    contact.marketing_consent_at = now
                    columns |= {"marketing_consent", "marketing_consent_source", "marketing_consent_at"}
            elif channel == Channel.SMS:
                kind = ContactConsentEvent.Kind.WITHDRAWN
                if contact.sms_status != Contact.SmsStatus.OPTED_OUT:
                    contact.sms_status = Contact.SmsStatus.OPTED_OUT
                    contact.sms_status_changed_at = now
                    columns |= {"sms_status", "sms_status_changed_at"}
            else:
                kind = ContactConsentEvent.Kind.WITHDRAWN
                if contact.whatsapp_opt_in != Contact.WhatsAppOptIn.WITHDRAWN:
                    contact.whatsapp_opt_in = Contact.WhatsAppOptIn.WITHDRAWN
                    contact.whatsapp_opt_in_source = ADMIN_SOURCE
                    contact.whatsapp_opt_in_at = now
                    columns |= {"whatsapp_opt_in", "whatsapp_opt_in_source", "whatsapp_opt_in_at"}
            events.append(
                ContactConsentEvent(
                    contact=contact,
                    channel=channel,
                    kind=kind,
                    source=ADMIN_SOURCE,
                    actor=actor,
                    detail={"reason": reason} if reason else {},
                    occurred_at=now,
                )
            )
        contact.save(update_fields=sorted(columns))
        ContactConsentEvent.objects.bulk_create(events)
        audit(actor, CrmAuditEvent.Action.OPT_OUT, "contact", contact.pk, {"channels": channels})


def segment_count(definition: dict, *, now: datetime) -> Optional[int]:
    """The email audience for a definition, or None while one of its rules is unavailable."""

    try:
        audience, _ = evaluate(definition, None, None, now=now)
    except SegmentError as exc:
        if exc.code == "rule_not_available":
            return None
        raise
    return audience.count()


def save_segment(segment: Optional[Segment], data: dict, *, actor, now: datetime) -> Segment:
    """Create a segment, or update the given one; a new definition is recounted."""

    creating = segment is None
    if creating:
        segment = Segment(created_by=actor)
    if "definition" in data:
        segment.definition = validate_definition(data["definition"])
        segment.last_count = segment_count(segment.definition, now=now)
        segment.last_counted_at = now if segment.last_count is not None else None
    elif creating:
        raise CrmError("invalid_definition", pointer="/", reason="required")
    for name in ("name", "description"):
        if name in data:
            setattr(segment, name, " ".join(data[name].split()) if name == "name" else data[name])
    if not segment.name:
        raise CrmError("invalid_request", fields={"name": ["blank"]})
    with transaction.atomic():
        segment.save()
        audit(
            actor,
            CrmAuditEvent.Action.EDIT,
            "segment",
            segment.pk,
            {"operation": "create" if creating else "update", "fields": sorted(data)},
        )
    return segment


def delete_segment(segment: Segment, *, actor) -> None:
    segment_id = segment.pk
    with transaction.atomic():
        segment.delete()
        audit(actor, CrmAuditEvent.Action.EDIT, "segment", segment_id, {"operation": "delete"})
