"""Sanitized public display metadata from current member read authority."""

from django.core.cache import cache
from integrations.models import CommunityBridgeChannel
from integrations.services import slack_directory_authority as directory
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.message_sync.scheduler import BudgetDeferred
from .formatting import approved_slack_avatar_url


def profile(user_id, *, workspace_id=None):
    """Revalidate cached names/avatars; optional metadata may fall back to IDs."""
    user_id = str(user_id or "").strip()
    fallback = {"display_name": user_id or "Unknown user", "avatar_url": ""}
    workspace_id = workspace_id if workspace_id is not None else directory.configured_workspace()
    if not user_id:
        return fallback
    try:
        selected = directory.capture_reader(workspace_id, refresh_token=True)
        if selected is None:
            return fallback
        key = f"slack-member-display-v1:{directory.cache_scope(selected)}:{user_id}"
        with directory.fence(selected) as (_, connection):
            cached = cache.get(key)
            if isinstance(cached, dict):
                return cached
            response = dm._invoke_slack_with_locked_connection(selected, connection, "users_info", user=user_id)
            user = response.get("user") or {}
            if not response.get("ok") or user.get("id") != user_id or user.get("team_id") != workspace_id:
                return fallback
            raw = user.get("profile") or {}
            name = str(raw.get("display_name") or raw.get("real_name") or user.get("real_name") or user_id)
            result = {"display_name": " ".join(name.split())[:200], "avatar_url": ""}
            for field in ("image_192", "image_512", "image_72", "image_48", "image_original"):
                if image := approved_slack_avatar_url(raw.get(field)):
                    result["avatar_url"] = image
                    break
            cache.set(key, result, timeout=300)
            return result
    except (dm.SlackDmMirrorError, dm.SlackApiError, BudgetDeferred):
        return fallback


def channel_name(channel_id, *, workspace_id=None):
    """Resolve only admitted Community labels; never probe private mentions."""
    channel_id = str(channel_id or "").strip()
    workspace_id = workspace_id if workspace_id is not None else directory.configured_workspace()
    if not channel_id or workspace_id != directory.configured_workspace():
        return channel_id
    room = CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace_id, slack_channel_id=channel_id,
        audience="community", destination_platform="buzz", enabled=True, slack_visibility="public",
        mirror_decision="mirror", slack_archived=False, slack_deleted=False, hold_reason="").first()
    if room is None:
        return channel_id
    try:
        selected = directory.capture_reader(workspace_id, channel_id=channel_id, refresh_token=True)
        with directory.fence(selected, channel_id=channel_id, room_id=room.destination_channel_id) as (_, connection):
            response = dm._invoke_slack_with_locked_connection(selected, connection, "conversations_info", channel=channel_id)
            raw = response.get("channel") or {}
            if (not response.get("ok") or not access._public_channel_response(response, channel_id)
                    or raw.get("is_archived") or raw.get("is_member") is not True):
                return channel_id
            return " ".join(str(raw.get("name") or channel_id).split())[:200]
    except (dm.SlackDmMirrorError, dm.SlackApiError, BudgetDeferred):
        return channel_id


def prepare_legacy_text(delivery, text, mention_ids):
    """Validate legacy mention metadata before the worker takes delivery locks.

    Ordinary Chat messages use the member delivery engine. Retained Discord and
    co-working delivery paths also require current member authority for reads.
    The later send retains its separate sender, privacy and AI-consent fences.
    """
    from .broadcast_mentions import has_broadcast_mentions, prepare_slack_broadcasts
    from integrations.services.slack_mentions import eligible_user, validate_mention_users

    broadcasts = (delivery.get("source_platform") == "buzz"
                  and delivery.get("delivery_type") == "create"
                  and not delivery.get("source_parent_message_id")
                  and has_broadcast_mentions(text))
    if not mention_ids and not broadcasts:
        return prepare_slack_broadcasts(delivery, text)
    hint = delivery.get("channel") or {}
    workspace = str(hint.get("slack_workspace_id") or "")
    channel_id = str(delivery.get("target_channel_id") or "")
    selected = directory.capture_reader(workspace, channel_id=channel_id, refresh_token=True)
    with directory.fence(selected, channel_id=channel_id) as (_, connection):
        room = CommunityBridgeChannel.objects.select_for_update().filter(
            pk=delivery.get("channel_id"), slack_workspace_id=workspace,
            slack_channel_id=channel_id, enabled=True, audience="community",
            slack_visibility="public", mirror_decision="mirror", hold_reason="",
            slack_archived=False, slack_deleted=False,
        ).first()
        if room is None:
            raise dm.SlackDmMirrorAuthorizationError("The Slack channel is unavailable.")

        class Reader:
            def conversations_info(self, **kwargs):
                if kwargs.get("channel") != channel_id:
                    raise dm.SlackDmMirrorAuthorizationError("The Slack channel is unavailable.")
                response = dm._invoke_slack_with_locked_connection(
                    selected, connection, "conversations_info", **kwargs)
                raw = response.get("channel") or {}
                if (not response.get("ok") or not access._public_channel_response(response, channel_id)
                        or raw.get("is_archived") or raw.get("is_member") is not True):
                    raise dm.SlackDmMirrorAuthorizationError("The Slack channel is unavailable.")
                return response

            def users_info(self, **kwargs):
                response = dm._invoke_slack_with_locked_connection(selected, connection, "users_info", **kwargs)
                user = response.get("user") or {}
                if (not response.get("ok") or user.get("id") != kwargs.get("user")
                        or not eligible_user(user, workspace)):
                    raise dm.SlackDmMirrorAuthorizationError("The Slack account is unavailable.")
                return response

        client = Reader()
        # Fresh source facts precede even a mention cache hit.
        client.conversations_info(channel=channel_id)
        if mention_ids:
            validate_mention_users(client, mention_ids, workspace,
                                   scope="legacy-member:" + directory.cache_scope(selected))
        return prepare_slack_broadcasts(delivery, text, client=client)
