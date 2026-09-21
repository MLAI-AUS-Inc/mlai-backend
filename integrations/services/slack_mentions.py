"""Owner-authorized Slack mention directory and explicit channel invitations.

Directory names are cached without email addresses. Canonical account links
come from the existing verified identity service, never from matching names.
"""

import hashlib
import re
import uuid

from django.core.cache import cache
from django.db import transaction

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgePlatform,
    SlackDmMirrorConversation,
)
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_roo import public_roo_target
from integrations.services import slack_dm_mirror as mirror


def eligible_user(user, workspace):
    """Include people and apps, but never deleted or foreign workspace users."""
    return bool(
        re.fullmatch(r"[UW][A-Z0-9]+", str(user.get("id") or ""))
        and user.get("id") != "USLACKBOT"
        and not user.get("deleted")
        and not user.get("is_stranger")
        and str(user.get("team_id") or user.get("team") or workspace) == workspace
    )


def channel_for_grant(grant, channel_id, *, allow_native=False):
    """Resolve only public bridge channels or this owner's live private mirror."""
    try:
        uuid.UUID(str(channel_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise mirror.SlackDmMirrorError("Choose a valid MLAI Chat channel.") from exc
    private = SlackDmMirrorConversation.objects.filter(
        grant=grant,
        mlai_channel_id=channel_id,
        status="live",
    ).first()
    if private:
        return private.slack_conversation_id, private
    public = CommunityBridgeChannel.objects.filter(
        enabled=True,
        destination_platform=CommunityBridgePlatform.BUZZ,
        destination_channel_id=channel_id,
        slack_workspace_id=grant.slack_workspace_id,
    ).first()
    if public:
        return public.slack_channel_id, None
    if allow_native:
        return "", None
    raise mirror.SlackDmMirrorAuthorizationError(
        "This channel is not connected to your Slack workspace."
    )


def _key(authority, category, value):
    material = f"{authority.grant_id}:{authority.consent_generation}:{authority.oauth_generation}:{category}:{value}"
    return "slack-mentions-v1:" + hashlib.sha256(material.encode()).hexdigest()


def _read(authority, method, **kwargs):
    return mirror._call_slack_with_grant_authority(
        authority,
        method,
        required_scopes={"users:read"},
        **kwargs,
    )


def _members(authority, channel):
    key = _key(authority, "members", channel)
    state = cache.get(key) or {"ids": [], "cursor": "", "complete": False}
    if not state["complete"]:
        try:
            response = _read(
                authority,
                "conversations_members",
                channel=channel,
                cursor=state["cursor"],
                limit=200,
            )
        except BudgetDeferred:
            return None
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if cursor and cursor == state["cursor"]:
            raise mirror.SlackDmMirrorError("Slack member pagination made no progress.")
        state = {
            "ids": sorted(set(state["ids"]) | set(response.get("members") or [])),
            "cursor": cursor,
            "complete": not cursor,
        }
        cache.set(key, state, timeout=60)
    return set(state["ids"]) if state["complete"] else None


def search_mentions(grant, *, channel_id, query="", cursor="", limit=50):
    """Return one resumable directory page, with truthful channel membership."""
    channel, private = channel_for_grant(grant, channel_id, allow_native=True)
    authority = mirror._capture_slack_grant_api_authority(grant)
    query = str(query or "").strip().casefold()
    if len(query) > 100:
        raise mirror.SlackDmMirrorError(
            "Slack user search is limited to 100 characters."
        )
    slack_cursor, offset = mirror._decode_directory_cursor(cursor)
    members = (
        set(private.participant_slack_ids)
        if private
        else _members(authority, channel) if channel else set()
    )
    key = _key(authority, "users", slack_cursor)
    page = cache.get(key)
    retry_after = 0
    if page is None:
        try:
            response = _read(authority, "users_list", limit=200, cursor=slack_cursor)
            # Strip private Slack profile fields before placing anything in cache.
            page = {
                "users": [
                    {
                        **mirror._serialize_slack_user(user),
                        "is_bot": bool(user.get("is_bot") or user.get("is_app_user")),
                        "search": " ".join(
                            str(value or "")
                            for value in (
                                user.get("name"),
                                user.get("real_name"),
                                (user.get("profile") or {}).get("display_name"),
                                (user.get("profile") or {}).get("real_name"),
                            )
                        ).casefold(),
                    }
                    for user in response.get("members") or []
                    if isinstance(user, dict)
                    and eligible_user(user, grant.slack_workspace_id)
                ],
                "next": str(
                    (response.get("response_metadata") or {}).get("next_cursor") or ""
                ),
            }
            cache.set(key, page, timeout=600)
        except BudgetDeferred as exc:
            retry_after = max(1, int(exc.retry_after))
    if page is None:
        users = []
        next_cursor = mirror._encode_directory_cursor(slack_cursor, offset)
    else:
        matches = [user for user in page["users"] if query in user["search"]]
        limit = max(1, min(int(limit), 50))
        users = matches[offset : offset + limit]
        next_cursor = (
            mirror._encode_directory_cursor(slack_cursor, offset + limit)
            if offset + limit < len(matches)
            else (
                mirror._encode_directory_cursor(page["next"], 0) if page["next"] else ""
            )
        )
    # Roo is available immediately, including in Roo DMs and empty searches.
    target = public_roo_target()
    if (
        not cursor
        and target
        and target[0] == grant.slack_workspace_id
        and query in "roo"
        and not any(user["slack_user_id"] == target[1] for user in users)
    ):
        profile = (
            (private.participant_profiles or {}).get(target[1], {}) if private else {}
        )
        users.insert(
            0,
            {
                "slack_user_id": target[1],
                "display_name": "Roo",
                "avatar_url": profile.get("avatar_url", ""),
                "is_bot": True,
            },
        )
    from integrations.services.community_bridge.identity import (
        verified_identity_for_slack,
    )

    result = []
    for user in users:
        identity = verified_identity_for_slack(
            slack_workspace_id=grant.slack_workspace_id,
            slack_user_id=user["slack_user_id"],
        )
        result.append(
            {key: value for key, value in user.items() if key != "search"}
            | {
                "is_member": (
                    None if members is None else user["slack_user_id"] in members
                ),
                "native_only": not bool(channel),
                "profile_id": (identity or {}).get("user_profile_id"),
                "pubkey": (identity or {}).get("buzz_pubkey"),
            }
        )
    with transaction.atomic():
        mirror._lock_slack_grant_api_authority(
            authority, required_scopes={"users:read"}
        )
    return {
        "users": result,
        "next_cursor": next_cursor,
        "retry_after_seconds": retry_after,
        "membership_pending": members is None,
    }


def invite_mentions(grant, *, channel_id, user_ids):
    """Invite only after an explicit owner action, using that owner's authority."""
    if (
        not isinstance(user_ids, list)
        or not 1 <= len(user_ids) <= 20
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[UW][A-Z0-9]+", value)
            for value in user_ids
        )
    ):
        raise mirror.SlackDmMirrorError("Choose one to twenty people to invite.")
    channel, private = channel_for_grant(grant, channel_id)
    if private and mirror.conversation_kind(private) in {"im", "mpim"}:
        raise mirror.SlackDmMirrorError(
            "Start a new group DM to include more people in this conversation."
        )
    authority = mirror._capture_slack_grant_api_authority(grant)
    info = _read(authority, "conversations_info", channel=channel).get("channel") or {}
    if (
        info.get("id") != channel
        or not info.get("is_member")
        or info.get("is_archived")
        or any(
            info.get(flag) for flag in ("is_shared", "is_ext_shared", "is_org_shared")
        )
    ):
        raise mirror.SlackDmMirrorAuthorizationError(
            "Join this channel in Slack before inviting people."
        )
    # Slack enforces the caller's role, invite scopes and workspace restrictions.
    _read(
        authority,
        "conversations_invite",
        channel=channel,
        users=",".join(sorted(set(user_ids))),
    )
    cache.delete(_key(authority, "members", channel))
    return {"invited": sorted(set(user_ids))}


def validate_mention_users(client, user_ids, workspace, *, scope):
    """Resume multi-person validation across provider admission deferrals."""
    for user_id in user_ids:
        key = (
            "slack-mention-user-v1:"
            + hashlib.sha256(f"{scope}:{workspace}:{user_id}".encode()).hexdigest()
        )
        user = cache.get(key)
        if not isinstance(user, dict):
            user = client.users_info(user=user_id).get("user") or {}
            if user.get("id") != user_id or not eligible_user(user, workspace):
                raise mirror.SlackDmMirrorAuthorizationError(
                    "A mentioned Slack account is no longer available."
                )
            cache.set(key, {"id": user_id, "team_id": workspace}, timeout=30)
        if user.get("id") != user_id or not eligible_user(user, workspace):
            raise mirror.SlackDmMirrorAuthorizationError(
                "A mentioned Slack account is no longer available."
            )
