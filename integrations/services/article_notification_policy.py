"""Delivery policy for Content Factory automation events (not account emails)."""


def should_deliver_automation_event(event_type: str, channel_type: str) -> bool:
    # Diagnostics remain in durable run state and operator logs. A generation
    # failure is never a useful customer notification between draft repairs.
    if event_type == "error":
        return False
    if channel_type == "email":
        return event_type in {"review_ready", "content_ready"}
    # Preserve opted-in WhatsApp/Slack research topic selection.
    return True
