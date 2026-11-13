"""An owner's own OpenAI dot, reached through the owner's private Slack mirror.

OpenAI connects a dot to Slack as a bot user of its ChatGPT Slack app. MLAI
Chat already sends an owner's private messages to Slack with the owner's own
Slack authorization, so a conversation with the dot in MLAI Chat is the owner
talking to their dot in Slack. This module holds the one exception that makes
that work: the bot filter accepts the bot user the owner registered as their
dot, in that owner's one-to-one Slack conversation only.

Identity is always the workspace and Slack user IDs the owner registered, plus
a fresh ``users.info`` that must report a bot of an allowlisted Slack app. A
display name never selects a bot. The registration lives in the owner's Slack
connection metadata, so disconnecting Slack removes it.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

PERSONAL_DOT_KEY = "personal_dot_v1"
_WORKSPACE = re.compile(r"T[A-Z0-9]{1,40}")
_USER = re.compile(r"[UW][A-Z0-9]{1,40}")
_APP = re.compile(r"A[A-Z0-9]{1,40}")


class PersonalDotError(Exception):
    """A registration request that cannot be accepted, with a user-safe message."""

    def __init__(self, message: str, code: str = "personal_dot_invalid"):
        super().__init__(message)
        self.code = code


def personal_dot_enabled() -> bool:
    """Both the switch and at least one allowed Slack app are required."""
    return bool(
        getattr(settings, "COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED", False)
        and allowed_dot_app_ids()
    )


def allowed_dot_app_ids() -> frozenset[str]:
    raw = getattr(settings, "COMMUNITY_CHAT_DOT_SLACK_APP_IDS", "") or ""
    values = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    return frozenset(
        value for value in (str(item or "").strip() for item in values) if _APP.fullmatch(value)
    )


def _stored(connection) -> dict[str, Any]:
    value = (getattr(connection, "provider_metadata", None) or {}).get(PERSONAL_DOT_KEY)
    return value if isinstance(value, dict) else {}


def personal_dot_target(grant) -> tuple[str, str] | None:
    """Return ``(workspace_id, slack_user_id)`` of this owner's dot, or ``None``."""
    if grant is None or not personal_dot_enabled():
        return None
    stored = _stored(getattr(grant, "connection", None))
    workspace = str(stored.get("workspace_id") or "")
    user = str(stored.get("slack_user_id") or "")
    if (
        workspace
        and workspace == str(grant.slack_workspace_id or "")
        and str(stored.get("owner_slack_user_id") or "") == str(grant.slack_user_id or "")
        and _WORKSPACE.fullmatch(workspace)
        and _USER.fullmatch(user)
        and str(stored.get("app_id") or "") in allowed_dot_app_ids()
    ):
        return workspace, user
    return None


def personal_dot_summary(grant) -> dict[str, Any] | None:
    """The owner-facing description of their registered dot (no tokens)."""
    target = personal_dot_target(grant)
    if target is None:
        return None
    stored = _stored(grant.connection)
    return {
        "slack_workspace_id": target[0],
        "slack_user_id": target[1],
        "display_name": str(stored.get("display_name") or "")[:80],
        "avatar_url": str(stored.get("avatar_url") or "")[:2000],
        "registered_at": str(stored.get("registered_at") or ""),
    }


def _is_roo(workspace_id: str, slack_user_id: str) -> bool:
    from integrations.services.slack_roo import public_roo_target

    return public_roo_target() == (workspace_id, slack_user_id)


def is_dot_bot_profile(user: dict[str, Any], *, workspace_id: str) -> bool:
    """Validate a fresh ``users.info`` result as a dot of an allowed Slack app."""
    if not isinstance(user, dict):
        return False
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    slack_user_id = str(user.get("id") or "")
    return bool(
        personal_dot_enabled()
        and _USER.fullmatch(slack_user_id)
        and str(user.get("team_id") or user.get("team") or "") == workspace_id
        and user.get("is_bot") is True
        and not user.get("deleted")
        and not user.get("is_stranger")
        and str(profile.get("api_app_id") or "") in allowed_dot_app_ids()
        and not _is_roo(workspace_id, slack_user_id)
    )


def is_personal_dot_user(user: dict[str, Any], *, grant) -> bool:
    """True only for this owner's registered dot, revalidated from Slack."""
    target = personal_dot_target(grant)
    return bool(
        target
        and target == (str(grant.slack_workspace_id or ""), str((user or {}).get("id") or ""))
        and is_dot_bot_profile(user, workspace_id=target[0])
    )


def is_personal_dot_reply(message: dict[str, Any], *, grant, conversation_id: str) -> bool:
    """Accept a message authored by this owner's dot in a one-to-one conversation.

    Callers must still enforce the exact owner/dot participant set.
    """
    target = personal_dot_target(grant)
    return bool(
        target
        and target == (str(grant.slack_workspace_id or ""), str((message or {}).get("user") or ""))
        and re.fullmatch(r"D[A-Z0-9]+", str(conversation_id or ""))
    )


def workspace_personal_dot_author(workspace_id: str, slack_user_id: str) -> bool:
    """Whether any connected owner in this workspace registered this bot user.

    Live Slack events are normalised before the owning mirror is known. This
    only lets the event through the bot filter; per-owner routing then requires
    the conversation's own grant to have registered the same dot.
    """
    if not (
        personal_dot_enabled()
        and _WORKSPACE.fullmatch(str(workspace_id or ""))
        and _USER.fullmatch(str(slack_user_id or ""))
    ):
        return False
    from integrations.models import (
        ExternalServiceConnectionStatus,
        ExternalServiceProvider,
        SlackDmMirrorGrant,
        SlackDmMirrorGrantStatus,
    )

    candidates = SlackDmMirrorGrant.objects.select_related("connection").filter(
        slack_workspace_id=workspace_id,
        status=SlackDmMirrorGrantStatus.ACTIVE,
        revoked_at__isnull=True,
        connection__provider=ExternalServiceProvider.SLACK,
        connection__status__in=(
            ExternalServiceConnectionStatus.CONNECTED,
            ExternalServiceConnectionStatus.SYNCING,
        ),
    )
    return any(
        personal_dot_target(grant) == (workspace_id, slack_user_id)
        for grant in candidates.iterator()
    )


def register_personal_dot(grant, *, slack_user_id: str) -> dict[str, Any]:
    """Validate ``slack_user_id`` with the owner's Slack token and store it."""
    from integrations.services import slack_dm_mirror as mirror

    if not personal_dot_enabled():
        raise PersonalDotError("Connecting a dot through Slack is not available yet.", "personal_dot_disabled")
    slack_user_id = str(slack_user_id or "").strip()
    if not _USER.fullmatch(slack_user_id):
        raise PersonalDotError("Use your dot's Slack member ID, which starts with U.")
    if slack_user_id == grant.slack_user_id:
        raise PersonalDotError("Choose your dot, not yourself.")
    if _is_roo(grant.slack_workspace_id, slack_user_id):
        raise PersonalDotError("Roo is already available. Choose your own dot.")
    authority = mirror._capture_slack_grant_api_authority(grant)
    response = mirror._call_slack_with_grant_authority(
        authority,
        "users_info",
        required_scopes={"users:read"},
        user=slack_user_id,
    )
    user = response.get("user") if isinstance(response.get("user"), dict) else {}
    if str(user.get("id") or "") != slack_user_id or not is_dot_bot_profile(
        user, workspace_id=grant.slack_workspace_id
    ):
        raise PersonalDotError(
            "That Slack member is not an OpenAI dot in this workspace. Add your dot to Slack from its ChatGPT profile first.",
            "personal_dot_not_a_dot",
        )
    profile = mirror._profile_from_slack_user(user)
    record = {
        "workspace_id": grant.slack_workspace_id,
        "owner_slack_user_id": grant.slack_user_id,
        "slack_user_id": slack_user_id,
        "app_id": str((user.get("profile") or {}).get("api_app_id") or ""),
        "display_name": str(profile.get("display_name") or user.get("name") or "")[:80],
        "avatar_url": str(profile.get("avatar_url") or "")[:2000],
        "registered_at": timezone.now().isoformat(),
    }
    _write(grant, record)
    grant.refresh_from_db()
    return personal_dot_summary(grant) or {}


def clear_personal_dot(grant) -> None:
    _write(grant, None)


def _write(grant, record: dict[str, Any] | None) -> None:
    from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant, SlackDmMirrorGrantStatus

    with transaction.atomic():
        locked = (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(pk=grant.pk, status=SlackDmMirrorGrantStatus.ACTIVE, revoked_at__isnull=True)
            .first()
        )
        if locked is None:
            raise PersonalDotError("Connect or resume Slack first.", "personal_dot_no_slack")
        connection = ExternalServiceConnection.objects.select_for_update().get(pk=locked.connection_id)
        if (record is not None) and (
            str(record["workspace_id"]) != str(locked.slack_workspace_id)
            or str(record["owner_slack_user_id"]) != str(locked.slack_user_id)
        ):
            raise PersonalDotError("Slack changed while connecting your dot. Try again.", "personal_dot_conflict")
        metadata = dict(connection.provider_metadata or {})
        if record is None:
            metadata.pop(PERSONAL_DOT_KEY, None)
        else:
            metadata[PERSONAL_DOT_KEY] = record
        connection.provider_metadata = metadata
        connection.save(update_fields=("provider_metadata", "updated_at"))
