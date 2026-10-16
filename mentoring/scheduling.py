"""Pure scheduling and settlement rules. No database access, easy to unit test."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

MAX_RANGE_DAYS = 31
MIN_LEAD_MINUTES = 60
MAX_ADVANCE_DAYS = 30


def generate_slots(*, rules, tz_name, session_minutes, first_day, last_day, busy, now):
    """Return the free slot start datetimes (UTC, aware) for a mentor.

    rules: iterable of (weekday, start_time, end_time) in the mentor's timezone.
    busy: iterable of (starts_at, ends_at) aware datetimes that block the slot.
    A slot is kept only when it fits wholly inside a window, starts at least
    MIN_LEAD_MINUTES from now, is within MAX_ADVANCE_DAYS, and overlaps no busy
    interval. Wall-clock times that do not exist or repeat around a DST change
    are resolved by zoneinfo and then filtered by the window check.
    """
    tz = ZoneInfo(tz_name)
    length = timedelta(minutes=session_minutes)
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    latest = now + timedelta(days=MAX_ADVANCE_DAYS)
    busy = [(s, e) for s, e in busy]
    by_weekday = {}
    for weekday, start, end in rules:
        by_weekday.setdefault(weekday, []).append((start, end))
    slots = []
    day = first_day
    while day <= last_day:
        for start, end in sorted(by_weekday.get(day.weekday(), [])):
            cursor = datetime.combine(day, start, tzinfo=tz).astimezone(dt_timezone.utc)
            window_end = datetime.combine(day, end, tzinfo=tz).astimezone(dt_timezone.utc)
            while cursor + length <= window_end:
                slot_end = cursor + length
                if (
                    cursor >= earliest
                    and cursor <= latest
                    and not any(cursor < b_end and slot_end > b_start for b_start, b_end in busy)
                ):
                    slots.append(cursor)
                cursor = slot_end
        day += timedelta(days=1)
    return sorted(set(slots))


def validate_range(first_day, last_day):
    if last_day < first_day:
        return 'The end date is before the start date.'
    if (last_day - first_day).days >= MAX_RANGE_DAYS:
        return f'Ask for at most {MAX_RANGE_DAYS} days at a time.'
    return None


def settlement_action(*, status, now, starts_at, ends_at, expires_at, auto_complete_hours):
    """What a lazy read should do to a session: 'expire', 'complete' or None."""
    if status == 'requested' and now >= expires_at:
        return 'expire'
    if status == 'accepted' and now >= ends_at + timedelta(hours=auto_complete_hours):
        return 'complete'
    return None


def request_expiry(*, now, starts_at, ttl_hours):
    """Unanswered requests lapse after ttl_hours, or at the start time if sooner."""
    return min(now + timedelta(hours=ttl_hours), starts_at)


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and a_end > b_start
