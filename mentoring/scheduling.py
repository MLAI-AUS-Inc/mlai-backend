"""Pure scheduling and settlement rules. No database access, easy to unit test."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

MAX_RANGE_DAYS = 31
MIN_LEAD_MINUTES = 60
MAX_ADVANCE_DAYS = 30


def protected_interval(starts_at, session_minutes, before_minutes=0, after_minutes=0):
    """Return the half-open UTC interval protected by a meeting's frozen buffers."""
    return (
        starts_at - timedelta(minutes=before_minutes),
        starts_at + timedelta(minutes=session_minutes + after_minutes),
    )


def generate_slots(*, rules, tz_name, session_minutes, first_day, last_day, busy, now,
                   away=(), buffer_before_minutes=0, buffer_after_minutes=0,
                   minimum_notice_minutes=MIN_LEAD_MINUTES,
                   booking_horizon_days=MAX_ADVANCE_DAYS,
                   start_increment_minutes=None):
    """Return UTC slots, checking full protected intervals against busy time.

    Legacy callers retain duration-sized increments from each window's start.
    Instant callers explicitly select midnight-aligned increments and buffers.
    UTC stepping preserves both folds of a repeated hour and skips nonexistent
    local times. Buffers must fit inside the offered weekly window.
    """
    tz = ZoneInfo(tz_name)
    length = timedelta(minutes=session_minutes)
    if length <= timedelta(0):
        # A zero or negative length would never advance the cursor below.
        return []
    before = timedelta(minutes=buffer_before_minutes)
    after = timedelta(minutes=buffer_after_minutes)
    earliest = now + timedelta(minutes=minimum_notice_minutes)
    latest = now + timedelta(days=booking_horizon_days)
    busy = list(busy)
    away = list(away)
    by_weekday = {}
    for weekday, start, end in rules:
        by_weekday.setdefault(weekday, []).append((start, end))
    slots = []
    day = first_day
    while day <= last_day:
        if any(a <= day <= b for a, b in away):
            day += timedelta(days=1)
            continue
        for start, end in sorted(by_weekday.get(day.weekday(), [])):
            window_start = datetime.combine(day, start, tzinfo=tz).astimezone(dt_timezone.utc)
            window_end = datetime.combine(day, end, tzinfo=tz).replace(fold=1).astimezone(dt_timezone.utc)
            cursor = window_start
            step = timedelta(minutes=1 if start_increment_minutes else session_minutes)
            while cursor + length <= window_end:
                local = cursor.astimezone(tz)
                local_end = (cursor + length).astimezone(tz)
                aligned = (
                    start_increment_minutes is None
                    or (local.hour * 60 + local.minute) % start_increment_minutes == 0
                )
                p_start, p_end = cursor - before, cursor + length + after
                if (
                    aligned and local.date() == day
                    and local.time().replace(tzinfo=None) >= start
                    and local_end.date() == day
                    and local_end.time().replace(tzinfo=None) <= end
                    and p_start >= window_start and p_end <= window_end
                    and cursor >= earliest and cursor <= latest
                    and not any(overlaps(p_start, p_end, b_start, b_end) for b_start, b_end in busy)
                ):
                    slots.append(cursor)
                cursor += step
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
