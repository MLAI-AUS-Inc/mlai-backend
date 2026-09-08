"""Public Luma event copy for Volunteer invitations; no attendee data or writes."""

from django.conf import settings
from django.core.cache import cache

from integrations.services.luma import (
    LumaAPIError,
    LumaAttendeeReportService,
    LumaConfigurationError,
)


def public_event(event_id):
    """Find an upcoming public event in the bounded, cached community calendar."""
    if not event_id:
        return None
    key = "community-chat:volunteer-public-events:v1"
    events = cache.get(key)
    if events is None:
        try:
            events = LumaAttendeeReportService(
                timeout=settings.LUMA_API_TIMEOUT_SECONDS,
            ).list_upcoming_events(limit=10)
        except (LumaAPIError, LumaConfigurationError):
            events = []
        cache.set(key, events, timeout=60)
    return next((event for event in events if event.get("id") == event_id), None)
