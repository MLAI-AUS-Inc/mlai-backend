"""Resolve Slack permalinks to existing mirrors without disclosing message bodies.

The client must then read the event through the relay's current channel ACL.
This deliberately avoids bot-token reads and stale copies of private messages.
"""

import re

from integrations.models import (
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    ExternalServiceConnectionStatus,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorGrantStatus,
)
from integrations.services.community_bridge.formatting import slack_message_reference
from integrations.services.slack_dm_mirror import _private_destination_message_id


class SlackMessageReferenceError(ValueError):
    """A reference is not available to the current account."""


def resolve_slack_message_reference(raw_url, *, user):
    """Return a mapped event address; all content stays behind relay authorization."""
    identity = slack_message_reference(raw_url)
    if identity is None:
        return None
    if not getattr(user, "is_authenticated", False):
        raise SlackMessageReferenceError("Message unavailable.")
    channel, timestamp = identity
    common = dict(
        channel__enabled=True,
        source_deleted_at__isnull=True,
        destination_deleted_at__isnull=True,
    )
    incoming = CommunityBridgeMessageLink.objects.filter(
        **common,
        source_platform=CommunityBridgePlatform.SLACK,
        source_channel_id=channel,
        source_message_id=timestamp,
        destination_platform=CommunityBridgePlatform.BUZZ,
    ).first()
    if incoming:
        return _payload(
            raw_url, incoming.destination_channel_id, incoming.destination_message_id
        )
    outgoing = CommunityBridgeMessageLink.objects.filter(
        **common,
        source_platform=CommunityBridgePlatform.BUZZ,
        destination_platform=CommunityBridgePlatform.SLACK,
        destination_channel_id=channel,
        destination_message_id=timestamp,
    ).first()
    if outgoing:
        return _payload(raw_url, outgoing.source_channel_id, outgoing.source_message_id)
    conversations = SlackDmMirrorConversation.objects.filter(
        slack_conversation_id=channel,
        mlai_channel_id__isnull=False,
        status__in=(
            SlackDmMirrorConversationStatus.LIVE,
            SlackDmMirrorConversationStatus.PAUSED,
        ),
        grant__user=user,
        grant__revoked_at__isnull=True,
        grant__status__in=(
            SlackDmMirrorGrantStatus.ACTIVE,
            SlackDmMirrorGrantStatus.PAUSED,
        ),
        grant__connection__status__in=(
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        ),
    ).order_by("-updated_at")[:5]
    for conversation in conversations:
        event_id = _private_destination_message_id(conversation, timestamp)
        if event_id:
            return _payload(raw_url, str(conversation.mlai_channel_id), event_id)
    raise SlackMessageReferenceError("Message unavailable or you don’t have access.")


def _payload(href, channel_id, event_id):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", event_id or ""):
        raise SlackMessageReferenceError("Message unavailable.")
    return {
        "href": href,
        "title": "Thread",
        "description": "",
        "site_name": "MLAI Chat",
        "image_url": "",
        "thread": {"channel_id": channel_id, "message_id": event_id},
    }
