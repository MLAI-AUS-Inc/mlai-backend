"""Workspace emoji presentation from official, currently authorized member APIs.

NIP-30 carries the source shortcode and image on each message/reaction. No
workspace-wide palette is published, so a private emoji use stays in its room.
"""
import re
from urllib.parse import urlsplit

from .slack_emoji import _emoji_maps, custom_shortcode, SLACK_CUSTOM_NAME_RE

TOKEN = re.compile(r":([a-z0-9_+\-]{1,100}(?:::skin-tone-[2-6]){0,2}):")
CODE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`|<https?://[^>]+>|https?://[^\s<>]+)")


def needs_catalog(text):
    """Plain text and known Unicode shortcodes do not need a provider request."""
    standard = _emoji_maps()[0]
    return any(match[1] not in standard
               for i, part in enumerate(CODE.split(str(text or ""))) if i % 2 == 0
               for match in TOKEN.finditer(part))


def safe_url(value):
    """Only public Slack emoji CDN URLs, never token-bearing or arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        u = urlsplit(value)
        if (u.scheme == "https" and u.hostname in {"emoji.slack-edge.com", "a.slack-edge.com"}
                and not (u.username or u.password or u.port or u.query or u.fragment)):
            return value
    except ValueError:
        pass
    return ""


def resolve_catalog(raw):
    """Resolve bounded alias chains, including aliases to standard Unicode."""
    if not isinstance(raw, dict) or len(raw) > 20000:
        return {}
    standard = _emoji_maps()[0]
    result = {}
    for name in raw:
        if not isinstance(name, str) or not SLACK_CUSTOM_NAME_RE.fullmatch(name):
            continue
        target, seen = name, set()
        for _ in range(32):
            if target in seen:
                break
            seen.add(target)
            value = raw.get(target)
            if isinstance(value, str) and value.startswith("alias:"):
                target = value[6:]
                if target not in raw and target in standard:
                    result[name] = {"unicode": standard[target]}
                    break
            elif image := safe_url(value):
                result[name] = {"shortcode": custom_shortcode(name), "url": image}
                break
            else:
                break
    return result


def load_catalog(workspace):
    """Read/cache emoji metadata before callers acquire delivery/privacy locks."""
    from django.core.cache import cache
    from . import slack_directory_authority as directory, slack_dm_mirror as dm
    authority = directory.capture_reader(workspace, refresh_token=True, required_scopes={"emoji:read"})
    if authority is None:
        return {}
    key = "slack-emoji-v1:" + directory.cache_scope(authority)
    with directory.fence(authority) as (_, connection):
        # The normal fence rechecks the whole OAuth generation; also assert the
        # purpose-specific permission instead of using a bot fallback.
        if "emoji:read" not in connection.scopes:
            return {}
        cached = cache.get(key)
        if isinstance(cached, dict):
            return cached
        response = dm._invoke_slack_with_locked_connection(authority, connection, "emoji_list")
        if not response.get("ok"):
            raise dm.SlackDmMirrorError("Slack emoji catalog unavailable")
        result = resolve_catalog(response.get("emoji"))
        cache.set(key, result, timeout=300)
        return result


def present(text, catalog):
    """Render standard/alias glyphs and attach only custom images used in text."""
    standard = _emoji_maps()[0]
    by_shortcode = {v.get("shortcode"): v for v in catalog.values() if v.get("shortcode")}
    images = {}
    def replace(match):
        name = match[1]
        item = catalog.get(name) or by_shortcode.get(name) or {}
        if item.get("unicode"):
            return item["unicode"]
        if item.get("url"):
            images[item["shortcode"]] = {"shortcode": item["shortcode"], "url": item["url"]}
            return ":" + item["shortcode"] + ":"
        return standard.get(name, match[0])
    parts = CODE.split(str(text or ""))
    rendered = "".join(part if i % 2 else TOKEN.sub(replace, part) for i, part in enumerate(parts))
    return rendered, list(images.values())[:100]
