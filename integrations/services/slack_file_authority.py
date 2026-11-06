"""Member-only file reads fenced through preview serialization and downloads."""

from contextlib import contextmanager, ExitStack
from urllib.error import URLError

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from slack_sdk.errors import SlackApiError, SlackRequestError

from community_chat.onboarding import require_community_access
from integrations.models import CommunityBridgeChannel, SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.slack_chat_catalog import conversation_kind, private_channels_enabled
from integrations.services.slack_directory_authority import cache_scope, configured_workspace, _validate_room
from integrations.services.message_sync.scheduler import BudgetDeferred


def _candidates(user):
    # Prefer the account's own authority. Other members may only serve files
    # shared into a public Community room, never Members or private rooms.
    grants = SlackDmMirrorGrant.objects.filter(status="active", revoked_at__isnull=True,
        user__is_active=True).select_related("user", "connection").order_by("pk")
    for own in (True, False):
        selected = grants.filter(user_id=user.pk) if own else grants.exclude(user_id=user.pk).filter(
            slack_workspace_id=configured_workspace())
        for grant in selected[:access.MAX_READER_USERS]:
            if "files:read" not in (grant.connection.scopes or []):
                continue
            if not own and (not access.public_reader_enabled(grant, allow_expired=True)
                            or not _public_rooms(grant, community_only=True).exists()):
                continue
            try:
                yield dm._capture_slack_grant_api_authority(grant)
            except dm.SlackDmMirrorError:
                continue


def _public_rooms(grant, *, community_only=False):
    from integrations.models import SlackChannelMembership

    joined = SlackChannelMembership.objects.filter(grant=grant, state="joined",
        slack_workspace_id=grant.slack_workspace_id).values("slack_channel_id")
    rooms = CommunityBridgeChannel.objects.filter(enabled=True, destination_platform="buzz",
        slack_workspace_id=grant.slack_workspace_id, slack_channel_id__in=joined,
        slack_visibility="public", slack_archived=False, slack_deleted=False,
        mirror_decision="mirror", hold_reason="")
    return rooms.filter(audience="community") if community_only else rooms


@contextmanager
def _fence(authority, user):
    with transaction.atomic():
        # Requester deactivation and source consent revocation use User first.
        users = list(get_user_model().objects.select_for_update().filter(
            pk__in={user.pk, authority.user_id}).order_by("pk"))
        if len(users) != len({user.pk, authority.user_id}) or not all(u.is_active for u in users):
            raise dm.SlackDmMirrorAuthorizationError("This account is unavailable.")
        grant, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"files:read"})
        grant.connection = connection
        if (not access.enabled() or not grant.consented_at
                or grant.consent_version not in dm.PRIVATE_CHANNEL_CONSENTS | {SlackDmMirrorGrant.CONSENT_VERSION}
                or connection.token_expires_at and connection.token_expires_at <= timezone.now()):
            raise dm.SlackDmMirrorAuthorizationError("Current Slack file consent is required.")
        if grant.user_id != user.pk:
            require_community_access(user)
            if not access.public_reader_enabled(grant, authority=authority):
                raise dm.SlackDmMirrorAuthorizationError("Current Slack file reader is required.")
        yield grant, connection


def _shares(data, *, public, workspace_id):
    fields = ("channels",) if public else ("ims", "groups")
    result = {value for field in fields if isinstance(data.get(field), list)
              for value in data[field] if isinstance(value, str)}
    shares = data.get("shares") or {}
    values = (shares.get("public" if public else "private") or {}) if isinstance(shares, dict) else {}
    if isinstance(values, dict):
        for channel, entries in values.items():
            if (isinstance(entries, list) and entries
                    and all(isinstance(entry, dict) and entry.get("team_id", workspace_id) == workspace_id for entry in entries)):
                result.add(channel)
            else:
                result.discard(channel)
    return result


def _source(grant, connection, authority, data, user):
    from community_chat.slack_file_previews import SlackFilePreviewError

    # Slack's documented file shape may omit top-level workspace fields. The
    # exact grant/token and validated shared conversation bind that case.
    if (any(data.get(key) and data[key] != grant.slack_workspace_id for key in ("team_id", "user_team", "source_team"))
            or data.get("is_tombstoned") or data.get("mode") == "tombstone"):
        raise SlackFilePreviewError("The Slack file is not available in this workspace.")
    public_ids = _shares(data, public=True, workspace_id=grant.slack_workspace_id)
    if access.public_reader_enabled(grant, authority=authority):
        for candidate in _public_rooms(grant, community_only=grant.user_id != user.pk).filter(
                slack_channel_id__in=public_ids).order_by("pk"):
            membership = access._membership(grant, candidate.slack_channel_id, locked=True)
            if access._membership_state(grant, membership) != "joined":
                continue
            room = CommunityBridgeChannel.objects.select_for_update().get(pk=candidate.pk)
            _validate_room(grant, room)
            if room.audience == "community":
                require_community_access(user)
            elif room.audience != "members" or grant.user_id != user.pk:
                continue
            raw = dm._invoke_slack_with_locked_connection(authority, connection,
                "conversations_info", channel=room.slack_channel_id).get("channel") or {}
            if not _safe_source(raw, room.slack_channel_id) or raw.get("is_private") or not raw.get("is_channel") or raw.get("is_member") is not True:
                continue
            return lambda: _validate_room(grant, room)
    if grant.user_id == user.pk:
        for conversation in SlackDmMirrorConversation.objects.select_for_update().filter(
                grant=grant, slack_workspace_id=grant.slack_workspace_id,
                slack_conversation_id__in=_shares(data, public=False, workspace_id=grant.slack_workspace_id), status="live").order_by("pk"):
            kind = conversation_kind(conversation)
            required = {"im": "im:read", "mpim": "mpim:read", "private_channel": "groups:read"}[kind]
            if required not in authority.scopes or kind == "private_channel" and not private_channels_enabled(grant):
                continue
            raw = dm._invoke_slack_with_locked_connection(authority, connection,
                "conversations_info", channel=conversation.slack_conversation_id).get("channel") or {}
            if not _safe_source(raw, conversation.slack_conversation_id):
                continue
            valid = (kind == "im" and raw.get("is_im") is True or
                     kind == "mpim" and raw.get("is_mpim") is True and raw.get("is_member") is True or
                     kind == "private_channel" and raw.get("is_private") is True and not raw.get("is_mpim") and raw.get("is_member") is True)
            if valid:
                return lambda: None
    raise SlackFilePreviewError("The Slack file is not shared in an available MLAI Chat conversation.")


def _safe_source(raw, channel):
    return raw.get("id") == channel and not any(raw.get(key) for key in (
        "is_archived", "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared"))


def _record_rejected_token(authority, error):
    if isinstance(error, SlackApiError) and dm._is_slack_auth_error(error):
        grant = SlackDmMirrorGrant.objects.filter(pk=authority.grant_id).first()
        if grant is not None:
            access.record_reader_failure(grant, error, channel_id="", authority=authority, purpose="info")


@contextmanager
def authorized_file(file_id, *, user, load):
    """Require fresh file-share proof even when image bytes are already cached."""
    from community_chat.slack_file_previews import SlackFilePreviewDeferred, SlackFilePreviewError

    if user is None or not getattr(user, "is_authenticated", False) or not access.enabled():
        raise SlackFilePreviewError("Current Slack file access is required.")
    for authority in _candidates(user):
        stack = ExitStack()
        try:
            grant, connection = stack.enter_context(_fence(authority, user))
            scope = cache_scope(authority)
            data = load(file_id, cache_scope=scope, fresh=True,
                read=lambda **kwargs: dm._invoke_slack_with_locked_connection(authority, connection, "files_info", **kwargs))
            validate = _source(grant, connection, authority, data, user)
        except (BudgetDeferred, dm.SlackDmMirrorRateLimited) as error:
            stack.close()
            raise SlackFilePreviewDeferred(getattr(error, "retry_after", 2), reason="shared_budget") from error
        except SlackFilePreviewDeferred:
            stack.close()
            raise  # Another token must never bypass a shared provider budget.
        except (TimeoutError, ConnectionError, URLError, SlackRequestError) as error:
            stack.close()
            raise SlackFilePreviewDeferred(15, reason="transport") from error
        except SlackApiError as error:
            stack.close()
            code = str(error.response.get("error") or "")
            if code in {"ratelimited", "internal_error", "service_unavailable", "request_timeout"}:
                retry = (getattr(error.response, "headers", {}) or {}).get("Retry-After", "2")
                raise SlackFilePreviewDeferred(int(retry) if str(retry).isdigit() else 2,
                    reason="upstream_rate_limit" if code == "ratelimited" else "upstream_unavailable") from error
            _record_rejected_token(authority, error)
            continue
        except (SlackFilePreviewError, dm.SlackDmMirrorError) as error:
            stack.close()
            _record_rejected_token(authority, error.__cause__)
            continue
        except BaseException:
            stack.close()
            raise
        try:
            with stack:
                yield data, str(connection.access_token), scope
                validate()
        except dm.SlackDmMirrorError as error:
            raise SlackFilePreviewError("Current Slack file access is required.") from error
        return
    raise SlackFilePreviewError("The Slack file is not shared in an available MLAI Chat conversation.")
