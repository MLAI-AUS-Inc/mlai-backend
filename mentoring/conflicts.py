"""Calendar protection independent of booking approval mode."""
from datetime import datetime, timedelta, timezone as utc
from zoneinfo import ZoneInfo

from django.utils import timezone
from .calendar_models import MentorCalendarConnection
from .calendars.errors import CalendarError


def fingerprint(profile):
    """Configuration fence, read without provider calls while holding user locks."""
    return (profile.booking_revision, list(MentorCalendarConnection.objects.filter(
        user_id=profile.user_id).order_by('pk').values(
        'id', 'revision', 'status', 'conflict_calendar_ids', 'booking_calendar_id')))


def read(profile, start, end):
    """An empty result is free only after every selected source succeeds."""
    from .calendars import service
    from . import google
    rows = list(MentorCalendarConnection.objects.filter(user_id=profile.user_id))
    if rows:
        # History prevents disconnect from reopening availability via legacy fallback.
        if not any(c.conflict_calendar_ids for c in rows):
            raise CalendarError('calendar_required')
        if any(c.booking_calendar_id and c.booking_calendar_id not in c.conflict_calendar_ids for c in rows):
            raise CalendarError('invalid_selection')
        return service.fetch_busy(profile, start, end)
    email = getattr(profile, 'busy_calendar_email', '')
    if not email or not google.is_enabled():
        raise CalendarError('calendar_not_configured')
    return google.busy_times(email, start, end, fresh=True)


def window(profile, first_day, last_day):
    """Cover mentor-local dates plus frozen/saved buffers at either edge."""
    tz = ZoneInfo(profile.timezone)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(utc.utc)
    end = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(utc.utc)
    return (start - timedelta(minutes=profile.buffer_before_minutes),
            end + timedelta(minutes=profile.buffer_after_minutes))


def preflight(profile, start, end):
    """Keep the earliest check time and config fence with the verified intervals."""
    config = fingerprint(profile)
    checked_at = timezone.now()
    busy = read(profile, start, end)
    return {'config': config, 'checked_at': checked_at, 'busy': busy}


def validate(profile, check):
    """Reject stale external evidence before any admission/payment side effect."""
    from .service import MentoringError
    if fingerprint(profile) != check['config']:
        raise MentoringError('settings_changed', 'Calendar settings changed. Please check again.', 409)
    if not 0 <= (timezone.now() - check['checked_at']).total_seconds() <= 10:
        raise MentoringError('calendar_unavailable', 'Please check the calendar again.', 503)
