"""Resolve reward-only bridge references from stored public delivery provenance."""

import re

from django.conf import settings

from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink
from .access import VolunteerError, channels


def imported_reward_root(source_id, channel_id):
    """A signed bridge tag alone cannot identify an original author or channel."""
    if (not re.fullmatch(r"[0-9a-f]{64}", source_id or "")
            or channel_id not in set(channels().values())):
        raise VolunteerError("source_unavailable", 403)
    link = CommunityBridgeMessageLink.objects.select_related("channel").filter(
        source_platform="slack", destination_platform="buzz",
        destination_channel_id=channel_id, destination_message_id=source_id,
        source_parent_message_id="", destination_parent_message_id="",
    ).first()
    if link is None:
        raise VolunteerError("source_unavailable", 409)
    mapping = link.channel
    if (not mapping.enabled or mapping.audience != "community"
            or mapping.slack_visibility != "public" or mapping.slack_archived
            or mapping.slack_deleted or mapping.hold_reason
            or mapping.mirror_decision != "mirror"
            or mapping.slack_workspace_id != settings.COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID
            or mapping.destination_platform != "buzz"
            or mapping.destination_channel_id != channel_id
            or mapping.slack_channel_id != link.source_channel_id
            or link.source_deleted_at or link.destination_deleted_at
            or not re.fullmatch(r"[UW][A-Z0-9]+", link.source_author_id)
            or not re.fullmatch(r"[0-9]{10,}\.[0-9]+", link.source_message_id)):
        raise VolunteerError("source_unavailable", 403)
    if not CommunityBridgeDelivery.objects.filter(
        channel_id=link.channel_id, source_platform="slack", target_platform="buzz",
        source_channel_id=link.source_channel_id, source_message_id=link.source_message_id,
        target_channel_id=channel_id, delivery_type="create", status="completed",
        completed_at__isnull=False,
    ).exists():
        raise VolunteerError("source_unavailable", 409)
    return link


def root_owned_by(link, user, *, current_only=False):
    """Compare canonical aliases; a bridge bot never becomes the post author."""
    from integrations.models import CommunityBridgeIdentityLink
    aliases = CommunityBridgeIdentityLink.objects.filter(
        user=user, slack_workspace_id=link.channel.slack_workspace_id,
        slack_user_id=link.source_author_id,
    )
    if current_only:
        aliases = aliases.filter(revoked_at__isnull=True)
    return bool(user.slack_id == link.source_author_id or aliases.exists())
