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


def is_public_roo_reply(message, *, workspace_id, conversation_id):
    """Accept only Roo-authored replies in a verified Slack direct conversation.

    The caller must still enforce the owner grant and exact participant boundary.
    Group/private channel bot messages keep their existing exclusion.
    """
    target = public_roo_target()
    return bool(
        target
        and target == (workspace_id, str(message.get("user") or ""))
        and re.fullmatch(r"D[A-Z0-9]+", str(conversation_id or ""))
    )
