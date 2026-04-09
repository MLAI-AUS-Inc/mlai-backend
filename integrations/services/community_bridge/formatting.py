import html
import re
from typing import Iterable, Optional

from integrations.models import CommunityBridgePlatform


SLACK_USER_MENTION_RE = re.compile(r"<@[^>]+>")
SLACK_CHANNEL_MENTION_RE = re.compile(r"<#([^>|]+)\|?([^>]*)>")
SLACK_SPECIAL_MENTION_RE = re.compile(r"<!([^>|]+)\|?([^>]*)>")
SLACK_LINK_RE = re.compile(r"<((?:https?|mailto):[^>|]+)\|?([^>]*)>")

DISCORD_USER_MENTION_RE = re.compile(r"<@!?\d+>")
DISCORD_CHANNEL_MENTION_RE = re.compile(r"<#\d+>")
DISCORD_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
DISCORD_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")


def sanitize_slack_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = SLACK_LINK_RE.sub(_replace_slack_link, text)
    text = SLACK_CHANNEL_MENTION_RE.sub(_replace_slack_channel, text)
    text = SLACK_SPECIAL_MENTION_RE.sub(_replace_slack_special_mention, text)
    text = SLACK_USER_MENTION_RE.sub("@user", text)
    return _strip_trailing_whitespace(text)


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
            url = str(getattr(item, "url", "") or getattr(item, "proxy_url", "") or "").strip()
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
    source_label = "Slack" if source_platform == CommunityBridgePlatform.SLACK else "Discord"
    normalized_body = _strip_trailing_whitespace(body)
    sections = [_format_author_line(destination_platform, author_name, source_label)]
    if normalized_body:
        sections.append(normalized_body)
    attachment_lines = _format_attachment_lines(destination_platform, attachments or [])
    if attachment_lines:
        sections.append(attachment_lines)
    return "\n\n".join(section for section in sections if section).strip()


def _format_author_line(destination_platform: str, author_name: str, source_label: str) -> str:
    if destination_platform == CommunityBridgePlatform.SLACK:
        return f"*{author_name} ({source_label})*"
    return f"**{author_name} ({source_label})**"


def _format_attachment_lines(destination_platform: str, attachments: Iterable[dict]) -> str:
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


def _replace_slack_channel(match: re.Match) -> str:
    label = str(match.group(2) or "").strip()
    if label:
        return f"#{label}"
    return "#channel"


def _replace_slack_special_mention(match: re.Match) -> str:
    token = str(match.group(2) or match.group(1) or "").strip()
    if token in {"here", "channel", "everyone"}:
        return f"@{token}"
    return "@mention"


def _strip_trailing_whitespace(value: str) -> str:
    lines = [line.rstrip() for line in str(value or "").splitlines()]
    return "\n".join(lines).strip()
