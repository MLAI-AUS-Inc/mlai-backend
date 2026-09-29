"""Channel audience announcements for verified Chat-to-Slack deliveries.

Slack apps need explicit <!channel> syntax; plain @channel is only display text.
Keep parsing independent of Django so the policy can be regression tested without
constructing a database or running migrations.
"""

import html
import re


_LITERAL = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\]]*\])*\](?:\([^)]*\)|\[[^\]]*\])|https?://[^\s<]+|<!(?:channel|here|everyone)\|[^>]*>"
)


def _mask_code(text):
    """Mask Markdown fences, indented code and equal-length backtick spans."""
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
            chars[offset:offset + len(content)] = " " * len(content)
        offset += len(line)
    for item in re.finditer(r"(?<![`\\])(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)", "".join(chars)):
        chars[item.start():item.end()] = " " * len(item[0])
    return "".join(chars)

_MENTION = re.compile(r"(?<![\w@\\/])@(channel|here|everyone)(?!\w)|(?<!\\)<!(channel|here|everyone)>")


class BroadcastPermissionDenied(RuntimeError):
    """A queued broadcast no longer has permission to notify Slack members."""

    permanent = True


def _matches(text):
    masked = _mask_code(text)
    literals = [(item.start(), item.end()) for item in _LITERAL.finditer(masked)]
    references = {
        " ".join(item[1].split()).casefold()
        for item in re.finditer(r"(?m)^ {0,3}\[([^\]]+)\]:", masked)
    }
    literals.extend(
        (item.start(), item.end())
        for item in re.finditer(r"!?\[([^\]]+)\]", masked)
        if " ".join(item[1].split()).casefold() in references
    )
    return [
        item for item in _MENTION.finditer(masked)
        if not any(start <= item.start() < end for start, end in literals)
    ]


def has_broadcast_mentions(text):
    """Recognize the three audience mentions, excluding literal/code content."""
    return bool(_matches(str(text or "")))


def render_broadcast_mentions(text, *, role, channel, is_thread=False):
    """Validate Slack's default member policy and encode the channel audience.

    Administrators may use all three mentions in any channel. Their @everyone
    outside general becomes <!channel>, preserving that channel's audience.
    Threads remain display text, matching Slack's no-broadcast-in-threads rule.
    """
    text = str(text or "")
    matches = _matches(text)
    mentions = {item.group(1) or item.group(2) for item in matches}
    is_admin = role in {"admin", "owner"}
    is_general = channel.get("is_general") is True
    if not is_thread and not is_admin:
        if "everyone" in mentions and not is_general:
            raise BroadcastPermissionDenied("Members can use @everyone only in #general; use @channel here")
        if "everyone" in mentions and role == "guest":
            raise BroadcastPermissionDenied("Guests cannot use @everyone")
        if mentions & {"channel", "here"}:
            count = channel.get("num_members")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError("Slack channel member count unavailable")
            if count >= 10_000:
                raise BroadcastPermissionDenied("Only admins can notify channels with 10,000 or more members")
    # Slack does not understand Markdown-link or backslash escaping. Escape
    # every inactive raw special mention before adding authorized wire tokens.
    pieces = []
    cursor = 0
    for item in matches:
        pieces.append(_escape_inactive_mentions(text[cursor:item.start()]))
        name = item.group(1) or item.group(2)
        if is_thread:
            replacement = "@" + name
        else:
            target = "channel" if name == "everyone" and not is_general else name
            replacement = f"<!{target}>"
        pieces.append(replacement)
        cursor = item.end()
    pieces.append(_escape_inactive_mentions(text[cursor:]))
    return "".join(pieces)


def _escape_inactive_mentions(text):
    return re.sub(r"<!(?:channel|here|everyone)(?:\|[^>]*)?>", lambda match: html.escape(match[0], quote=False), text)


def prepare_slack_broadcasts(delivery, text):
    """Recheck a Chat sender's live account role before a bot broadcasts for them.

    Never accept an admin flag or channel facts from signed client metadata.
    The mapping is server-owned and conversations.info supplies Slack's actual
    general channel and member count. Custom workspace policies are not exposed
    by this bot API; this mirrors Slack's documented default permissions.
    """
    if delivery.get("source_platform") != "buzz":
        return text
    if not has_broadcast_mentions(text):
        return render_broadcast_mentions(text, role="member", channel={}, is_thread=True)
    # Edits do not cause new broadcasts and replies must never notify a channel.
    if delivery.get("source_parent_message_id") or delivery.get("delivery_type") != "create":
        return render_broadcast_mentions(text, role="member", channel={}, is_thread=True)

    from community_chat.permissions import device_chat_role
    from .identity import verified_identity_for_buzz
    from .slack import SlackBridgeClient

    public_key = str((delivery.get("payload") or {}).get("source_author_id") or "")
    role = device_chat_role(public_key)
    channel_id = str(delivery.get("target_channel_id") or "")
    response = SlackBridgeClient.get_client().conversations_info(
        channel=channel_id, include_num_members=True,
    )
    channel = response.get("channel") or {}
    if not response.get("ok") or channel.get("id") != channel_id:
        raise RuntimeError("Slack channel permissions unavailable")
    if role not in {"admin", "owner"} and any(
        (item.group(1) or item.group(2)) == "everyone" for item in _matches(text)
    ):
        identity = verified_identity_for_buzz(
            slack_workspace_id=str((delivery.get("channel") or {}).get("slack_workspace_id") or ""),
            buzz_pubkey=public_key,
        )
        if identity and identity.get("slack_user_id"):
            response = SlackBridgeClient.get_client().users_info(user=identity["slack_user_id"])
            user = response.get("user") or {}
            if not response.get("ok") or user.get("id") != identity["slack_user_id"]:
                raise RuntimeError("Slack member permissions unavailable")
            if user.get("is_restricted") or user.get("is_ultra_restricted"):
                role = "guest"
    return render_broadcast_mentions(text, role=role, channel=channel)


def prepare_private_slack_broadcasts(delivery, client):
    """Encode private channel callouts using the already-authorized owner's token.

    DMs, group DMs, replies and edits stay silent. Slack applies any custom
    workspace restrictions to this user token in addition to the default policy.
    The caller must hold its existing owner/device/grant authorization boundary.
    """
    from community_chat.permissions import device_chat_role
    from integrations.services.slack_chat_catalog import conversation_kind

    text = str(delivery.encrypted_text or "")
    metadata = delivery.metadata or {}
    if (
        delivery.operation != "create"
        or metadata.get("source_parent_message_id")
        or metadata.get("original_source_parent_message_id")
        or conversation_kind(delivery.conversation) != "private_channel"
        or not has_broadcast_mentions(text)
    ):
        return render_broadcast_mentions(text, role="member", channel={}, is_thread=True)
    channel_id = delivery.conversation.slack_conversation_id
    response = client.conversations_info(channel=channel_id, include_num_members=True)
    channel = response.get("channel") or {}
    if not response.get("ok") or channel.get("id") != channel_id:
        raise RuntimeError("Slack channel permissions unavailable")
    role = device_chat_role(str(delivery.source_author_id or ""))
    return render_broadcast_mentions(text, role=role, channel=channel)
