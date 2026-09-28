"""Convert Slack display content to the Markdown shared by Chat clients.

Slack's top-level text is a notification fallback, not the displayed message.
This module is independent of Django so conversion can be tested offline.
"""

import re
from html import unescape
from urllib.parse import quote, urlsplit


_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_LINK = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")


def escape_text(value):
    """Escape literal display text without giving it Markdown semantics."""
    return re.sub(r"([\\`*_\[\]<>~])", r"\\\1", unescape(str(value or "")))


def safe_url(value):
    """Allow ordinary web/mail links, never executable or credential URLs."""
    value = unescape(str(value or "")).strip()
    if not value or any(ord(c) < 32 or c.isspace() for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "mailto" and parsed.path:
            return quote(value, safe=":/@?=&%+;,#")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return ""
    except ValueError:
        return ""
    return quote(value, safe=":/@?=&%+;,#")


def markdown_link(label, url):
    """Produce a labelled link with a safely encoded destination."""
    destination = safe_url(url)
    return (
        f"[{escape_text(label or url)}]({destination})"
        if destination
        else escape_text(label)
    )


def slack_mrkdwn(value, *, resolve_entities=lambda text: text):
    """Translate Slack emphasis/links while leaving code and URL contents intact."""
    value = str(value or "").replace("\x00", "\ufffd")
    pieces = _CODE.split(value)
    code = _CODE.findall(value)
    rendered = []
    for index, piece in enumerate(pieces):
        links = []

        def stash(label, url):
            links.append(markdown_link(label, url))
            return f"\x00{len(links) - 1}\x00"

        # Decode entities after resolving real mentions: &lt;@U...&gt; is literal.
        piece = _LINK.sub(lambda m: stash(m[2] or m[1], m[1]), piece)
        piece = resolve_entities(piece).replace("&lt;", r"\<").replace("&gt;", r"\>")
        piece = unescape(piece)
        piece = re.sub(r"https?://[^\s<>]+", lambda m: stash(m[0], m[0]), piece)
        piece = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])", r"**\1**", piece)
        piece = re.sub(r"(?<![\w~])~(?=\S)([^~\n]*?\S)~(?![\w~])", r"~~\1~~", piece)
        piece = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m[1])], piece)
        rendered.append(piece)
        if index < len(code):
            rendered.append(unescape(code[index]))
    return "".join(rendered)


def slack_message_markdown(
    message, *, resolve_entities=lambda text: text, action_link=None
):
    """Prefer visible Block Kit content; use fallback text only when necessary."""

    def text_object(value):
        if not isinstance(value, dict):
            return ""
        text = value.get("text") or ""
        return (
            slack_mrkdwn(text, resolve_entities=resolve_entities)
            if value.get("type") == "mrkdwn"
            else escape_text(text)
        )

    def element(value):
        if not isinstance(value, dict):
            return ""
        kind = value.get("type")
        if kind == "text":
            result = escape_text(value.get("text"))
        elif kind == "link":
            result = markdown_link(
                value.get("text") or value.get("url"), value.get("url")
            )
        elif kind == "emoji":
            result = f":{value.get('name', '')}:"
        elif kind == "user":
            result = resolve_entities(f"<@{value.get('user_id', '')}>")
        elif kind == "channel":
            result = resolve_entities(f"<#{value.get('channel_id', '')}>")
        elif kind == "broadcast":
            result = resolve_entities(f"<!{value.get('range', '')}>")
        else:
            return ""
        style = value.get("style") or {}
        if style.get("code") and kind == "text":
            literal = str(value.get("text") or "")
            fence = "`" * (
                max((len(m[0]) for m in re.finditer(r"`+", literal)), default=0) + 1
            )
            return f"{fence} {literal} {fence}"
        for flag, marker in (("bold", "**"), ("italic", "_"), ("strike", "~~")):
            if style.get(flag):
                result = f"{marker}{result}{marker}"
        return result

    def rich(value):
        kind = value.get("type")
        children = value.get("elements") or []
        if kind == "rich_text_list":
            offset = value.get("offset") or 0
            offset = offset if isinstance(offset, int) else 0
            return "\n".join(
                (f"{offset + i + 1}. " if value.get("style") == "ordered" else "- ")
                + rich(child)
                for i, child in enumerate(children)
                if isinstance(child, dict)
            )
        content = "".join(element(child) for child in children)
        if kind == "rich_text_quote":
            return "\n".join("> " + line for line in content.splitlines())
        if kind == "rich_text_preformatted":
            literal = "".join(
                str(child.get("text") or "")
                for child in children
                if isinstance(child, dict)
            )
            fence = "`" * max(
                3, max((len(m[0]) + 1 for m in re.finditer(r"`+", literal)), default=3)
            )
            return f"{fence}\n{literal}\n{fence}"
        return content

    def button(value):
        if not isinstance(value, dict) or value.get("type") != "button":
            return ""
        label = (value.get("text") or {}).get("text") or "Open"
        if value.get("url"):
            return markdown_link(label, value["url"])
        if action_link:
            url = action_link(value)
            if url:
                return markdown_link(label, url)
        return escape_text(label) + " (open in Slack)"

    def blocks(values):
        sections = []
        for block in values if isinstance(values, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "header":
                content = text_object(block.get("text"))
                if content:
                    sections.append(f"**{content}**")
            elif kind == "section":
                parts = [text_object(block.get("text"))]
                parts.extend(text_object(field) for field in block.get("fields") or [])
                parts.append(button(block.get("accessory")))
                sections.extend(part for part in parts if part)
            elif kind == "context":
                sections.append(
                    "\n".join(
                        filter(
                            None, (text_object(v) for v in block.get("elements") or [])
                        )
                    )
                )
            elif kind == "divider":
                sections.append("---")
            elif kind == "rich_text":
                sections.extend(
                    rich(child)
                    for child in block.get("elements") or []
                    if isinstance(child, dict)
                )
            elif kind == "actions":
                sections.append(
                    "\n\n".join(
                        filter(None, (button(v) for v in block.get("elements") or []))
                    )
                )
            elif kind == "image":
                sections.append(
                    markdown_link(
                        block.get("alt_text") or "Image", block.get("image_url")
                    )
                )
        return "\n\n".join(section for section in sections if section.strip())

    body = blocks(message.get("blocks"))
    if not body:
        body = slack_mrkdwn(message.get("text"), resolve_entities=resolve_entities)
    attachments = []
    for attachment in message.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        content = blocks(attachment.get("blocks"))
        if not content:
            parts = [
                (
                    markdown_link(attachment.get("title"), attachment.get("title_link"))
                    if attachment.get("title_link")
                    else escape_text(attachment.get("title"))
                )
            ]
            parts.extend(
                slack_mrkdwn(attachment.get(field), resolve_entities=resolve_entities)
                for field in ("pretext", "text")
            )
            content = "\n\n".join(filter(None, parts)) or escape_text(
                attachment.get("fallback")
            )
        if content and content != body:
            attachments.append(content)
    return "\n\n".join(filter(None, [body, *attachments])).strip()
