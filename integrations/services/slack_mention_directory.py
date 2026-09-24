"""Community directory reads, independent of private-message import consent."""

import uuid

from django.core.cache import cache
from slack_sdk.errors import SlackApiError

from community_chat.onboarding import require_community_access
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgePlatform,
    SlackDmMirrorConversation,
)
from integrations.services import slack_dm_mirror as mirror
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.slack_mentions import _search_directory, search_mentions
from integrations.services.slack_workspace_users import cache_key, configured_scope


def search_workspace_mentions(user, *, channel_id, query="", cursor="", limit=50):
    """Expose names and avatars to members, without granting private Slack reads.

    Private channel membership still uses the owner's current import authority.
    Paused imports can search the community directory but reveal no membership.
    Invitations and DM creation retain their separate owner-consent checks.
    """
    require_community_access(user)
    try:
        uuid.UUID(str(channel_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise mirror.SlackDmMirrorError("Choose a valid MLAI Chat channel.") from exc
    private = (
        SlackDmMirrorConversation.objects.select_related("grant__connection")
        .filter(
            grant__user=user,
            mlai_channel_id=channel_id,
        )
        .first()
    )
    if (
        private
        and private.status == "live"
        and private.grant.status == "active"
        and private.grant.revoked_at is None
    ):
        return search_mentions(
            private.grant,
            channel_id=channel_id,
            query=query,
            cursor=cursor,
            limit=limit,
        )

    configured = configured_scope()
    if configured is None:
        raise mirror.SlackDmMirrorError(
            "The community Slack directory is not configured."
        )
    workspace, scope = configured

    def scoped_key(category, value):
        return cache_key(scope, category, value)

    client = SlackBridgeClient.get_client()
    verified_key = scoped_key("workspace", "")
    public = CommunityBridgeChannel.objects.filter(
        enabled=True,
        destination_platform=CommunityBridgePlatform.BUZZ,
        destination_channel_id=channel_id,
        slack_workspace_id=workspace,
    ).first()
    membership_unknown = bool(private)

    def read(method, **kwargs):
        nonlocal membership_unknown
        try:
            if not cache.get(verified_key):
                identity = client.auth_test()
                if identity.get("team_id") != workspace:
                    raise mirror.SlackDmMirrorAuthorizationError(
                        "The Slack directory workspace does not match this community."
                    )
                cache.set(verified_key, True, timeout=600)
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            # Directory access does not depend on the bot joining every channel.
            if method == "conversations_members" and exc.response.get("error") in {
                "channel_not_found",
                "not_in_channel",
                "missing_scope",
            }:
                membership_unknown = True
                return {"members": [], "response_metadata": {"next_cursor": ""}}
            raise

    result = _search_directory(
        workspace=workspace,
        channel=public.slack_channel_id if public else "",
        private=None,
        query=query,
        cursor=cursor,
        limit=limit,
        read=read,
        cache_key=scoped_key,
        validate=lambda: None,
        membership_unknown=membership_unknown,
    )
    if membership_unknown:
        if public:
            cache.delete(scoped_key("members", public.slack_channel_id))
        for entry in result["users"]:
            entry["is_member"] = None
            entry["native_only"] = False
    return result
