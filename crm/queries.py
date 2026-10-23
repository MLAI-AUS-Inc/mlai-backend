"""Querysets and filters behind the contacts API, built from ORM expressions only.

Every helper that takes ``prefix`` can run on ``Contact`` (``prefix=""``) or
through a relation to it, such as ``EventRegistration`` (``"contact__"``).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

from django.conf import settings
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    Exists,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf

from core.models import AccountBan

from .luma_guests import APPROVED, WAITLIST
from .models import (
    Blast,
    BlastDelivery,
    Contact,
    ContactConsentEvent,
    CrmAuditEvent,
    CrmEvent,
    EventRegistration,
    InboundMessage,
)

PENDING = "pending_approval"
DECLINED = "declined"
INFERRED_CONSENT_DAYS = 730
DEFAULT_SMS_PREFIXES = ("+614",)
GUEST_STATUSES = ("going", "waitlist", "pending", "checked_in", "no_show", "all")
EVENT_SCOPES = ("upcoming", "past")
CONTACT_SORTS = ("recent", "name", "attended")
SEARCH_FIELDS = ("email", "first_name", "last_name", "display_name", "company")
MAX_SEARCH_TERMS = 5
MAX_SEARCH_LENGTH = 200
MIN_PHONE_DIGITS = 4
DETAIL_REGISTRATION_LIMIT = 200
DETAIL_ACTIVITY_LIMIT = 100
_PHONE_LIKE = re.compile(r"^[\d\s()+\-.]+$")
_NOTHING = "pk__isnull"  # never true: a primary key is never null
_SENT = (
    BlastDelivery.Status.SENT,
    BlastDelivery.Status.DELIVERED,
    BlastDelivery.Status.READ,
    BlastDelivery.Status.UNDELIVERED,
    BlastDelivery.Status.BOUNCED,
    BlastDelivery.Status.UNSUBSCRIBED,
)
_DELIVERED = (BlastDelivery.Status.DELIVERED, BlastDelivery.Status.READ)
_FAILED = (BlastDelivery.Status.FAILED, BlastDelivery.Status.UNDELIVERED)


def banned(prefix: str = "") -> Exists:
    """True when the contact's email has an active ``AccountBan``."""

    return Exists(
        AccountBan.objects.filter(revoked_at__isnull=True)
        .annotate(email_key=Lower("email"))
        .filter(email_key=OuterRef(f"{prefix}email"))
    )


def marketing_consent_q(now: datetime, prefix: str = "") -> Q:
    """Granted, or attended an event in the last 24 months; withdrawal wins."""

    inferred_since = now - timedelta(days=INFERRED_CONSENT_DAYS)
    return ~Q(**{f"{prefix}marketing_consent": Contact.MarketingConsent.WITHDRAWN}) & (
        Q(**{f"{prefix}marketing_consent": Contact.MarketingConsent.GRANTED})
        | Q(**{f"{prefix}last_attended_at__gte": inferred_since})
    )


def email_reachable_q(prefix: str = "") -> Q:
    return Q(**{f"{prefix}email_status": Contact.EmailStatus.ACTIVE}) & ~Q(banned(prefix))


def marketing_eligible_q(now: datetime, prefix: str = "") -> Q:
    """Active email, not banned, and the marketing consent rule."""

    return email_reachable_q(prefix) & marketing_consent_q(now, prefix)


def sms_allowed_prefixes() -> list[str]:
    configured = getattr(settings, "CRM_SMS_ALLOWED_PREFIXES", DEFAULT_SMS_PREFIXES)
    if isinstance(configured, str):
        configured = configured.split(",")
    return [str(prefix).strip() for prefix in configured or () if str(prefix).strip()]


def sms_reachable_q(prefix: str = "") -> Q:
    """Textable (``sms_status=active``) on an allowed number prefix, not banned."""

    allowed = None
    for number_prefix in sms_allowed_prefixes():
        starts = Q(**{f"{prefix}phone_e164__startswith": number_prefix})
        allowed = starts if allowed is None else allowed | starts
    if allowed is None:
        allowed = Q(**{_NOTHING: True})
    return Q(**{f"{prefix}sms_status": Contact.SmsStatus.ACTIVE}) & allowed & ~Q(banned(prefix))


def whatsapp_reachable_q(prefix: str = "") -> Q:
    """Opted in to WhatsApp with a number on file, not banned."""

    return (
        Q(**{f"{prefix}whatsapp_opt_in": Contact.WhatsAppOptIn.GRANTED})
        & ~Q(**{f"{prefix}phone_e164": ""})
        & ~Q(banned(prefix))
    )


REACHABLE = {
    "email": email_reachable_q,
    "sms": sms_reachable_q,
    "whatsapp": whatsapp_reachable_q,
}


def has_mobile_q(prefix: str = "") -> Q:
    """A mobile number on file, whether or not the person opted out of texts."""

    return ~Q(**{f"{prefix}phone_e164": ""}) & Q(
        **{f"{prefix}sms_status__in": (Contact.SmsStatus.ACTIVE, Contact.SmsStatus.OPTED_OUT)}
    )


def registered_q(prefix: str = "") -> Q:
    """A registration that still counts: not removed in Luma and not declined."""

    return Q(**{f"{prefix}removed_at__isnull": True}) & ~Q(**{f"{prefix}approval_status": DECLINED})


def event_has_ended(event: CrmEvent, now: datetime) -> bool:
    ended_at = event.end_at or event.start_at
    return bool(ended_at and ended_at < now)


def guest_status_q(status: str, *, event_ended: bool) -> Q:
    """Filter one event's registrations by the guest-list tab."""

    if status == "going":
        return Q(approval_status=APPROVED)
    if status == "waitlist":
        return Q(approval_status=WAITLIST)
    if status == "pending":
        return Q(approval_status=PENDING)
    if status == "checked_in":
        return Q(checked_in_at__isnull=False)
    if status == "no_show":
        if not event_ended:
            return Q(**{_NOTHING: True})
        return Q(approval_status=APPROVED, checked_in_at__isnull=True)
    if status == "all":
        return Q()
    raise ValueError(f"Unknown guest status {status!r}.")


def contact_search_q(text, prefix: str = "") -> Optional[Q]:
    """Every word must match a name, email or company; a phone-like query matches digits."""

    text = " ".join(str(text or "").split())[:MAX_SEARCH_LENGTH]
    if not text:
        return None
    words = Q()
    for term in text.split(" ")[:MAX_SEARCH_TERMS]:
        any_field = Q()
        for name in SEARCH_FIELDS:
            any_field |= Q(**{f"{prefix}{name}__icontains": term})
        words &= any_field
    digits = re.sub(r"\D", "", text)
    if _PHONE_LIKE.match(text) and len(digits) >= MIN_PHONE_DIGITS:
        words |= Q(**{f"{prefix}phone_e164__contains": digits})
        if digits.startswith("0"):
            # Australian numbers are typed locally (0412 …) but stored as +61412 ….
            words |= Q(**{f"{prefix}phone_e164__contains": "61" + digits[1:]})
    return words


def with_marketing_eligible(queryset, now: datetime):
    return queryset.annotate(
        marketing_eligible=Case(
            When(marketing_eligible_q(now), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        )
    )


def contact_sort_name():
    return Lower(
        Coalesce(
            NullIf("display_name", Value("")),
            NullIf("first_name", Value("")),
            "email",
            output_field=CharField(),
        )
    )


def sort_contacts(queryset, sort: str):
    if sort == "name":
        return queryset.annotate(sort_name=contact_sort_name()).order_by("sort_name", "pk")
    if sort == "attended":
        return queryset.order_by(
            "-events_attended_count", F("last_attended_at").desc(nulls_last=True), "pk"
        )
    if sort == "recent":
        return queryset.order_by("-created_at", "-pk")
    raise ValueError(f"Unknown sort {sort!r}.")


def upcoming_events_q(now: datetime) -> Q:
    return Q(end_at__gte=now) | Q(end_at__isnull=True, start_at__gte=now)


def events_with_last_blast():
    return CrmEvent.objects.annotate(
        last_blast_id=Subquery(
            Blast.objects.filter(event=OuterRef("pk"), sent_at__isnull=False)
            .order_by("-sent_at")
            .values("pk")[:1]
        )
    )


def events_queryset(scope: str, *, now: datetime, text: str = ""):
    """Upcoming events soonest first, or past events latest first, with the last blast id."""

    events = events_with_last_blast()
    text = " ".join(str(text or "").split())[:MAX_SEARCH_LENGTH]
    if text:
        events = events.filter(name__icontains=text)
    if scope == "upcoming":
        return events.filter(upcoming_events_q(now)).order_by(
            F("start_at").asc(nulls_last=True), "pk"
        )
    if scope == "past":
        return events.filter(~upcoming_events_q(now)).order_by(
            F("start_at").desc(nulls_last=True), "pk"
        )
    raise ValueError(f"Unknown event scope {scope!r}.")


def next_event(now: datetime) -> Optional[CrmEvent]:
    return events_queryset("upcoming", now=now).first()


def event_stats(events: Iterable[CrmEvent], *, now: datetime) -> dict:
    """Guest counts and per-channel reach for each event, in one query."""

    events = list(events)
    if not events:
        return {}
    rows = (
        EventRegistration.objects.filter(event__in=events, removed_at__isnull=True)
        .values("event_id")
        .annotate(
            going=Count("id", filter=Q(approval_status=APPROVED)),
            waitlist=Count("id", filter=Q(approval_status=WAITLIST)),
            pending=Count("id", filter=Q(approval_status=PENDING)),
            checked_in=Count("id", filter=Q(checked_in_at__isnull=False)),
            going_not_checked_in=Count(
                "id", filter=Q(approval_status=APPROVED, checked_in_at__isnull=True)
            ),
            reach_email=Count("id", filter=registered_q() & email_reachable_q("contact__")),
            reach_sms=Count("id", filter=registered_q() & sms_reachable_q("contact__")),
            reach_whatsapp=Count("id", filter=registered_q() & whatsapp_reachable_q("contact__")),
        )
        .order_by()
    )
    by_event = {row["event_id"]: row for row in rows}
    stats = {}
    for event in events:
        row = by_event.get(event.pk, {})
        stats[event.pk] = {
            "counts": {
                "going": row.get("going", 0),
                "waitlist": row.get("waitlist", 0),
                "pending": row.get("pending", 0),
                "checked_in": row.get("checked_in", 0),
                "no_show": row.get("going_not_checked_in", 0) if event_has_ended(event, now) else 0,
            },
            "reach": {
                "email": row.get("reach_email", 0),
                "sms": row.get("reach_sms", 0),
                "whatsapp": row.get("reach_whatsapp", 0),
            },
        }
    return stats


def event_guests(event: CrmEvent, status: str, *, now: datetime, text: str = ""):
    guests = EventRegistration.objects.filter(event=event, removed_at__isnull=True).filter(
        guest_status_q(status, event_ended=event_has_ended(event, now))
    )
    search = contact_search_q(text, prefix="contact__")
    if search is not None:
        guests = guests.filter(search)
    return guests.select_related("contact").order_by(
        F("registered_at").desc(nulls_last=True), "pk"
    )


def contacts_queryset(
    *,
    now: datetime,
    text: str = "",
    tag: str = "",
    email_status: str = "",
    marketing_only: bool = False,
    has_mobile: Optional[bool] = None,
    whatsapp: str = "",
    segment_q: Optional[Q] = None,
    sort: str = "recent",
):
    """The contact list with every filter the API accepts; each one narrows it."""

    contacts = Contact.objects.all()
    if segment_q is not None:
        contacts = contacts.filter(segment_q)
    search = contact_search_q(text)
    if search is not None:
        contacts = contacts.filter(search)
    if tag:
        contacts = contacts.filter(tags__contains=[tag])
    if email_status:
        contacts = contacts.filter(email_status=email_status)
    if marketing_only:
        contacts = contacts.filter(marketing_eligible_q(now))
    if has_mobile is not None:
        contacts = contacts.filter(has_mobile_q() if has_mobile else ~has_mobile_q())
    if whatsapp:
        contacts = contacts.filter(whatsapp_opt_in=whatsapp)
    return sort_contacts(with_marketing_eligible(contacts, now), sort)


def contact_registrations(contact: Contact):
    return (
        EventRegistration.objects.filter(contact=contact, removed_at__isnull=True)
        .select_related("event")
        .order_by(F("event__start_at").desc(nulls_last=True), "pk")[:DETAIL_REGISTRATION_LIMIT]
    )


def contact_deliveries(contact: Contact):
    return (
        BlastDelivery.objects.filter(contact=contact)
        .select_related("blast", "blast__whatsapp_template")
        .order_by("-created_at", "-pk")[:DETAIL_ACTIVITY_LIMIT]
    )


def contact_replies(contact: Contact):
    return InboundMessage.objects.filter(contact=contact).order_by("-received_at", "-pk")[
        :DETAIL_ACTIVITY_LIMIT
    ]


def contact_history(contact: Contact):
    return ContactConsentEvent.objects.filter(contact=contact).order_by(
        "-occurred_at", "-created_at", "-pk"
    )[:DETAIL_ACTIVITY_LIMIT]


def blast_stats(blasts: Iterable[Blast]) -> dict:
    """Per-channel delivery counts for each blast, and its reply count, in two queries."""

    ids = [blast.pk for blast in blasts]
    if not ids:
        return {}
    rows = (
        BlastDelivery.objects.filter(blast_id__in=ids)
        .values("blast_id", "channel")
        .annotate(
            recipients=Count("id"),
            sent=Count("id", filter=Q(status__in=_SENT)),
            delivered=Count("id", filter=Q(status__in=_DELIVERED)),
            read=Count("id", filter=Q(status=BlastDelivery.Status.READ)),
            failed=Count("id", filter=Q(status__in=_FAILED)),
            bounced=Count("id", filter=Q(status=BlastDelivery.Status.BOUNCED)),
            unsubscribed=Count("id", filter=Q(status=BlastDelivery.Status.UNSUBSCRIBED)),
            opened=Count("id", filter=Q(first_opened_at__isnull=False)),
            clicked=Count("id", filter=Q(first_clicked_at__isnull=False)),
            segments=Sum("segments"),
            cost=Sum("price"),
            price_unit=Max("price_unit"),
        )
        .order_by()
    )
    replies = dict(
        InboundMessage.objects.filter(blast_id__in=ids)
        .values("blast_id")
        .annotate(total=Count("id"))
        .order_by()
        .values_list("blast_id", "total")
    )
    stats = {blast_id: {"channels": {}, "replies": replies.get(blast_id, 0)} for blast_id in ids}
    for row in rows:
        stats[row["blast_id"]]["channels"][row["channel"]] = row
    return stats


def latest_sent_blast() -> Optional[Blast]:
    return (
        Blast.objects.filter(sent_at__isnull=False)
        .select_related("event", "segment", "created_by", "whatsapp_template")
        .order_by("-sent_at")
        .first()
    )


def luma_sync_status() -> dict:
    """Whether the scheduled sync is on, and the outcome of the last recorded pass."""

    last = (
        CrmAuditEvent.objects.filter(action=CrmAuditEvent.Action.SYNC)
        .order_by("-created_at")
        .first()
    )
    status = "never"
    if last is not None:
        detail = last.detail if isinstance(last.detail, dict) else {}
        status = "partial" if detail.get("failed_events") or detail.get("rate_limited") else "completed"
    return {
        "enabled": bool(getattr(settings, "CRM_LUMA_SYNC_ENABLED", False)),
        "last_synced_at": last.created_at if last else None,
        "status": status,
    }


def contact_overview(now: datetime) -> dict:
    return Contact.objects.aggregate(
        total=Count("pk"),
        marketing_eligible=Count("pk", filter=marketing_eligible_q(now)),
        added_last_30_days=Count("pk", filter=Q(created_at__gte=now - timedelta(days=30))),
        with_mobile=Count("pk", filter=has_mobile_q()),
        whatsapp_opted_in=Count("pk", filter=Q(whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED)),
    )


def tag_counts() -> list[dict]:
    """Every tag in use with how many contacts carry it, most used first."""

    counts: Counter = Counter()
    for tags in Contact.objects.exclude(tags=[]).values_list("tags", flat=True).iterator(chunk_size=2000):
        counts.update({tag for tag in tags or () if isinstance(tag, str) and tag})
    return [{"tag": tag, "count": count} for tag, count in _ranked(counts)]


def attribute_key_counts() -> list[dict]:
    """Attribute keys with how many contacts have a non-empty value."""

    counts: Counter = Counter()
    rows = Contact.objects.exclude(attributes={}).values_list("attributes", flat=True)
    for attributes in rows.iterator(chunk_size=2000):
        if isinstance(attributes, dict):
            counts.update(key for key, value in attributes.items() if str(value or "").strip())
    return [{"key": key, "count": count} for key, count in _ranked(counts)]


def registration_question_counts() -> list[dict]:
    """Registration question labels with how many current registrations answered them."""

    counts: Counter = Counter()
    rows = (
        EventRegistration.objects.filter(removed_at__isnull=True)
        .exclude(registration_answers=[])
        .values_list("registration_answers", flat=True)
    )
    for answers in rows.iterator(chunk_size=2000):
        counts.update(
            {
                answer["label"]
                for answer in answers or ()
                if isinstance(answer, dict)
                and isinstance(answer.get("label"), str)
                and str(answer.get("value") or "").strip()
            }
        )
    return [{"label": label, "count": count} for label, count in _ranked(counts)]


def _ranked(counts: Counter) -> list[tuple]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
