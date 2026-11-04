"""Explicit public URLs and rollout settings for the Valley MCP."""
import base64
import json
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import caches

SCOPES = frozenset({"startup:brief:read", "startup:draft:write"})
DOT_SCOPES = frozenset({"agent:mentions:listen", "agent:thread:read", "agent:thread:reply"})
CAPABILITIES = (
    {"id": "pulse", "title": "Pulse updates", "description": "Read your startup brief and save private narrative drafts for founder review. Financial evidence remains server-owned.", "scopes": ["startup:brief:read", "startup:draft:write"]},
    {"id": "articles-read", "title": "Read articles", "description": "Read article setup, Roo prices, existing drafts and run progress.", "scopes": ["article:read"]},
    {"id": "articles-generate", "title": "Research and generate articles", "description": "Start paid research or generation only after approving the quoted Roo cost.", "scopes": ["article:generate"]},
    {"id": "articles-edit", "title": "Edit article drafts", "description": "Revise private article drafts with revision conflict protection.", "scopes": ["article:draft:write"]},
    {"id": "articles-publish", "title": "Publish reviewed articles", "description": "Publish the exact reviewed article through your verified repository integration.", "scopes": ["article:publish"]},
    {"id": "community-read", "title": "Read community messages", "description": "List, read and search only the channels you select and can currently access.", "scopes": ["community:read"]},
    {"id": "community-write", "title": "Send community messages", "description": "Post and reply in your selected channels with visible agent attribution.", "scopes": ["community:write"]},
    {"id": "personal-agent", "title": "Personal Dot in chat", "description": "Listen for your mentions in one approved public channel, read those threads and automatically answer there. Select an existing agent you enrolled in Apps & agents. Thread messages may be sent to your AI provider.", "scopes": sorted(DOT_SCOPES)},
)
ALL_SCOPES = frozenset(scope for item in CAPABILITIES for scope in item["scopes"])
STARTUP_IDENTITY_SCOPES = ("startup:brief:read", "article:read", "startup:draft:write", "article:generate", "article:draft:write", "article:publish")


def mlai_url():
    """Return the explicit audience of the new MLAI server."""
    base = public_base()
    return base + "/mcp/mlai" if base else None


def resource_scopes(resource):
    """Keep legacy grants confined to the original Pulse permissions."""
    if resource and resource == mcp_url():
        return SCOPES
    if resource and resource == mlai_url():
        return ALL_SCOPES
    return frozenset()


def capability_catalog(scopes):
    """Describe only the capabilities requested by this OAuth client."""
    return [{**item, "scopes": [scope for scope in item["scopes"] if scope in scopes]}
            for item in CAPABILITIES if set(item["scopes"]) & set(scopes)]
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


def availability(resource=None):
    is_mlai = resource == mlai_url() if resource else False
    gate = "MLAI_MCP_ENABLED" if is_mlai else "VALLEY_MCP_ENABLED"
    if not getattr(settings, "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", False) or not getattr(settings, gate, False):
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
    unified = bool(getattr(settings, "MLAI_MCP_ENABLED", False))
    server_url = mlai_url() if unified else mcp_url()
    brand = "MLAI" if unified else "Valley"
    overrides = getattr(settings, "MLAI_MCP_CLIENT_INSTALL_URLS", {}) if unified else getattr(settings, "VALLEY_MCP_CLIENT_INSTALL_URLS", {})
    rows = []
    clients = (
        ("claude", "Claude", "https://claude.ai/settings/connectors"),
        ("codex", "Codex", "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"),
        ("cursor", "Cursor", "https://cursor.com/docs/context/mcp"),
    )
    if unified:
        clients = (("chatgpt", "ChatGPT", "https://chatgpt.com/"), *clients)
    for key, name, setup in clients:
        override = overrides.get(key) if isinstance(overrides, dict) else None
        if not unified and key == "codex" and not override and isinstance(overrides, dict):
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
        if not install and server_url and key == "claude":
            install = "https://claude.ai/customize/connectors?" + urlencode({"modal": "add-custom-connector", "connectorName": brand, "connectorUrl": server_url})
            method = "deeplink"
        if not install and server_url and key == "cursor":
            config = base64.b64encode(json.dumps({"url": server_url}, separators=(",", ":")).encode()).decode()
            install = "https://cursor.com/en-US/install-mcp?" + urlencode({"name": brand, "config": config})
            method = "deeplink"
        instructions = {
            "chatgpt": ["Open ChatGPT Settings → Apps and add the MLAI connector using the server URL.", "Sign in to MLAI, select your startup and explicitly choose capabilities and community channels."],
            "claude": ["Open Claude to add the prefilled Valley connector.", "If Claude asks for an OAuth client, choose Register automatically.", "Sign in to MLAI and choose the startup this agent can access."],
            "cursor": ["Open Cursor to install the Valley MCP server.", "Use Cursor's Connect action to sign in to MLAI and choose your startup."],
            "codex": ["In Codex, open Settings → MCP servers → Add server. Choose Streamable HTTP and enter the Valley server URL.", "Save, restart if requested, then choose Authenticate to sign in to MLAI and select your startup."],
        }[key]
        if key == "codex" and install:
            instructions = ["Open Valley's plugin listing and choose Install.", "Sign in to MLAI and choose the startup this agent can access."]
        instructions = [item.replace("Valley", brand) for item in instructions]
        rows.append({"id": key, "name": name, "installUrl": install, "setupUrl": setup,
            "method": method,
            "instructions": instructions,
            "config": {"url": server_url, "transport": "http"}})
    return rows
