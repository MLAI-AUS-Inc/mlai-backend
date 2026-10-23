"""Store MLAI's Luma guests as contacts and event registrations.

Uses the MLAI calendar key (``LUMA_API_KEY``). Luma is the source of truth for
registrations and check-ins; contact profile fields are only filled when blank
so manual edits and imports are never overwritten.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import User, UserEmailAlias
from integrations.services.luma import (
    LumaAPIError,
    LumaAttendeeReportService,
    LumaConfigurationError,
)

from .contact_fields import fill_phone, grant_whatsapp_opt_in
from .luma_guests import (
    APPROVED,
    WAITLIST,
    LumaEventRecord,
    LumaGuestRecord,
    event_record,
    guest_record,
    one_guest_per_email,
)
from .models import (
    Channel,
    Contact,
    ContactConsentEvent,
    CrmAuditEvent,
    CrmEvent,
    EventRegistration,
)

logger = logging.getLogger(__name__)

SYNC_THROTTLE_KEY = "crm:luma-guest-sync:throttle"
BULK_BATCH_SIZE = 250
_FILLABLE_FIELDS = ("first_name", "last_name", "display_name", "company", "job_title")
_PHONE_FIELDS = ("phone", "phone_e164", "sms_status", "sms_status_changed_at")
_WHATSAPP_FIELDS = ("whatsapp_opt_in", "whatsapp_opt_in_source", "whatsapp_opt_in_at")
_REGISTRATION_FIELDS = (
    "luma_guest_id",
    "approval_status",
    "registered_at",
    "checked_in_at",
    "ticket_names",
    "registration_answers",
    "utm_source",
    "custom_source",
    "removed_at",
)


class PacedSession(requests.Session):
    """Space requests evenly to stay inside a per-minute budget."""

    def __init__(self, requests_per_minute: int, *, clock=time.monotonic, sleep=time.sleep):
        super().__init__()
        self._interval = 60.0 / max(1, int(requests_per_minute))
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0

    def request(self, *args, **kwargs):
        now = self._clock()
        if now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + self._interval
        return super().request(*args, **kwargs)


def build_luma_service() -> LumaAttendeeReportService:
    api_key = str(getattr(settings, "LUMA_API_KEY", "") or "").strip()
    if not api_key:
        raise LumaConfigurationError("LUMA_API_KEY is not configured on mlai-backend.")
    return LumaAttendeeReportService(
        api_key=api_key,
        session=PacedSession(settings.CRM_LUMA_REQUESTS_PER_MINUTE),
    )


def sync_event_catalog(service: LumaAttendeeReportService) -> list[CrmEvent]:
    """Upsert every managed calendar event. Reads no guest data."""

    records = [record for record in map(event_record, service.list_all_events()) if record]
    by_id = {record.luma_event_id: record for record in records}
    existing = {
        event.luma_event_id: event
        for event in CrmEvent.objects.filter(luma_event_id__in=by_id)
    }
    created, changed = [], []
    for event_id, record in by_id.items():
        event = existing.get(event_id)
        if event is None:
            created.append(CrmEvent(luma_event_id=event_id, **_event_values(record)))
            continue
        values = _event_values(record)
        if any(getattr(event, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(event, name, value)
            event.updated_at = timezone.now()
            changed.append(event)
    CrmEvent.objects.bulk_create(created, ignore_conflicts=True)
    if changed:
        CrmEvent.objects.bulk_update(
            changed, [*_event_values_fields(), "updated_at"], batch_size=BULK_BATCH_SIZE
        )
    return list(CrmEvent.objects.filter(luma_event_id__in=by_id))


def events_due_for_guest_sync(*, now: datetime, recent_days: int, limit: int) -> list[CrmEvent]:
    """Upcoming events and recently ended ones, least recently synced first."""

    cutoff = now - timedelta(days=recent_days)
    return list(
        CrmEvent.objects.filter(
            Q(end_at__gte=cutoff) | Q(end_at__isnull=True, start_at__gte=cutoff)
        ).order_by(F("guests_synced_at").asc(nulls_first=True), "start_at")[:limit]
    )


def sync_event_guests(
    service: LumaAttendeeReportService,
    event: CrmEvent,
    *,
    now: Optional[datetime] = None,
    consent_labels: Iterable[str] = (),
    whatsapp_labels: Iterable[str] = (),
) -> dict:
    """Replace one event's registrations with Luma's current guest list."""

    now = now or timezone.now()
    raw_guests = service.list_guests(event_id=event.luma_event_id, approval_status=None)
    parsed = [
        guest_record(raw, consent_labels=consent_labels, whatsapp_labels=whatsapp_labels)
        for raw in raw_guests
    ]
    records = one_guest_per_email(record for record in parsed if record)
    skipped = sum(1 for record in parsed if record is None)

    with transaction.atomic():
        contacts, created_count, consent_count, whatsapp_count = _upsert_contacts(
            records, event=event, now=now
        )
        removed_ids = _replace_registrations(event, records, contacts, now=now)
        _refresh_event_counts(event, now=now)
        refresh_contact_rollups({contact.pk for contact in contacts.values()} | removed_ids)

    return {
        "guests": len(records),
        "skipped_without_email": skipped,
        "contacts_created": created_count,
        "consent_granted": consent_count,
        "whatsapp_opt_in": whatsapp_count,
        "registrations_removed": len(removed_ids),
    }


def sync_recent_luma_guests(
    service: LumaAttendeeReportService,
    *,
    now: Optional[datetime] = None,
    recent_days: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    """One scheduled pass: refresh the catalogue, then the events that change."""

    now = now or timezone.now()
    recent_days = settings.CRM_LUMA_SYNC_RECENT_DAYS if recent_days is None else recent_days
    limit = settings.CRM_LUMA_SYNC_MAX_EVENTS_PER_RUN if limit is None else limit
    catalog = sync_event_catalog(service)
    due = events_due_for_guest_sync(now=now, recent_days=recent_days, limit=limit)
    totals: Counter = Counter()
    failed_events: list[str] = []
    rate_limited = False
    synced = 0
    for event in due:
        try:
            totals.update(
                sync_event_guests(
                    service,
                    event,
                    now=now,
                    consent_labels=settings.CRM_LUMA_CONSENT_QUESTION_LABELS,
                    whatsapp_labels=settings.CRM_LUMA_WHATSAPP_QUESTION_LABELS,
                )
            )
            synced += 1
        except LumaAPIError as exc:
            if exc.status_code == 429:
                rate_limited = True
                break
            logger.warning(
                "crm_luma_guest_sync event_failed event=%s status_code=%s",
                event.luma_event_id,
                exc.status_code,
            )
            failed_events.append(event.luma_event_id)

    summary = {
        "events_in_catalog": len(catalog),
        "events_due": len(due),
        "events_synced": synced,
        "failed_events": failed_events,
        "rate_limited": rate_limited,
        **dict(totals),
    }
    CrmAuditEvent.objects.create(
        action=CrmAuditEvent.Action.SYNC, target_type="luma_calendar", detail=summary
    )
    return summary


def run_luma_guest_sync_scheduler(*, now: Optional[datetime] = None, service=None) -> dict:
    """Scheduler runner: self-throttled, and quiet about transient Luma errors.

    A failed runner stops the shared scheduler process, so only a rejected key
    (which also breaks Chat Home) is reported as a failure. Outages and rate
    limits wait for the next interval.
    """

    if not getattr(settings, "CRM_LUMA_SYNC_ENABLED", False):
        return {"status": "skipped", "reason": "disabled"}
    if service is None and not str(getattr(settings, "LUMA_API_KEY", "") or "").strip():
        return {"status": "skipped", "reason": "luma_not_configured"}
    if not cache.add(SYNC_THROTTLE_KEY, 1, timeout=settings.CRM_LUMA_SYNC_INTERVAL_SECONDS):
        return {"status": "skipped", "reason": "throttled"}

    try:
        summary = sync_recent_luma_guests(service or build_luma_service(), now=now)
    except LumaAPIError as exc:
        if exc.status_code in (401, 403):
            return {"status": "failed", "reason": "luma_key_rejected", "status_code": exc.status_code}
        logger.warning("crm_luma_guest_sync deferred status_code=%s", exc.status_code)
        return {"status": "deferred", "reason": "luma_unavailable", "status_code": exc.status_code}

    status = "partial" if summary["failed_events"] or summary["rate_limited"] else "completed"
    return {"status": status, **summary}


def refresh_contact_rollups(contact_ids: Iterable) -> None:
    """Recompute event counts and dates from active registrations."""

    ids = list(contact_ids)
    if not ids:
        return
    counted = Q(approval_status=APPROVED) | Q(checked_in_at__isnull=False)
    rows = (
        EventRegistration.objects.filter(contact_id__in=ids, removed_at__isnull=True)
        .values("contact_id")
        .annotate(
            registered=Count("id", filter=counted),
            attended=Count("id", filter=Q(checked_in_at__isnull=False)),
            last_event=Max("event__start_at", filter=counted),
            last_attended=Max("checked_in_at"),
        )
    )
    by_contact = {row["contact_id"]: row for row in rows}
    contacts = list(Contact.objects.filter(pk__in=ids))
    for contact in contacts:
        row = by_contact.get(contact.pk, {})
        contact.events_registered_count = row.get("registered", 0)
        contact.events_attended_count = row.get("attended", 0)
        contact.last_event_at = row.get("last_event")
        contact.last_attended_at = row.get("last_attended")
    Contact.objects.bulk_update(
        contacts,
        ["events_registered_count", "events_attended_count", "last_event_at", "last_attended_at"],
        batch_size=BULK_BATCH_SIZE,
    )


def _upsert_contacts(
    records: list[LumaGuestRecord], *, event: CrmEvent, now: datetime
) -> tuple[dict[str, Contact], int, int, int]:
    emails = [record.email for record in records]
    if not emails:
        return {}, 0, 0, 0
    already = set(Contact.objects.filter(email__in=emails).values_list("email", flat=True))
    users = users_by_email(emails)
    Contact.objects.bulk_create(
        [
            Contact(
                email=record.email,
                source=Contact.Source.LUMA,
                first_seen_at=record.registered_at or now,
                user=users.get(record.email),
                **{name: getattr(record, name) for name in _FILLABLE_FIELDS},
            )
            for record in records
            if record.email not in already
        ],
        ignore_conflicts=True,
    )

    # Re-read under lock so a concurrent creator's row is the one we update.
    contacts = {
        contact.email: contact
        for contact in Contact.objects.select_for_update().filter(email__in=emails)
    }
    changed, consent_events = [], []
    email_grants = whatsapp_grants = 0
    source = f"luma:{event.luma_event_id}"
    for record in records:
        contact = contacts[record.email]
        dirty = False
        for name in _FILLABLE_FIELDS:
            if not getattr(contact, name) and getattr(record, name):
                setattr(contact, name, getattr(record, name))
                dirty = True
        if fill_phone(contact, record.phone, now=now):
            dirty = True
        if contact.user_id is None and users.get(record.email):
            contact.user = users[record.email]
            dirty = True
        seen_at = record.registered_at or now
        if contact.first_seen_at is None or seen_at < contact.first_seen_at:
            contact.first_seen_at = seen_at
            dirty = True
        if record.consent_granted and contact.marketing_consent == Contact.MarketingConsent.UNKNOWN:
            contact.marketing_consent = Contact.MarketingConsent.GRANTED
            contact.marketing_consent_source = source
            contact.marketing_consent_at = seen_at
            email_grants += 1
            consent_events.append(
                ContactConsentEvent(
                    contact=contact,
                    channel=Channel.EMAIL,
                    kind=ContactConsentEvent.Kind.GRANTED,
                    source=contact.marketing_consent_source,
                    detail={"event": event.luma_event_id},
                    occurred_at=seen_at,
                )
            )
            dirty = True
        if record.whatsapp_opt_in:
            whatsapp_event = grant_whatsapp_opt_in(contact, source=source, at=seen_at)
            if whatsapp_event is not None:
                whatsapp_event.detail = {"event": event.luma_event_id}
                consent_events.append(whatsapp_event)
                whatsapp_grants += 1
                dirty = True
        if dirty:
            contact.updated_at = timezone.now()
            changed.append(contact)
    if changed:
        Contact.objects.bulk_update(
            changed,
            [
                *_FILLABLE_FIELDS,
                *_PHONE_FIELDS,
                *_WHATSAPP_FIELDS,
                "user",
                "first_seen_at",
                "marketing_consent",
                "marketing_consent_source",
                "marketing_consent_at",
                "updated_at",
            ],
            batch_size=BULK_BATCH_SIZE,
        )
    ContactConsentEvent.objects.bulk_create(consent_events)
    return contacts, len(set(emails) - already), email_grants, whatsapp_grants


def _replace_registrations(
    event: CrmEvent,
    records: list[LumaGuestRecord],
    contacts: dict[str, Contact],
    *,
    now: datetime,
) -> set:
    existing = {
        registration.contact_id: registration
        for registration in EventRegistration.objects.select_for_update().filter(event=event)
    }
    created, changed, seen = [], [], set()
    for record in records:
        contact = contacts[record.email]
        seen.add(contact.pk)
        values = {
            "luma_guest_id": record.luma_guest_id,
            "approval_status": record.approval_status,
            "registered_at": record.registered_at,
            "checked_in_at": record.checked_in_at,
            "ticket_names": list(record.ticket_names),
            "registration_answers": list(record.registration_answers),
            "utm_source": record.utm_source,
            "custom_source": record.custom_source,
            "removed_at": None,
        }
        registration = existing.get(contact.pk)
        if registration is None:
            created.append(EventRegistration(event=event, contact=contact, **values))
        elif any(getattr(registration, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(registration, name, value)
            registration.updated_at = timezone.now()
            changed.append(registration)

    removed_ids = set()
    for contact_id, registration in existing.items():
        if contact_id not in seen and registration.removed_at is None:
            registration.removed_at = now
            registration.updated_at = timezone.now()
            changed.append(registration)
            removed_ids.add(contact_id)

    EventRegistration.objects.bulk_create(created, ignore_conflicts=True)
    if changed:
        EventRegistration.objects.bulk_update(
            changed, [*_REGISTRATION_FIELDS, "updated_at"], batch_size=BULK_BATCH_SIZE
        )
    return removed_ids


def _refresh_event_counts(event: CrmEvent, *, now: datetime) -> None:
    counts = EventRegistration.objects.filter(event=event, removed_at__isnull=True).aggregate(
        guest_count=Count("id"),
        approved_count=Count("id", filter=Q(approval_status=APPROVED)),
        waitlist_count=Count("id", filter=Q(approval_status=WAITLIST)),
        checked_in_count=Count("id", filter=Q(checked_in_at__isnull=False)),
    )
    for name, value in counts.items():
        setattr(event, name, value)
    event.guests_synced_at = now
    event.save(update_fields=[*counts, "guests_synced_at", "updated_at"])


def users_by_email(emails: Iterable[str]) -> dict[str, User]:
    """MLAI accounts for these lowercase emails, by primary email or alias."""

    wanted = list(set(emails))
    if not wanted:
        return {}
    found = {
        user.email.lower(): user
        for user in User.objects.annotate(email_key=Lower("email")).filter(email_key__in=wanted)
    }
    aliases = (
        UserEmailAlias.objects.annotate(email_key=Lower("email"))
        .filter(email_key__in=wanted)
        .select_related("user")
    )
    for alias in aliases:
        found.setdefault(alias.email_key, alias.user)
    return found


def _event_values(record: LumaEventRecord) -> dict:
    return {
        "name": record.name,
        "url": record.url,
        "cover_url": record.cover_url,
        "start_at": record.start_at,
        "end_at": record.end_at,
        "timezone": record.timezone,
        "visibility": record.visibility,
        "raw_payload": record.raw,
    }


def _event_values_fields() -> list[str]:
    return ["name", "url", "cover_url", "start_at", "end_at", "timezone", "visibility", "raw_payload"]
