"""Invites, blasts and automatic reminders: the rules and Luma-style layouts.

An invite asks people who haven't registered to join an event: it is a
marketing email, tied to one event, and it leaves out everyone already on that
event's guest list or already invited to it. A blast tells the guest list
something about the event (an event update). A reminder is a blast the system
creates and sends on its own (``crm/reminders.py``).

Layouts are built from the event record only, so neither staff nor Roo can put
a wrong date or link in the event card.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q

from . import merge_tags
from .models import Blast, BlastDelivery, EventRegistration

logger = logging.getLogger(__name__)

INVITE = Blast.MessageType.INVITE
BLAST = Blast.MessageType.BLAST
REMINDER = Blast.MessageType.REMINDER
INVITE_EXCLUSIONS = ("already_registered", "already_invited")
GUEST_REFRESH_SECONDS = 600
_SENT_STATES = (
    BlastDelivery.Status.QUEUED,
    BlastDelivery.Status.SUBMITTING,
    BlastDelivery.Status.SENT,
    BlastDelivery.Status.DELIVERED,
    BlastDelivery.Status.READ,
    BlastDelivery.Status.UNDELIVERED,
    BlastDelivery.Status.BOUNCED,
    BlastDelivery.Status.UNKNOWN,
)


class MessageTypeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def check_rules(blast) -> None:
    """Raise ``MessageTypeError`` when the type, purpose and event don't fit together."""

    kind = blast.message_type or ""
    if kind == INVITE:
        if blast.purpose != Blast.Purpose.MARKETING:
            raise MessageTypeError("invite_must_be_marketing")
        if blast.event_id is None:
            raise MessageTypeError("event_required")
        if blast.quick_segment:
            # Event quick segments are the guest list, which an invite never targets.
            raise MessageTypeError("invite_cannot_target_guests")
    elif kind in (BLAST, REMINDER):
        if blast.purpose != Blast.Purpose.EVENT_UPDATE:
            raise MessageTypeError("blast_must_be_event_update")
        if blast.event_id is None:
            raise MessageTypeError("event_required")
    elif kind:
        raise MessageTypeError("invalid_message_type")


# Invite audience


def registered_for(event_id) -> Exists:
    """Anyone on the event's guest list in any state: going, waitlist, pending or checked in."""

    return Exists(EventRegistration.objects.filter(
        event_id=event_id, contact=OuterRef("pk"), removed_at__isnull=True,
    ))


def invited_to(event_id, *, exclude_blast_id=None) -> Exists:
    """Anyone another invite for the same event already reached or is about to reach."""

    deliveries = BlastDelivery.objects.filter(
        contact=OuterRef("pk"),
        channel="email",
        blast__event_id=event_id,
        blast__message_type=INVITE,
        status__in=_SENT_STATES,
    )
    if exclude_blast_id is not None:
        deliveries = deliveries.exclude(blast_id=exclude_blast_id)
    return Exists(deliveries)


def exclude_invitees(blast, queryset) -> tuple:
    """The invite's email audience without guests or people already invited, plus the counts."""

    if (blast.message_type or "") != INVITE or blast.event_id is None:
        return queryset, {}
    annotated = queryset.annotate(
        _registered=registered_for(blast.event_id),
        _invited=invited_to(blast.event_id, exclude_blast_id=blast.pk),
    )
    counts = annotated.aggregate(
        registered=_count(Q(_registered=True)),
        invited=_count(Q(_registered=False, _invited=True)),
    )
    kept = annotated.filter(_registered=False, _invited=False)
    return kept, {
        "already_registered": int(counts["registered"] or 0),
        "already_invited": int(counts["invited"] or 0),
    }


def _count(condition):
    from django.db.models import Count
    return Count("pk", filter=condition)


def invite_drop_reason(blast, contact_id) -> Optional[str]:
    """Checked again for each person just before handing them to listmonk."""

    if (blast.message_type or "") != INVITE or blast.event_id is None:
        return None
    if EventRegistration.objects.filter(
        event_id=blast.event_id, contact_id=contact_id, removed_at__isnull=True,
    ).exists():
        return "already_registered"
    if BlastDelivery.objects.filter(
        contact_id=contact_id, channel="email", blast__event_id=blast.event_id,
        blast__message_type=INVITE, status__in=_SENT_STATES,
    ).exclude(blast_id=blast.pk).exists():
        return "already_invited"
    return None


def refresh_guests_before_invite(blast, *, service=None) -> Optional[dict]:
    """Re-read the event's Luma guest list at most every ten minutes while an invite sends.

    Someone who registers halfway through a gradual send must not then be
    invited. Luma outages never stop the send: the last synced list still
    applies.
    """

    if (blast.message_type or "") != INVITE or blast.event_id is None:
        return None
    if not cache.add(f"crm:invite-guest-refresh:{blast.event_id}", 1, timeout=GUEST_REFRESH_SECONDS):
        return None
    return refresh_event_guests(blast.event, service=service)


def refresh_event_guests(event, *, service=None) -> Optional[dict]:
    from .luma_guests import can_sync_event_guests
    from .luma_sync import build_luma_service, sync_event_guests
    if event is None or not can_sync_event_guests(event.raw_payload):
        return None
    try:
        return sync_event_guests(service or build_luma_service(), event)
    except Exception as exc:  # Luma is optional here; the stored list still applies.
        logger.warning("crm guest refresh deferred event=%s error=%s", event.pk, type(exc).__name__)
        return None


# Layouts


def event_place(event) -> str:
    """The venue address or online link Luma gives, or an empty string."""

    raw = event.raw_payload if isinstance(getattr(event, "raw_payload", None), dict) else {}
    geo = raw.get("geo_address_json") if isinstance(raw.get("geo_address_json"), dict) else {}
    for value in (geo.get("full_address"), geo.get("address"), raw.get("location")):
        text = " ".join(str(value or "").split())
        if text:
            return text[:300]
    if raw.get("meeting_url") or raw.get("zoom_meeting_url"):
        return "Online"
    return ""


def tracked_event_url(event, *, campaign: str) -> str:
    url = str(getattr(event, "url", "") or "").strip()
    if not url.startswith("https://"):
        return ""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({"utm_source": "mlai-crm", "utm_medium": "email", "utm_campaign": campaign})
    return _safe_url(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))


def _md(text) -> str:
    """Event text inside Markdown: no template braces, no HTML, no link syntax."""

    text = " ".join(str(text or "").split())
    for char in "\\[]()*_`#":
        text = text.replace(char, "\\" + char)
    # Braces could open a listmonk template action; angle brackets could open HTML.
    for char, entity in (("{", "&#123;"), ("}", "&#125;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(char, entity)
    return text


def _safe_url(url: str) -> str:
    return url.replace("{", "%7B").replace("}", "%7D").replace(" ", "%20").replace(")", "%29").replace("(", "%28")


def event_card(event, *, button: str, campaign: str, with_cover: bool) -> str:
    when = merge_tags.format_event_start(event.start_at, getattr(event, "timezone", ""))
    place = event_place(event)
    url = tracked_event_url(event, campaign=campaign)
    lines = []
    cover = str(getattr(event, "cover_url", "") or "").strip()
    if with_cover and cover.startswith("https://"):
        lines.append(f"![{_md(event.name)}]({_safe_url(cover)})")
        lines.append("")
    lines.append(f"### {_md(event.name)}")
    details = " · ".join(_md(part) for part in (when, place) if part)
    if details:
        lines.append("")
        lines.append(details)
    if url:
        lines.append("")
        lines.append(f"**[{button} →]({url})**")
    return "\n".join(lines)


def layout(blast, body: str, *, campaign: str) -> str:
    """Wrap an already-rendered body in its Luma-style layout."""

    kind = blast.message_type or ""
    event = blast.event
    if event is None or kind not in (INVITE, BLAST, REMINDER):
        return body
    if kind == INVITE:
        # Cover and event first, then the host's message, then Register again.
        card = event_card(event, button="Register", campaign=campaign, with_cover=True)
        url = tracked_event_url(event, campaign=campaign)
        closing = f"\n\n**[Register →]({url})**" if url else ""
        return f"{card}\n\n---\n\n{body}{closing}"
    card = event_card(event, button="View event", campaign=campaign, with_cover=False)
    return f"{body}\n\n---\n\n{card}"


def reminder_subject(event, offset: str) -> str:
    when = "tomorrow" if offset == Blast.ReminderOffset.DAY_BEFORE else "in 1 hour"
    return f"Reminder: {event.name} starts {when}"[:300]


def reminder_body(event, offset: str, note: str = "") -> str:
    """Fixed reminder text, like Luma's: the time, the place and a short staff note."""

    start = merge_tags.format_event_start(event.start_at, getattr(event, "timezone", ""))
    clock = start.split(", ", 1)[-1] if start else ""
    if offset == Blast.ReminderOffset.DAY_BEFORE:
        opening = f"{_md(event.name)} starts tomorrow at {clock}." if clock else f"{_md(event.name)} starts tomorrow."
    else:
        opening = f"{_md(event.name)} starts in 1 hour, at {clock}." if clock else f"{_md(event.name)} starts in 1 hour."
    lines = ["Hi {{ first_name }},", "", opening]
    note = " ".join(str(note or "").split())[:280]
    if note:
        lines += ["", _md(note)]
    lines += ["", "See you there."]
    return "\n".join(lines)
