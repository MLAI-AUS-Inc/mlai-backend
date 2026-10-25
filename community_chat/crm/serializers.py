"""Strict contacts API inputs and wire shapes: snake_case keys, UTC times ending in Z."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

from crm.blasts import QUICK_SEGMENTS as BLAST_QUICK_SEGMENTS, mask_address
from crm.contact_import import MAX_TAGS, clean_tags
from crm.emails import normalize_contact_email
from crm.models import Blast, BlastDelivery, Channel, Contact, ContactImport, CrmAuditEvent
from crm.phones import normalize_contact_phone
from crm.queries import CONTACT_SORTS, EVENT_SCOPES, GUEST_STATUSES, INFERRED_CONSENT_DAYS
from crm.segments import ATTRIBUTE_KEY, PURPOSES

from .errors import CrmError

PROFILE_TEXT_FIELDS = {
    "first_name": 150,
    "last_name": 150,
    "display_name": 300,
    "company": 300,
    "job_title": 300,
    "city": 120,
}
EDITABLE_FIELDS = (*PROFILE_TEXT_FIELDS, "phone", "linkedin_url", "tags", "attributes")
MAX_ATTRIBUTE_KEYS = 50
MAX_ATTRIBUTE_VALUE = 2000
MAX_IMPORT_HEADERS = 200
MAX_BLAST_BODY = 100_000
MAX_SMS_BODY = 1600
MAX_TEMPLATE_VARIABLES = 20
MAX_TEMPLATE_VALUE = 500
QUICK_SEGMENTS = {
    "going": "Going",
    "checked_in": "Checked in",
    "no_show": "No-shows",
    "waitlist": "Waitlist",
}
_TITLE_LENGTH = 60
BlastDeliveryStatus = BlastDelivery.Status
_SCALARS = (str, int, float, bool, type(None))


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def money(amount, currency: str = "") -> dict:
    total = abs(Decimal(amount or 0)).quantize(Decimal("0.01"))
    return {"amount": f"{total}", "currency": (currency or "AUD").upper()}


class StrictSerializer(serializers.Serializer):
    """Reject fields the endpoint does not accept instead of ignoring them."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError(
                {"non_field_errors": [ErrorDetail("Send a JSON object.", code="invalid")]}
            )
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {key: [ErrorDetail("This field is not accepted.", code="unknown_field")] for key in unknown}
            )
        return super().to_internal_value(data)


def _text_field(limit: int):
    return serializers.CharField(max_length=limit, allow_blank=True, required=False)


class ContactFieldsInput(StrictSerializer):
    first_name = _text_field(150)
    last_name = _text_field(150)
    display_name = _text_field(300)
    company = _text_field(300)
    job_title = _text_field(300)
    city = _text_field(120)
    phone = _text_field(64)
    linkedin_url = _text_field(500)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=64), max_length=MAX_TAGS, required=False
    )
    attributes = serializers.DictField(
        child=serializers.CharField(max_length=MAX_ATTRIBUTE_VALUE, allow_blank=True),
        required=False,
    )

    def validate_phone(self, value):
        value = " ".join(value.split())
        if value and not normalize_contact_phone(value).e164:
            raise serializers.ValidationError("Enter a mobile or phone number.", code="invalid_phone")
        return value

    def validate_linkedin_url(self, value):
        if not value:
            return value
        try:
            URLValidator(schemes=["https"])(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError("Use an https:// link.", code="https_required") from exc
        return value

    def validate_tags(self, value):
        return clean_tags(value)

    def validate_attributes(self, value):
        if len(value) > MAX_ATTRIBUTE_KEYS:
            raise serializers.ValidationError("Too many attributes.", code="too_many_attributes")
        for key in value:
            if not ATTRIBUTE_KEY.match(key):
                raise serializers.ValidationError(
                    "Attribute keys use lowercase letters, digits and underscores.",
                    code="invalid_attribute_key",
                )
        return {key: " ".join(text.split()) for key, text in value.items()}


class ContactCreateInput(ContactFieldsInput):
    email = serializers.CharField(max_length=254)

    def validate_email(self, value):
        email = normalize_contact_email(value)
        if not email:
            raise serializers.ValidationError("Enter a valid email address.", code="invalid_email")
        return email


class StopMessagingInput(StrictSerializer):
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=Channel.values), min_length=1, max_length=3
    )
    reason = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")

    def validate_channels(self, value):
        return list(dict.fromkeys(value))


class ImportPlanInput(StrictSerializer):
    headers = serializers.ListField(
        child=serializers.CharField(max_length=255, allow_blank=True, trim_whitespace=False),
        min_length=1,
        max_length=MAX_IMPORT_HEADERS,
    )

    def validate_headers(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Column headers must be unique.", code="duplicate_header")
        return value


class ImportCreateInput(StrictSerializer):
    file_name = serializers.CharField(max_length=255)
    consent = serializers.ChoiceField(choices=ContactImport.Consent.values)
    consent_source = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    tags = serializers.ListField(
        child=serializers.CharField(max_length=64), max_length=MAX_TAGS, required=False, default=list
    )
    mapping = serializers.DictField(
        child=serializers.CharField(max_length=300), allow_empty=False
    )
    row_count = serializers.IntegerField(min_value=0, max_value=1_000_000, required=False, default=0)
    dry_run = serializers.BooleanField(required=False, default=False)


class ImportRowsInput(StrictSerializer):
    start_row = serializers.IntegerField(min_value=0)
    rows = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate_rows(self, value):
        for row in value:
            if any(not isinstance(header, str) or not isinstance(cell, _SCALARS) for header, cell in row.items()):
                raise serializers.ValidationError("Row values must be text or numbers.", code="invalid_rows")
        return value


class SegmentInput(StrictSerializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    definition = serializers.JSONField()


class SegmentPreviewInput(StrictSerializer):
    definition = serializers.JSONField()
    purpose = serializers.ChoiceField(choices=PURPOSES, allow_null=True, required=False, default=None)
    event_id = serializers.UUIDField(allow_null=True, required=False, default=None)


def _channel_list(**kwargs):
    return serializers.ListField(
        child=serializers.ChoiceField(choices=Channel.values), max_length=3, **kwargs
    )


class BlastEmailInput(StrictSerializer):
    mailbox_id = serializers.UUIDField(allow_null=True, required=False)
    subject = serializers.CharField(max_length=300, allow_blank=True, required=False)
    body_markdown = serializers.CharField(
        max_length=MAX_BLAST_BODY, allow_blank=True, required=False, trim_whitespace=False
    )


MAX_MAILBOX_DAILY_LIMIT = 100_000


def _valid_email(value: str) -> str:
    email = normalize_contact_email(value)
    if not email:
        raise serializers.ValidationError("Enter a valid email address.", code="invalid_email")
    return email


class MailboxUpdateInput(StrictSerializer):
    """A sender's editable settings; on ``PATCH`` only sent keys change. The address is fixed."""

    display_name = serializers.CharField(max_length=200)
    reply_to = serializers.CharField(max_length=254, allow_blank=True, required=False, default="")
    daily_limit = serializers.IntegerField(
        min_value=1, max_value=MAX_MAILBOX_DAILY_LIMIT, required=False, default=500
    )
    enabled = serializers.BooleanField(required=False, default=False)

    def validate_display_name(self, value):
        name = " ".join(value.split())
        if not name:
            raise serializers.ValidationError("Enter a From name.", code="blank")
        return name

    def validate_reply_to(self, value):
        return _valid_email(value) if value.strip() else ""


class MailboxCreateInput(MailboxUpdateInput):
    email = serializers.CharField(max_length=254)

    def validate_email(self, value):
        return _valid_email(value)


class BlastSmsInput(StrictSerializer):
    body = serializers.CharField(max_length=MAX_SMS_BODY, allow_blank=True, required=False)


class BlastWhatsAppInput(StrictSerializer):
    template_id = serializers.UUIDField(allow_null=True, required=False)
    variables = serializers.DictField(
        child=serializers.CharField(max_length=MAX_TEMPLATE_VALUE, allow_blank=True), required=False
    )

    def validate_variables(self, value):
        if len(value) > MAX_TEMPLATE_VARIABLES:
            raise serializers.ValidationError("Too many template values.", code="too_many_variables")
        for key in value:
            if not key.isdigit() or not 0 < int(key) < 100:
                raise serializers.ValidationError("Template slots are numbered 1 to 99.", code="invalid_slot")
        return {str(int(key)): " ".join(text.split()) for key, text in value.items()}


class BlastInput(StrictSerializer):
    """A blast draft. On ``PATCH`` every key is optional and only sent keys change."""

    purpose = serializers.ChoiceField(choices=Blast.Purpose.values, required=False)
    event_id = serializers.UUIDField(allow_null=True, required=False)
    quick_segment = serializers.ChoiceField(
        choices=BLAST_QUICK_SEGMENTS, allow_null=True, allow_blank=True, required=False
    )
    segment_id = serializers.UUIDField(allow_null=True, required=False)
    channels = _channel_list(required=False)
    sms_only_without_whatsapp = serializers.BooleanField(required=False)
    email = BlastEmailInput(required=False)
    sms = BlastSmsInput(required=False)
    whatsapp = BlastWhatsAppInput(required=False)

    def validate_channels(self, value):
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        if attrs.get("quick_segment") and attrs.get("segment_id"):
            raise serializers.ValidationError(
                {"segment_id": [ErrorDetail("Choose a quick segment or a saved segment.", code="audience_conflict")]}
            )
        return attrs


class BlastSendInput(StrictSerializer):
    confirm = serializers.DictField(child=serializers.IntegerField(min_value=0))
    idempotency_key = serializers.CharField(max_length=64)

    def validate_confirm(self, value):
        if any(key not in Channel.values for key in value):
            raise serializers.ValidationError("Confirm counts per channel.", code="invalid_channel")
        return value


class BlastTestInput(StrictSerializer):
    channels = _channel_list(min_length=1)


def blast_list_filters(params) -> dict:
    status = params.get("status") or ""
    if status and status not in Blast.Status.values:
        raise CrmError("invalid_filter", field="status")
    channel = params.get("channel") or ""
    if channel and channel not in Channel.values:
        raise CrmError("invalid_filter", field="channel")
    event_id = params.get("event_id") or ""
    return {
        "status": status,
        "channel": channel,
        "event_id": parse_uuid(event_id, "event_id") if event_id else "",
    }


def delivery_filters(params) -> dict:
    channel = params.get("channel") or ""
    if channel and channel not in Channel.values:
        raise CrmError("invalid_filter", field="channel")
    status = params.get("status") or ""
    if status and status not in BlastDeliveryStatus.values:
        raise CrmError("invalid_filter", field="status")
    return {"channel": channel, "status": status}


def contact_list_filters(params) -> dict:
    """Parse the contact list's query string; any unknown value is a 400."""

    sort = params.get("sort") or "recent"
    if sort not in CONTACT_SORTS:
        raise CrmError("invalid_filter", field="sort")
    email_status = params.get("email_status") or ""
    if email_status and email_status not in Contact.EmailStatus.values:
        raise CrmError("invalid_filter", field="email_status")
    eligible = params.get("eligible") or ""
    if eligible not in ("", "marketing"):
        raise CrmError("invalid_filter", field="eligible")
    has_mobile = {"": None, "1": True, "true": True, "0": False, "false": False}.get(
        str(params.get("has_mobile") or "").lower(), "invalid"
    )
    if has_mobile == "invalid":
        raise CrmError("invalid_filter", field="has_mobile")
    whatsapp = params.get("whatsapp") or ""
    if whatsapp and whatsapp not in Contact.WhatsAppOptIn.values:
        raise CrmError("invalid_filter", field="whatsapp")
    segment_id = params.get("segment_id") or ""
    if segment_id:
        segment_id = parse_uuid(segment_id, "segment_id")
    return {
        "text": params.get("q") or "",
        "tag": " ".join(str(params.get("tag") or "").split())[:64],
        "email_status": email_status,
        "marketing_only": eligible == "marketing",
        "has_mobile": has_mobile,
        "whatsapp": whatsapp,
        "sort": sort,
        "segment_id": segment_id or None,
    }


def choice_param(params, name: str, allowed, default: str) -> str:
    value = params.get(name) or default
    if value not in allowed:
        raise CrmError("invalid_filter", field=name)
    return value


def event_scope(params) -> str:
    return choice_param(params, "scope", EVENT_SCOPES, "upcoming")


def guest_status(params) -> str:
    return choice_param(params, "status", GUEST_STATUSES, "all")


def parse_uuid(value, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise CrmError("invalid_filter", field=field) from exc


def _display_name(contact: Contact) -> str:
    return contact.display_name or " ".join(
        part for part in (contact.first_name, contact.last_name) if part
    )


def contact_row(contact: Contact) -> dict:
    return {
        "id": str(contact.pk),
        "email": contact.email,
        "display_name": contact.display_name,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company": contact.company,
        "job_title": contact.job_title,
        "tags": list(contact.tags or []),
        "phone_e164": contact.phone_e164,
        "events_registered_count": contact.events_registered_count,
        "events_attended_count": contact.events_attended_count,
        "last_event_at": iso(contact.last_event_at),
        "last_attended_at": iso(contact.last_attended_at),
        "email_status": contact.email_status,
        "marketing_consent": contact.marketing_consent,
        "marketing_eligible": bool(getattr(contact, "marketing_eligible", False)),
        "sms_status": contact.sms_status,
        "whatsapp_opt_in": contact.whatsapp_opt_in,
    }


def contact_detail(contact: Contact, *, now, registrations, deliveries, replies, history) -> dict:
    inferred_until = None
    if contact.marketing_consent == Contact.MarketingConsent.UNKNOWN and contact.last_attended_at:
        lapses_at = contact.last_attended_at + timedelta(days=INFERRED_CONSENT_DAYS)
        inferred_until = lapses_at if lapses_at > now else None
    return {
        **contact_row(contact),
        "phone": contact.phone,
        "city": contact.city,
        "linkedin_url": contact.linkedin_url,
        "attributes": dict(contact.attributes or {}),
        "source": contact.source,
        "first_seen_at": iso(contact.first_seen_at),
        "user": {"display_name": contact.user.full_name} if contact.user_id and contact.user else None,
        "consent": {
            "email": {
                "state": contact.marketing_consent,
                "source": contact.marketing_consent_source,
                "at": iso(contact.marketing_consent_at),
                "inferred_until": iso(inferred_until),
            },
            "whatsapp": {
                "state": contact.whatsapp_opt_in,
                "source": contact.whatsapp_opt_in_source,
                "at": iso(contact.whatsapp_opt_in_at),
            },
            "sms": {"status": contact.sms_status, "changed_at": iso(contact.sms_status_changed_at)},
        },
        "registrations": [
            {
                "event": {
                    "id": str(registration.event_id),
                    "name": registration.event.name,
                    "start_at": iso(registration.event.start_at),
                },
                "approval_status": registration.approval_status,
                "registered_at": iso(registration.registered_at),
                "checked_in_at": iso(registration.checked_in_at),
                "answers": [
                    {"label": answer.get("label", ""), "value": answer.get("value", "")}
                    for answer in registration.registration_answers or ()
                    if isinstance(answer, dict)
                ],
            }
            for registration in registrations
        ],
        "messages": [
            {
                "blast_id": str(delivery.blast_id),
                "channel": delivery.channel,
                "title": blast_title(delivery.blast),
                "sent_at": iso(delivery.blast.sent_at or delivery.blast.queued_at),
                "status": delivery.status,
                "first_opened_at": iso(delivery.first_opened_at),
                "first_clicked_at": iso(delivery.first_clicked_at),
            }
            for delivery in deliveries
        ],
        "replies": [
            {
                "channel": reply.channel,
                "body": reply.body,
                "received_at": iso(reply.received_at),
                "blast_id": str(reply.blast_id) if reply.blast_id else None,
            }
            for reply in replies
        ],
        "history": [
            {
                "kind": event.kind,
                "channel": event.channel,
                "source": event.source,
                "occurred_at": iso(event.occurred_at),
            }
            for event in history
        ],
    }


def guest_row(registration) -> dict:
    contact = registration.contact
    return {
        "contact_id": str(contact.pk),
        "display_name": _display_name(contact),
        "email": contact.email,
        "phone_e164": contact.phone_e164,
        "company": contact.company,
        "job_title": contact.job_title,
        "approval_status": registration.approval_status,
        "registered_at": iso(registration.registered_at),
        "checked_in_at": iso(registration.checked_in_at),
        "ticket_names": list(registration.ticket_names or []),
        "email_status": contact.email_status,
        "sms_status": contact.sms_status,
        "whatsapp_opt_in": contact.whatsapp_opt_in,
    }


def blast_title(blast) -> str:
    """The email subject, else the first words of the SMS, else the template name."""

    if blast.subject:
        return blast.subject
    text = _shorten(blast.sms_body)
    if text:
        return text
    template = blast.whatsapp_template if blast.whatsapp_template_id else None
    return template.name if template else ""


_SYNC_SUMMARIES = {
    "luma_calendar": "Luma sync",
    "luma_backfill": "Luma backfill",
    "listmonk": "Email feedback synced",
    "whatsapp_templates": "WhatsApp templates synced",
}
_EDIT_SUMMARIES = {
    ("contact", "create"): "Contact added",
    ("contact", "update"): "Contact edited",
    ("segment", "create"): "Segment created",
    ("segment", "update"): "Segment edited",
    ("segment", "delete"): "Segment deleted",
}
_BLAST_VERBS = {
    ("edit", "create"): "Campaign {title}drafted",
    ("edit", "update"): "Campaign {title}edited",
    ("send", "queue"): "Campaign {title}sent",
    ("send", "cancel"): "Campaign {title}cancelled",
    ("send", "refuse"): "Campaign {title}stopped: its creator is no longer an admin",
    ("send", "test"): "Test of campaign {title}sent",
}


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _contacts(count: int) -> str:
    return f"{count} contact{'' if count == 1 else 's'}"


def activity_summary(event, blasts: dict) -> str:
    """One short line for an audit row, from its action and a few whitelisted detail keys.

    ``blasts`` maps blast ids to blasts, for the campaign's title. Free text in
    ``detail`` never reaches the line.
    """

    action, target = event.action, event.target_type
    detail = event.detail if isinstance(event.detail, dict) else {}
    operation = detail.get("operation")
    Action = CrmAuditEvent.Action
    if action == Action.SYNC:
        return _SYNC_SUMMARIES.get(target, "Sync")
    if action == Action.IMPORT:
        if operation == "cancel":
            return "Import stopped"
        counts = detail.get("counts") if operation else detail
        counts = counts if isinstance(counts, dict) else {}
        imported = _count(counts.get("created")) + _count(counts.get("updated"))
        if detail.get("dry_run"):
            return f"Import checked: {_contacts(imported)}"
        return f"{_contacts(imported)} imported"
    if target == "blast" and (action, operation) in _BLAST_VERBS:
        blast = blasts.get(event.target_id)
        title = _shorten(blast_title(blast)) if blast is not None else ""
        return _BLAST_VERBS[(action, operation)].format(title=f'"{title}" ' if title else "")
    if action == Action.EDIT:
        return _EDIT_SUMMARIES.get((target, operation), "Record edited")
    if action == Action.OPT_OUT:
        return "Contact opted out"
    return str(Action(action).label) if action in Action.values else "Activity"


def activity_row(event, blasts: dict) -> dict:
    actor = event.actor if event.actor_id else None
    return {
        "action": event.action,
        "created_at": iso(event.created_at),
        "actor_name": (actor.full_name or None) if actor is not None else None,
        "summary": activity_summary(event, blasts),
    }


def _shorten(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= _TITLE_LENGTH:
        return text
    return (text[:_TITLE_LENGTH].rsplit(" ", 1)[0] or text[:_TITLE_LENGTH]) + "…"


def event_summary(event, stats: dict, last_blast=None) -> dict:
    return {
        "id": str(event.pk),
        "luma_event_id": event.luma_event_id,
        "name": event.name,
        "url": event.url,
        "cover_url": event.cover_url,
        "start_at": iso(event.start_at),
        "end_at": iso(event.end_at),
        "timezone": event.timezone,
        "counts": stats["counts"],
        "reach": stats["reach"],
        "last_blast": (
            {"id": str(last_blast.pk), "title": blast_title(last_blast), "sent_at": iso(last_blast.sent_at)}
            if last_blast is not None
            else None
        ),
        "guests_synced_at": iso(event.guests_synced_at),
    }


def quick_segments(event, counts: dict, definition_for) -> list:
    return [
        {"key": key, "label": label, "count": counts[key], "definition": definition_for(key, event)}
        for key, label in QUICK_SEGMENTS.items()
    ]


def _audience(blast) -> Optional[dict]:
    if blast.quick_segment:
        label = QUICK_SEGMENTS.get(blast.quick_segment, blast.quick_segment)
        if blast.event_id and blast.event:
            label = f"{label} · {blast.event.name}"
        return {"kind": "quick_segment", "key": blast.quick_segment, "label": label}
    if blast.segment_id and blast.segment:
        return {"kind": "segment", "key": str(blast.segment_id), "label": blast.segment.name}
    if blast.segment_definition:
        return {"kind": "segment", "key": None, "label": "Deleted segment"}
    return None


def _channel_stats(channel: str, row: Optional[dict]) -> dict:
    row = row or {}

    def pick(*names):
        return {name: row.get(name) or 0 for name in names}

    if channel == Channel.EMAIL:
        return pick("recipients", "sent", "opened", "clicked", "bounced", "unsubscribed")
    if channel == Channel.SMS:
        return {
            **pick("recipients", "sent", "delivered", "failed", "segments"),
            "cost": money(row.get("cost"), row.get("price_unit") or ""),
        }
    return pick("recipients", "sent", "delivered", "read", "failed")


def blast_summary(blast, stats: Optional[dict] = None) -> dict:
    stats = stats or {"channels": {}, "replies": 0}
    channels = [channel for channel in blast.channels or [] if channel in Channel.values]
    return {
        "id": str(blast.pk),
        "title": blast_title(blast),
        "purpose": blast.purpose,
        "status": blast.status,
        "event": {"id": str(blast.event_id), "name": blast.event.name} if blast.event_id and blast.event else None,
        "audience": _audience(blast),
        "channels": channels,
        "recipient_count": blast.recipient_count,
        "per_channel": {
            channel: _channel_stats(channel, stats["channels"].get(channel)) for channel in channels
        },
        "replies": stats["replies"],
        "created_by": (
            {"display_name": blast.created_by.full_name} if blast.created_by_id and blast.created_by else None
        ),
        "queued_at": iso(blast.queued_at),
        "sent_at": iso(blast.sent_at),
    }


def blast_progress(blast, rows: dict) -> dict:
    """Per channel ``sent`` of ``to_send``; a running email campaign uses listmonk's counts."""

    progress = {}
    for channel in blast.channels or []:
        if channel not in Channel.values:
            continue
        row = rows.get(channel) or {}
        sent, pending = row.get("sent") or 0, row.get("pending") or 0
        progress[channel] = {"sent": sent, "to_send": sent + pending}
    email = (blast.stats or {}).get("email") if isinstance(blast.stats, dict) else None
    if (
        "email" in progress
        and isinstance(email, dict)
        and email.get("listmonk_status") == "running"
        and isinstance(email.get("sent"), int)
        and isinstance(email.get("to_send"), int)
    ):
        progress["email"] = {"sent": email["sent"], "to_send": email["to_send"]}
    return progress


def blast_detail(blast, stats: Optional[dict] = None) -> dict:
    stats = stats or {"channels": {}, "replies": 0}
    return {
        **blast_summary(blast, stats),
        "sms_only_without_whatsapp": blast.sms_only_without_whatsapp,
        "email": {
            "mailbox_id": str(blast.mailbox_id) if blast.mailbox_id else None,
            "subject": blast.subject,
            "body_markdown": blast.body_markdown,
        },
        "sms": {"body": blast.sms_body},
        "whatsapp": {
            "template_id": str(blast.whatsapp_template_id) if blast.whatsapp_template_id else None,
            "variables": dict(blast.whatsapp_variables or {}),
        },
        "quick_segment": blast.quick_segment or None,
        "segment_id": str(blast.segment_id) if blast.segment_id else None,
        "segment_definition": blast.segment_definition or None,
        "progress": blast_progress(blast, stats["channels"]),
        "last_error": blast.last_error or None,
        "created_at": iso(blast.created_at),
        "updated_at": iso(blast.updated_at),
    }


def delivery_row(delivery) -> dict:
    return {
        "contact_id": str(delivery.contact_id),
        "display_name": _display_name(delivery.contact),
        "channel": delivery.channel,
        "address": mask_address(delivery.channel, delivery.address),
        "status": delivery.status,
        "error_code": delivery.error_code or None,
        "first_opened_at": iso(delivery.first_opened_at),
        "first_clicked_at": iso(delivery.first_clicked_at),
        "updated_at": iso(delivery.updated_at),
    }


def reply_row(message) -> dict:
    contact = message.contact if message.contact_id else None
    return {
        "contact_id": str(message.contact_id) if message.contact_id else None,
        "display_name": _display_name(contact) if contact else "",
        "channel": message.channel,
        "body": message.body,
        "received_at": iso(message.received_at),
    }


def mailbox_dto(mailbox, sent_last_24h: int) -> dict:
    return {
        "id": str(mailbox.pk),
        "email": mailbox.email,
        "display_name": mailbox.display_name,
        "domain": mailbox.domain,
        "route": mailbox.route,
        "reply_to": mailbox.reply_to,
        "daily_limit": mailbox.daily_limit,
        "sent_last_24h": sent_last_24h,
        "remaining_today": max(0, mailbox.daily_limit - sent_last_24h),
        "enabled": mailbox.enabled,
    }


def sending_domain_dto(domain: str, route: Optional[str], health: Optional[dict], mailbox_count: int) -> dict:
    """A sending domain; ``configured`` is false for one only a stored mailbox still names."""

    return {
        "domain": domain,
        "route": route,
        "configured": route is not None,
        "status": health["status"] if health else "not_configured",
        "checked_at": iso(health["checked_at"]) if health else None,
        "records": health["records"] if health else [],
        "mailbox_count": mailbox_count,
    }


def template_dto(template) -> dict:
    return {
        "id": str(template.pk),
        "name": template.name,
        "category": template.category,
        "body": template.body,
        "variables": [
            {
                "index": slot.get("index"),
                "source": slot.get("source") or "custom",
                "label": slot.get("label") or "",
            }
            for slot in template.variables or []
            if isinstance(slot, dict)
        ],
        "approved": template.approved,
    }


def import_record(record: ContactImport) -> dict:
    return {
        "id": str(record.pk),
        "file_name": record.file_name,
        "origin": record.origin,
        "status": record.status,
        "dry_run": record.dry_run,
        "row_count": record.row_count,
        "rows_received": record.rows_received,
        "counts": dict(record.counts or {}),
        "error_sample": list(record.error_sample or []),
        "created_at": iso(record.created_at),
        "finished_at": iso(record.finished_at),
    }


def segment_dto(segment) -> dict:
    return {
        "id": str(segment.pk),
        "name": segment.name,
        "description": segment.description,
        "definition": segment.definition,
        "last_count": segment.last_count,
        "last_counted_at": iso(segment.last_counted_at),
        "updated_at": iso(segment.updated_at),
    }
