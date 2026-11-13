"""Operator-registered personal agent bots reached through the owner's Slack mirror.

A personal agent bot (for example an OpenAI dot installed through OpenAI's own
Slack app) is reached exactly as it is in Slack: the owner's MLAI Chat messages
are posted with the owner's own Slack authorization, the bot answers in Slack,
and its replies return through the private mirror. This module generalises the
Public Roo exception (``slack_roo``) to bots listed in ``SLACK_AGENT_BOTS``.

Identity is always the exact configured workspace and Slack member ID, checked
against a fresh Slack profile where one is available. Names never select a bot.
Everything is off unless ``SLACK_AGENT_BOTS_ENABLED`` is true.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from django.conf import settings

_WORKSPACE = re.compile(r"T[A-Z0-9]{1,30}")
_USER = re.compile(r"[UW][A-Z0-9]{1,30}")
_BOT = re.compile(r"B[A-Z0-9]{1,30}")
_APP = re.compile(r"A[A-Z0-9]{1,30}")
_DIRECT = re.compile(r"D[A-Z0-9]+")
_DIRECT_OR_CHANNEL = re.compile(r"[DCG][A-Z0-9]+")

KINDS = frozenset({"openai_dot"})


@dataclass(frozen=True)
class SlackAgentBot:
    """One configured bot identity owned by one MLAI account."""

    workspace_id: str
    bot_user_id: str
    owner_user_id: int
    name: str
    kind: str = "openai_dot"
    bot_id: str = ""
    app_id: str = ""


def enabled() -> bool:
    return bool(getattr(settings, "SLACK_AGENT_BOTS_ENABLED", False))


def _raw_entries():
    raw = getattr(settings, "SLACK_AGENT_BOTS", None) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def configured_bots() -> tuple[SlackAgentBot, ...]:
    """Return valid configured bots; malformed entries are ignored, never guessed."""
    if not enabled():
        return ()
    bots: list[SlackAgentBot] = []
    seen: set[tuple[str, str]] = set()
    for entry in _raw_entries():
        if not isinstance(entry, dict):
            continue
        workspace = str(entry.get("workspace_id") or "").strip()
        user = str(entry.get("bot_user_id") or "").strip()
        bot_id = str(entry.get("bot_id") or "").strip()
        app_id = str(entry.get("app_id") or "").strip()
        name = str(entry.get("name") or "").strip()[:80]
        kind = str(entry.get("kind") or "openai_dot").strip()
        owner = entry.get("owner_user_id")
        if (
            not _WORKSPACE.fullmatch(workspace)
            or not _USER.fullmatch(user)
            or (bot_id and not _BOT.fullmatch(bot_id))
            or (app_id and not _APP.fullmatch(app_id))
            or kind not in KINDS
            or isinstance(owner, bool)
            or not isinstance(owner, int)
            or owner <= 0
            or not name
            or any(ord(char) < 32 or char in "`<>@" for char in name)
            or (workspace, user) in seen
        ):
            continue
        seen.add((workspace, user))
        bots.append(SlackAgentBot(workspace, user, owner, name, kind, bot_id, app_id))
    return tuple(bots)


def bot_for_user(workspace_id: str, slack_user_id: str) -> SlackAgentBot | None:
    for bot in configured_bots():
        if bot.workspace_id == workspace_id and bot.bot_user_id == slack_user_id:
            return bot
    return None


def bot_for_bot_id(workspace_id: str, bot_id: str) -> SlackAgentBot | None:
    if not bot_id:
        return None
    for bot in configured_bots():
        if bot.workspace_id == workspace_id and bot.bot_id and bot.bot_id == bot_id:
            return bot
    return None


def owner_bots(owner_user_id: int, workspace_id: str = "") -> list[SlackAgentBot]:
    return [
        bot for bot in configured_bots()
        if bot.owner_user_id == owner_user_id
        and (not workspace_id or bot.workspace_id == workspace_id)
    ]


def workspace_bot_user_ids(workspace_id: str) -> set[str]:
    return {bot.bot_user_id for bot in configured_bots() if bot.workspace_id == workspace_id}


def with_agent_author(message: dict, *, workspace_id: str) -> dict:
    """Attribute a user-less reply to its configured bot member ID.

    Some Slack apps post with only ``bot_id``. Only an exactly configured
    ``bot_id`` is mapped; every other user-less bot message stays user-less and
    is therefore still filtered by the mirror.
    """
    if not isinstance(message, dict) or message.get("user"):
        return message
    bot = bot_for_bot_id(workspace_id, str(message.get("bot_id") or "").strip())
    if bot is None:
        return message
    return {**message, "user": bot.bot_user_id}


def is_agent_bot_user(user: dict, *, workspace_id: str, owner_user_id: int) -> bool:
    """Validate a fresh Slack profile before the owner opens a DM with their bot."""
    if not isinstance(user, dict):
        return False
    bot = bot_for_user(workspace_id, str(user.get("id") or ""))
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    return bool(
        bot is not None
        and bot.owner_user_id == owner_user_id
        and str(user.get("team_id") or user.get("team") or "") == workspace_id
        and user.get("is_bot") is True
        and not user.get("deleted")
        and not user.get("is_stranger")
        and (not bot.app_id or str(profile.get("api_app_id") or "") == bot.app_id)
        and (not bot.bot_id or str(profile.get("bot_id") or "") in {"", bot.bot_id})
    )


def is_agent_bot_reply(
    message: dict, *, workspace_id: str, conversation_id: str, allow_channels: bool = False
) -> bool:
    """Accept only a configured bot's message in a DM or, if allowed, a channel.

    Callers must still enforce the owner grant and exact participant boundary,
    exactly as for Public Roo. Other bots remain excluded.
    """
    if not isinstance(message, dict):
        return False
    message = with_agent_author(message, workspace_id=workspace_id)
    bot = bot_for_user(workspace_id, str(message.get("user") or ""))
    if bot is None:
        return False
    if bot.bot_id and message.get("bot_id") and str(message.get("bot_id")) != bot.bot_id:
        return False
    pattern = _DIRECT_OR_CHANNEL if allow_channels else _DIRECT
    return bool(pattern.fullmatch(str(conversation_id or "")))


def owner_payload(owner_user_id: int, workspace_id: str = "") -> list[dict]:
    """Owner-visible description of their own configured bots (no other owners)."""
    return [
        {
            "slack_user_id": bot.bot_user_id,
            "slack_workspace_id": bot.workspace_id,
            "name": bot.name,
            "kind": bot.kind,
        }
        for bot in owner_bots(owner_user_id, workspace_id)
    ]
