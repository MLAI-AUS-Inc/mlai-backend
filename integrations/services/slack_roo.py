"""The first-party Public Roo exception to the private mirror's bot filter."""

import re
from urllib.parse import urlparse

from django.conf import settings


def public_roo_target():
    """Return the operator-configured MLAI Slack identity, never a name match."""
    workspace = str(
        getattr(settings, "COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID", "") or ""
    ).strip()
    user = str(getattr(settings, "COMMUNITY_CHAT_ROO_SLACK_USER_ID", "") or "").strip()
    if (
        urlparse(settings.COMMUNITY_CHAT_RELAY_URL).hostname == "chat.mlai.au"
        and re.fullmatch(r"T[A-Z0-9]+", workspace)
        and re.fullmatch(r"[UW][A-Z0-9]+", user)
    ):
        return workspace, user
    return None


def is_public_roo_user(user, *, workspace_id):
    """Validate Slack's fetched bot profile before opening its one-to-one DM."""
    target = public_roo_target()
    return bool(
        target
        and target == (workspace_id, str(user.get("id") or ""))
        and str(user.get("team_id") or user.get("team") or "") == workspace_id
        and user.get("is_bot") is True
        and not user.get("deleted")
        and not user.get("is_stranger")
    )


def is_public_roo_reply(
    message, *, workspace_id, conversation_id, allow_channels=False
):
    """Accept only Roo-authored replies in a verified Slack direct conversation.

    The caller must still enforce the owner grant and exact participant boundary.
    Private channel callers must additionally verify the conversation type and
    refresh Slack membership before routing. Other bots remain excluded.
    """
    target = public_roo_target()
    return bool(
        target
        and target == (workspace_id, str(message.get("user") or ""))
        and re.fullmatch(
            r"[DCG][A-Z0-9]+" if allow_channels else r"D[A-Z0-9]+",
            str(conversation_id or ""),
        )
    )


def is_accepted_agent_reply(message, *, workspace_id, conversation_id, allow_channels=False):
    """Public Roo or an owner-configured Slack agent bot (``slack_agent_bots``).

    Both keep the caller's owner-grant and participant-boundary checks.
    """
    from integrations.services.slack_agent_bots import is_agent_bot_reply

    return is_public_roo_reply(
        message, workspace_id=workspace_id, conversation_id=conversation_id,
        allow_channels=allow_channels,
    ) or is_agent_bot_reply(
        message, workspace_id=workspace_id, conversation_id=conversation_id,
        allow_channels=allow_channels,
    )


def ai_recipient_user_ids(workspace_id):
    """Every AI bot member ID whose presence in a conversation requires AI consent."""
    from integrations.services.slack_agent_bots import workspace_bot_user_ids

    target = public_roo_target()
    ids = set(workspace_bot_user_ids(workspace_id))
    if target and target[0] == workspace_id:
        ids.add(target[1])
    return ids
