"""Delivery policy for Content Factory automation events (not account emails)."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _email_topics_enabled() -> bool:
    try:
        return bool(getattr(settings, "DAILY_RESEARCH_EMAIL_TOPICS_ENABLED", True))
    except ImproperlyConfigured:  # loaded outside Django (unit seams)
        return True


def should_deliver_automation_event(event_type: str, channel_type: str) -> bool:
    # Diagnostics remain in durable run state and operator logs. A generation
    # failure is never a useful customer notification between draft repairs.
    if event_type == "error":
        return False
    if channel_type == "email":
        if event_type == "topic_selection":
            # Founders can choose email for the daily research shortlist.
            return _email_topics_enabled()
        # B23: the one notice after several empty research nights.
        # B18: the one notice that research paused for points.
        return event_type in {"review_ready", "content_ready", "research_empty", "automation_paused_points"}
    # Preserve opted-in WhatsApp/Slack research topic selection.
    return True
