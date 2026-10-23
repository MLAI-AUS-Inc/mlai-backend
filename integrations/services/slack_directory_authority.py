"""Current member authority for shared, sanitized workspace directory reads."""

from contextlib import contextmanager
import hashlib
import json

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.db import transaction

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeChannel, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as mirror, slack_member_access as access
from integrations.services.slack_chat_catalog import member_channel_room_id


def configured_workspace():
    """Use the deployment's workspace identity, never its bot credential."""
    workspace = str(getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "") or "").strip()
    return workspace if access.WORKSPACE_ID.fullmatch(workspace) else ""


def capture_reader(workspace, *, channel_id="", user_id=None, refresh_token=False):
    """Select without provider I/O; refresh only before taking a room lock."""
    if not access.enabled() or workspace != configured_workspace():
        return None
    grants = SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace, status="active", revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("user", "connection").order_by("pk")
    if user_id is not None:
        grants = grants.filter(user_id=user_id)
    for grant in grants:
        if (not access.public_reader_enabled(grant, allow_expired=refresh_token)
                or "users:read" not in (grant.connection.scopes or [])):
            continue
        if channel_id and access._membership_state(grant, access._membership(grant, channel_id)) != "joined":
            continue
        selected = mirror._capture_slack_grant_api_authority(grant, refresh_token=refresh_token)
        if not cache.get("slack-directory-reader-retry:" + cache_scope(selected)):
            return selected
    return None


def cache_scope(authority):
    """Bind cached metadata/cursors to exact read authority, without storing tokens."""
    material = [authority.grant_id, authority.user_id, authority.connection_id,
                authority.workspace_id, authority.slack_user_id,
                authority.consent_generation, authority.consent_version,
                authority.oauth_generation, authority.scopes, authority.access_token]
    return hashlib.sha256(json.dumps(material, default=str, separators=(",", ":")).encode()).hexdigest()


def _validate_room(grant, room):
    from integrations.services.community_bridge.member_channels import member_channel_in_scope

    if (room is None or not room.enabled or room.destination_platform != "buzz"
            or room.slack_visibility != "public" or room.slack_archived or room.slack_deleted
            or room.mirror_decision != "mirror" or room.hold_reason
            or not member_channel_in_scope(room.slack_workspace_id, room.slack_channel_id)):
        raise mirror.SlackDmMirrorAuthorizationError("The Slack channel is unavailable.")
    if room.audience == "members":
        keys = CommunityChatDevice.objects.filter(
            user_id=grant.user_id, status="verified", revoked_at__isnull=True,
        ).values_list("public_key", flat=True)
        if not any(member_channel_room_id(grant, key, room, catalog_kinds={"public_channel"}) for key in keys):
            raise mirror.SlackDmMirrorAuthorizationError("Current Slack channel access is required.")


@contextmanager
def _fence(authority, *, channel_id="", room_id="", requester_id=None):
    """Keep consent and optional source membership current through serialization."""
    if authority is None or not access.enabled():
        raise mirror.SlackDmMirrorAuthorizationError("A current Slack directory reader is required.")
    with transaction.atomic():
        grant, connection = mirror._lock_slack_grant_api_authority(
            authority, required_scopes=access.PUBLIC_READER_SCOPES | {"users:read"})
        grant.connection = connection
        if not access.public_reader_enabled(grant, authority=authority):
            raise mirror.SlackDmMirrorAuthorizationError("Slack directory consent is unavailable.")
        if channel_id and access._membership_state(grant, access._membership(grant, channel_id, locked=True)) != "joined":
            raise mirror.SlackDmMirrorAuthorizationError("Current Slack channel membership is required.")
        room = None
        if room_id:
            room = CommunityBridgeChannel.objects.select_for_update().filter(
                destination_platform="buzz", destination_channel_id=room_id,
                slack_workspace_id=authority.workspace_id, slack_channel_id=channel_id,
            ).first()
            _validate_room(grant, room)
            if room.audience == "members" and grant.user_id != requester_id:
                raise mirror.SlackDmMirrorAuthorizationError("This Slack channel is not available to this account.")
        yield grant, connection
        if room is not None:
            _validate_room(grant, room)


def read(authority, connection, method, **kwargs):
    """Read directory metadata inside its already-held authority transaction."""
    if method not in {"users_list", "conversations_members"}:
        raise mirror.SlackDmMirrorAuthorizationError("Unsupported directory operation.")
    return mirror._invoke_slack_with_locked_connection(authority, connection, method, **kwargs)


@contextmanager
def fence(authority, *, channel_id="", room_id="", requester_id=None):
    """Record provider failure only after releasing the read transaction."""
    started_at = timezone.now()
    try:
        with _fence(authority, channel_id=channel_id, room_id=room_id,
                    requester_id=requester_id) as current:
            yield current
    except mirror.SlackApiError as error:
        grant = SlackDmMirrorGrant.objects.filter(pk=authority.grant_id).first()
        if grant is not None:
            access.record_reader_failure(grant, error, channel_id=channel_id,
                authority=authority, purpose="info", observed_at=started_at)
        cache.set("slack-directory-reader-retry:" + cache_scope(authority), True, timeout=60)
        raise
