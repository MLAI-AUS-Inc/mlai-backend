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
    """Advertise only the configured Roo already in this owner's private channel."""
    from integrations.services.slack_chat_catalog import (
        conversation_kind,
        conversation_metadata,
    )

    target = public_roo_target()
    if (
        not target
        or target[0] != getattr(conversation, "slack_workspace_id", "")
        or conversation_kind(conversation) != "private_channel"
        or conversation_metadata(conversation).get("source_archived")
        or target[1] not in (getattr(conversation, "participant_slack_ids", None) or [])
        or conversation.grant.slack_user_id not in conversation.participant_slack_ids
    ):
        return []
    profile = (conversation.participant_profiles or {}).get(target[1]) or {}
    return [
        {
            "slack_user_id": target[1],
            "display_name": str(profile.get("display_name") or "Roo")[:80],
            "avatar_url": str(profile.get("avatar_url") or "")[:2000],
            "is_bot": True,
        }
    ]


def render_outgoing_roo_mentions(text, tags, conversation):
    """Translate explicit signed targets, leaving code and ordinary text intact.

    The display label is presentation only. Identity and channel access are
    determined by the configured Slack ID and revalidated again before delivery.
    """
    allowed = {item["slack_user_id"] for item in roo_channel_targets(conversation)}
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
            or tag[1] not in allowed
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
