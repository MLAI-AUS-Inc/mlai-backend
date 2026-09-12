import html
import hashlib
import re
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from integrations.models import CommunityBridgePlatform
from ..slack_emoji import emoji_to_slack_reaction, slack_reaction_to_emoji  # noqa: F401


SLACK_USER_MENTION_RE = re.compile(r"<@([^>|]+)(?:\|([^>]*))?>")
SLACK_CHANNEL_MENTION_RE = re.compile(r"<#([^>|]+)\|?([^>]*)>")
SLACK_SPECIAL_MENTION_RE = re.compile(r"<!([^>|]+)\|?([^>]*)>")
SLACK_LINK_RE = re.compile(r"<((?:https?|mailto):[^>|]+)\|?([^>]*)>")

DISCORD_USER_MENTION_RE = re.compile(r"<@!?\d+>")
DISCORD_CHANNEL_MENTION_RE = re.compile(r"<#\d+>")
DISCORD_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
DISCORD_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")


def approved_slack_avatar_url(value: str | None) -> str:
    """Return an adapter-compatible optional avatar, or omit unusable metadata."""
    value = str(value or "").strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    try:
        if len(value.encode("utf-8")) > 2048:
            return ""
        parsed = urlsplit(value)
        # Evaluate the port so malformed authorities cannot reach the adapter.
        _ = parsed.port
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"avatars.slack-edge.com", "secure.gravatar.com"}
            and parsed.username in {None, ""}
            and parsed.password is None
        ):
            return value
    except (ValueError, UnicodeError):
        pass
    return ""


def reaction_object_id(*, message_id: str, reaction: str, author_id: str) -> str:
    material = "\0".join(
        [
            str(message_id or "").strip(),
            str(reaction or "").strip(),
            str(author_id or "").strip(),
        ]
    ).encode("utf-8")
    return "reaction:" + hashlib.sha256(material).hexdigest()


def sanitize_slack_text(
    value: str,
    *,
    user_name_resolver: Optional[Callable[[str], str]] = None,
    channel_name_resolver: Optional[Callable[[str], str]] = None,
    preserve_unresolved_mentions: bool = False,
) -> str:
    text = html.unescape(str(value or ""))
    text = SLACK_LINK_RE.sub(_replace_slack_link, text)
    text = SLACK_CHANNEL_MENTION_RE.sub(
        lambda match: _replace_slack_channel(
            match,
            channel_name_resolver=channel_name_resolver,
        ),
        text,
    )
    text = SLACK_SPECIAL_MENTION_RE.sub(_replace_slack_special_mention, text)
    text = SLACK_USER_MENTION_RE.sub(
        lambda match: _replace_slack_user(
            match,
            user_name_resolver=user_name_resolver,
            preserve_unresolved_mentions=preserve_unresolved_mentions,
        ),
        text,
    )
    return _strip_trailing_whitespace(text)


def has_slack_entity_references(value: str) -> bool:
    text = str(value or "")
    return bool(
        SLACK_USER_MENTION_RE.search(text) or SLACK_CHANNEL_MENTION_RE.search(text)
    )


def sanitize_discord_text(value: str) -> str:
    text = str(value or "")
    text = DISCORD_USER_MENTION_RE.sub("@user", text)
    text = DISCORD_CHANNEL_MENTION_RE.sub("#channel", text)
    text = DISCORD_ROLE_MENTION_RE.sub("@role", text)
    text = DISCORD_EMOJI_RE.sub(r":\1:", text)
    return _strip_trailing_whitespace(text)


def normalize_slack_files(files: Iterable[dict]) -> list[dict]:
    attachments = []
    for item in files or []:
        if not isinstance(item, dict):
            continue
        url = (
            str(item.get("permalink") or "").strip()
            or str(item.get("url_private_download") or "").strip()
            or str(item.get("url_private") or "").strip()
            or str(item.get("permalink_public") or "").strip()
        )
        if not url:
            continue
        title = str(item.get("title") or item.get("name") or url).strip()
        attachments.append({"title": title, "url": url})
    return attachments


def normalize_discord_attachments(attachments: Iterable[object]) -> list[dict]:
    normalized = []
    for item in attachments or []:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("proxy_url") or "").strip()
            title = str(item.get("filename") or item.get("title") or url).strip()
        else:
            url = str(
                getattr(item, "url", "") or getattr(item, "proxy_url", "") or ""
            ).strip()
            title = str(getattr(item, "filename", "") or url).strip()
        if not url:
            continue
        normalized.append({"title": title, "url": url})
    return normalized


def build_mirrored_text(
    *,
    destination_platform: str,
    source_platform: str,
    author_display_name: str,
    body: str,
    attachments: Optional[Iterable[dict]] = None,
) -> str:
    author_name = str(author_display_name or "Unknown user").strip() or "Unknown user"
    source_label = {
        CommunityBridgePlatform.SLACK: "Slack",
        CommunityBridgePlatform.DISCORD: "Discord",
        CommunityBridgePlatform.BUZZ: "MLAI Chat",
    }.get(source_platform, "Community")
    normalized_body = _strip_trailing_whitespace(body)
    sections = [_format_author_line(destination_platform, author_name, source_label)]
    if normalized_body:
        sections.append(normalized_body)
    attachment_lines = _format_attachment_lines(destination_platform, attachments or [])
    if attachment_lines:
        sections.append(attachment_lines)
    return "\n\n".join(section for section in sections if section).strip()


def _format_author_line(
    destination_platform: str, author_name: str, source_label: str
) -> str:
    if destination_platform == CommunityBridgePlatform.SLACK:
        return f"*{author_name} ({source_label})*"
    return f"**{author_name} ({source_label})**"


def _format_attachment_lines(
    destination_platform: str, attachments: Iterable[dict]
) -> str:
    items = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        url = str(attachment.get("url") or "").strip()
        if not url:
            continue
        title = str(attachment.get("title") or url).strip()
        if destination_platform == CommunityBridgePlatform.SLACK:
            items.append(f"• <{url}|{title}>")
        else:
            items.append(f"- {title}: {url}")
    if not items:
        return ""
    return "Attachments:\n" + "\n".join(items)


def _replace_slack_link(match: re.Match) -> str:
    url = str(match.group(1) or "").strip()
    label = str(match.group(2) or "").strip()
    if not label or label == url:
        return url
    return f"{label} ({url})"


def _replace_slack_user(
    match: re.Match,
    *,
    user_name_resolver: Optional[Callable[[str], str]],
    preserve_unresolved_mentions: bool = False,
) -> str:
    user_id = str(match.group(1) or "").strip()
    label = _resolved_slack_label(user_id, user_name_resolver)
    if label:
        return f"@{label}"
    return match.group(0) if preserve_unresolved_mentions else "@user"


def _replace_slack_channel(
    match: re.Match,
    *,
    channel_name_resolver: Optional[Callable[[str], str]],
) -> str:
    channel_id = str(match.group(1) or "").strip()
    label = str(match.group(2) or "").strip()
    if not label:
        label = _resolved_slack_label(channel_id, channel_name_resolver)
    if label:
        return f"#{label}"
    return "#channel"


def _resolved_slack_label(
    entity_id: str,
    resolver: Optional[Callable[[str], str]],
) -> str:
    if not entity_id or resolver is None:
        return ""
    try:
        label = str(resolver(entity_id) or "").strip()
    except Exception:
        return ""
    if not label or label == entity_id:
        return ""
    return " ".join(label.splitlines()).strip()


def _replace_slack_special_mention(match: re.Match) -> str:
    token = str(match.group(2) or match.group(1) or "").strip()
    if token in {"here", "channel", "everyone"}:
        return f"@{token}"
    return "@mention"


def _strip_trailing_whitespace(value: str) -> str:
    lines = [line.rstrip() for line in str(value or "").splitlines()]
    return "\n".join(lines).strip()
