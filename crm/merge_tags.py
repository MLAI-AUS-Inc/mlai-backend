"""Merge tags for blast bodies, shared by email and SMS.

Bodies may use ``{{ first_name }}``, ``{{ display_name }}``, ``{{ event_name }}``,
``{{ event_start_local }}`` and ``{{ event_url }}``. WhatsApp templates use
Twilio's numbered slots instead, filled from the same sources by ``value_for``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SUPPORTED_TAGS = ("first_name", "display_name", "event_name", "event_start_local", "event_url")
DEFAULT_EVENT_TIMEZONE = "Australia/Melbourne"
FALLBACK_NAME = "there"

_TAG = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class UnsupportedMergeTag(ValueError):
    """A body names a merge tag MLAI can't fill; the API answers 400."""

    def __init__(self, tags=(), code="unsupported_merge_tag", status=400):
        self.code, self.status = code, status
        self.tags = tuple(tags)
        super().__init__(code)


def tags_in(text) -> list[str]:
    """Every ``{{ … }}`` name in the text, in order, with whitespace trimmed."""

    return [match.group(1).strip() for match in _TAG.finditer(str(text or ""))]


def validate(text) -> None:
    """Raise ``UnsupportedMergeTag`` for any tag outside ``SUPPORTED_TAGS``."""

    unsupported = sorted({tag[:64] for tag in tags_in(text) if tag not in SUPPORTED_TAGS})
    if unsupported:
        raise UnsupportedMergeTag(unsupported)


def render(text, contact, event) -> str:
    """Fill every merge tag for one person; unsupported tags raise."""

    validate(text)
    return _TAG.sub(lambda match: value_for(match.group(1).strip(), contact, event), str(text or ""))


def value_for(source: str, contact, event) -> str:
    """The text one merge-tag source gives for this contact and event."""

    if source == "first_name":
        return _first_name(contact)
    if source == "display_name":
        return _display_name(contact)
    if source == "event_name":
        return str(getattr(event, "name", "") or "").strip()
    if source == "event_start_local":
        if event is None:
            return ""
        return format_event_start(event.start_at, getattr(event, "timezone", ""))
    if source == "event_url":
        return str(getattr(event, "url", "") or "").strip()
    raise UnsupportedMergeTag([str(source)[:64]])


def format_event_start(start_at, timezone_name: str = "") -> str:
    """``Thu 30 Oct, 6:30pm`` in the event's timezone (Melbourne by default)."""

    if not isinstance(start_at, datetime):
        return ""
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=datetime_timezone.utc)
    local = start_at.astimezone(_zone(timezone_name))
    hour = local.hour % 12 or 12
    clock = f"{hour}" if local.minute == 0 else f"{hour}:{local.minute:02d}"
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}, {clock}{meridiem}"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "").strip() or DEFAULT_EVENT_TIMEZONE)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo(DEFAULT_EVENT_TIMEZONE)


def _first_name(contact) -> str:
    first = str(getattr(contact, "first_name", "") or "").strip()
    if first:
        return first
    display = str(getattr(contact, "display_name", "") or "").split()
    return display[0] if display else FALLBACK_NAME


def _display_name(contact) -> str:
    display = " ".join(str(getattr(contact, "display_name", "") or "").split())
    if display:
        return display
    full = " ".join(
        part
        for part in (
            str(getattr(contact, "first_name", "") or "").strip(),
            str(getattr(contact, "last_name", "") or "").strip(),
        )
        if part
    )
    return full or FALLBACK_NAME
