"""Explicit public URLs and rollout settings for the Valley MCP."""
import base64
import json
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import caches

SCOPES = frozenset({"startup:brief:read", "startup:draft:write"})
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def public_base():
    value = str(getattr(settings, "VALLEY_MCP_PUBLIC_BASE_URL", "")).strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return ""
    return value


def mcp_url():
    base = public_base()
    return base + "/mcp/valley" if base else None


def availability():
    if not getattr(settings, "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", False) or not getattr(settings, "VALLEY_MCP_ENABLED", False):
        return False, "Agent connections are not enabled yet."
    if not public_base():
        return False, "Agent connections need a configured public server URL."
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if not settings.DEBUG and backend != "django.core.cache.backends.redis.RedisCache":
        return False, "Agent connections need a shared Redis cache."
    try:
        caches["default"].get("valley-mcp:availability")
    except Exception:
        return False, "Agent connections are temporarily unavailable."
    return True, None


def client_catalog():
    overrides = getattr(settings, "VALLEY_MCP_CLIENT_INSTALL_URLS", {})
    rows = []
    for key, name, setup in (
        ("claude", "Claude", "https://claude.ai/settings/connectors"),
        ("codex", "Codex", "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"),
        ("cursor", "Cursor", "https://cursor.com/docs/context/mcp"),
    ):
        override = overrides.get(key) if isinstance(overrides, dict) else None
        if key == "codex" and not override and isinstance(overrides, dict):
            # Existing deployments may have configured the shared OpenAI listing
            # under ChatGPT before the picker consolidated it into Codex.
            override = overrides.get("chatgpt")
        raw = str(override or "")
        try:
            parsed = urlsplit(raw)
            parsed.port
            install = raw if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password else None
        except ValueError:
            install = None
        method = "directory" if install else "settings"
        if not install and mcp_url() and key == "claude":
            install = "https://claude.ai/customize/connectors?" + urlencode({"modal": "add-custom-connector", "connectorName": "Valley", "connectorUrl": mcp_url()})
            method = "deeplink"
        if not install and mcp_url() and key == "cursor":
            config = base64.b64encode(json.dumps({"url": mcp_url()}, separators=(",", ":")).encode()).decode()
            install = "https://cursor.com/link/mcp/install?" + urlencode({"name": "Valley", "config": config})
            method = "deeplink"
        instructions = {
            "claude": ["Open Claude to add the prefilled Valley connector.", "If Claude asks for an OAuth client, choose Register automatically.", "Sign in to MLAI and choose the startup this agent can access."],
            "cursor": ["Open Cursor to install the Valley MCP server.", "Use Cursor's Connect action to sign in to MLAI and choose your startup."],
            "codex": ["In Codex, open Settings → MCP servers → Add server. Choose Streamable HTTP and enter the Valley server URL.", "Save, restart if requested, then choose Authenticate to sign in to MLAI and select your startup."],
        }[key]
        if key == "codex" and install:
            instructions = ["Open Valley's plugin listing and choose Install.", "Sign in to MLAI and choose the startup this agent can access."]
        rows.append({"id": key, "name": name, "installUrl": install, "setupUrl": setup,
            "method": method,
            "instructions": instructions,
            "config": {"url": mcp_url(), "transport": "http"}})
    return rows
