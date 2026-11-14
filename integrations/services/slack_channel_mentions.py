"""Channel-scoped Roo mention targets carried by signed private chat messages."""

import re

from integrations.services.slack_roo import public_roo_target, is_public_roo_user


_ID = re.compile(r"[UW][A-Z0-9]+")


def _mask_code(text):
    """Keep offsets while excluding fenced/indented Markdown and code spans."""
    chars = list(text)
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", content)
        masked = bool(fence) or bool(re.match(r"^(?: {4}|\t)", content))
        if fence:
            closing = re.match(r"^ {0,3}(`+|~+)[ \t]*$", content)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= fence[1]:
                fence = None
        elif opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            masked = True
        if masked:
            chars[offset : offset + len(content)] = " " * len(content)
        offset += len(line)
    masked = "".join(chars)
    # Backtick spans use matching delimiter lengths, including multiline spans.
    for match in re.finditer(r"(?<![`\\])(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", masked):
        chars[match.start() : match.end()] = " " * len(match[0])
    return "".join(chars)


def roo_channel_targets(conversation):
    """Advertise configured AI bots (Roo, agent bots) already in this owner's private channel."""
    from integrations.services.slack_chat_catalog import (
        conversation_kind,
        conversation_metadata,
    )

    from integrations.services.slack_agent_bots import configured_bots

    workspace = getattr(conversation, "slack_workspace_id", "")
    participants = getattr(conversation, "participant_slack_ids", None) or []
    # Public Roo first, then configured personal agent bots (for example an
    # OpenAI dot) that are already members of this owner's private channel.
    candidates = []
    target = public_roo_target()
    if target and target[0] == workspace:
        candidates.append((target[1], "Roo"))
    candidates.extend(
        (bot.bot_user_id, bot.name) for bot in configured_bots()
        if bot.workspace_id == workspace
    )
    if (
        not any(slack_user_id in participants for slack_user_id, _ in candidates)
        or conversation_kind(conversation) != "private_channel"
        or conversation_metadata(conversation).get("source_archived")
        or conversation.grant.slack_user_id not in participants
    ):
        return []
    result = []
    for slack_user_id, fallback_name in candidates:
        if slack_user_id not in participants or any(
            item["slack_user_id"] == slack_user_id for item in result
        ):
            continue
        profile = (conversation.participant_profiles or {}).get(slack_user_id) or {}
        result.append({
            "slack_user_id": slack_user_id,
            "display_name": str(profile.get("display_name") or fallback_name)[:80],
            "avatar_url": str(profile.get("avatar_url") or "")[:2000],
            "is_bot": True,
        })
    return result


def render_outgoing_roo_mentions(text, tags, conversation):
    """Translate explicit signed targets, leaving code and ordinary text intact.

    The display label is presentation only. Identity and channel access are
    determined by the configured Slack ID and revalidated again before delivery.
    """
    allowed = {item["slack_user_id"] for item in roo_channel_targets(conversation)}
    return render_slack_mentions(text, tags, allowed=allowed)


def render_slack_mentions(text, tags, *, allowed=None):
    """Render explicit identities; callers validate workspace authority before delivery."""
    mentions = [
        tag for tag in tags if isinstance(tag, list) and tag[:1] == ["slack-mention"]
    ]
    if len(mentions) > 20:
        raise ValueError("Too many Slack mentions.")
    used = set()
    for tag in mentions:
        if (
            len(tag) != 3
            or not all(isinstance(value, str) for value in tag)
            or not _ID.fullmatch(tag[1])
            or (allowed is not None and tag[1] not in allowed)
            or not 1 <= len(tag[2]) <= 80
            or any(ord(char) < 32 or char in "`<>@" for char in tag[2])
        ):
            raise ValueError("Roo is not available in this Slack channel.")
        pattern = re.compile(
            r"(^|\s|\(|[*_]{1,3}|\|\|)(@"
            + re.escape(tag[2])
            + r")(?=\|\||[\s,;.!?:)\]}*_]|$)",
            re.IGNORECASE,
        )
        for match in reversed(list(pattern.finditer(_mask_code(text)))):
            used.add(tag[1])
            start, end = match.span(2)
            text = text[:start] + f"<@{tag[1]}>" + text[end:]
    return text, sorted(used)


def validate_roo_channel_access(conversation, channel, members, bot):
    """Check fresh Slack membership and bot identity before a mention is sent."""
    target = public_roo_target()
    return bool(
        target
        and target[0] == conversation.slack_workspace_id
        and channel.get("id") == conversation.slack_conversation_id
        and channel.get("is_private")
        and not channel.get("is_mpim")
        and not channel.get("is_archived")
        and not any(
            channel.get(flag)
            for flag in ("is_ext_shared", "is_shared", "is_org_shared")
        )
        and {conversation.grant.slack_user_id, target[1]}.issubset(members)
        and is_public_roo_user(bot, workspace_id=conversation.slack_workspace_id)
    )
