"""Explain public Slack reaction consent before the client publishes an event."""

from django.conf import settings
from django.db.models import Q
from integrations.models import (
    CommunityBridgeDelivery,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
)


def reaction_requires_ai_consent(event_id):
    """Return only a permission requirement, never message or channel contents."""
    if not getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True):
        return False
    if (
        not isinstance(event_id, str)
        or len(event_id) != 64
        or any(c not in "0123456789abcdef" for c in event_id)
    ):
        return False
    buzz = CommunityBridgePlatform.BUZZ
    return (
        CommunityBridgeMessageLink.objects.filter(
            channel__enabled=True, channel__destination_platform=buzz
        )
        .filter(
            Q(source_platform=buzz, source_message_id=event_id)
            | Q(destination_platform=buzz, destination_message_id=event_id)
        )
        .exists()
        or CommunityBridgeDelivery.objects.filter(
            channel__enabled=True,
            channel__destination_platform=buzz,
            source_platform=buzz,
            source_message_id=event_id,
        ).exists()
    )
