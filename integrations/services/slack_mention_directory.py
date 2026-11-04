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
from integrations.services import slack_directory_authority as directory
from integrations.services.slack_mentions import _search_directory, search_mentions
from integrations.services.slack_workspace_users import cache_key, cached_workspace_snapshot


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

    workspace = directory.configured_workspace()
    public = CommunityBridgeChannel.objects.filter(
        enabled=True, destination_platform=CommunityBridgePlatform.BUZZ,
        destination_channel_id=channel_id, slack_workspace_id=workspace,
    ).first()
    member_room = public is not None and public.audience == "members"
    authority = directory.capture_reader(
        workspace, channel_id=public.slack_channel_id if public else "",
        user_id=user.pk if member_room else None, refresh_token=True,
    )
    membership_unknown = bool(private)
    source = public.slack_channel_id if public else ""
    if authority is None and not member_room:
        # Community names remain available without claiming channel membership
        # when no current joined reader can prove that particular channel.
        authority = directory.capture_reader(workspace, refresh_token=True)
        membership_unknown = bool(private or public)
        source = ""
    if authority is None:
        raise mirror.SlackDmMirrorAuthorizationError("A current Slack directory reader is required.")
    scope = directory.cache_scope(authority)

    def scoped_key(category, value):
        return cache_key(scope, category, value)

    with directory.fence(authority, channel_id=source,
                         room_id=channel_id if source else "", requester_id=user.pk) as (_, connection):
        def read(method, **kwargs):
            nonlocal membership_unknown
            try:
                return directory.read(authority, connection, method, **kwargs)
            except SlackApiError as exc:
                if method == "conversations_members" and exc.response.get("error") in {
                    "channel_not_found", "not_in_channel", "missing_scope",
                }:
                    membership_unknown = True
                    return {"members": [], "response_metadata": {"next_cursor": ""}}
                raise

        # Cached membership still needs current public-source proof. Otherwise
        # a private/Connect transition could disclose the prior member list.
        if source:
            try:
                info = mirror._invoke_slack_with_locked_connection(
                    authority, connection, "conversations_info", channel=source)
                raw = info.get("channel") or {}
                if (info.get("ok") is not True or not directory.access._public_channel_response(info, source)
                        or raw.get("is_archived") is True or raw.get("is_member") is not True):
                    raise mirror.SlackDmMirrorAuthorizationError("Current public channel access is required.")
            except SlackApiError as exc:
                if exc.response.get("error") not in {"channel_not_found", "not_in_channel", "missing_scope"}:
                    raise
                membership_unknown = True
        result = _search_directory(
            workspace=workspace, channel="" if membership_unknown else source, private=None,
            query=query, cursor=cursor, limit=limit, read=read,
            cache_key=scoped_key, validate=lambda: None,
            membership_unknown=membership_unknown,
            snapshot_reader=lambda version: cached_workspace_snapshot(workspace, version=version, authority=authority),
        )
        if membership_unknown:
            if public:
                cache.delete(scoped_key("members", public.slack_channel_id))
            for entry in result["users"]:
                entry["is_member"] = None
                entry["native_only"] = False
        return result
