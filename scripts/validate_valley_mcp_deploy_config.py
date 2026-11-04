#!/usr/bin/env python3
"""Validate public Valley MCP release settings before production mutation."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit


BOOL_KEYS = {"VALLEY_MCP_ENABLED", "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", "MLAI_MCP_ENABLED", "MLAI_MCP_COMMUNITY_ENABLED", "VALLEY_MCP_UNIFIED_ALIAS_ENABLED"}
PUBLIC_BASE = "VALLEY_MCP_PUBLIC_BASE_URL"
CHALLENGE = "VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN"


def validate_single(key: str, value: str, *, optional: bool = False) -> None:
    """Reject malformed single-line configuration without echoing its value."""
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in BOOL_KEYS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key in {PUBLIC_BASE, "MLAI_MCP_COMMUNITY_RELAY_URL"}:
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{key} must be a public HTTPS origin") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or value != value.strip() or any(char.isspace() for char in value)):
            raise ValueError(f"{key} must be a public HTTPS origin")
    elif key == "MLAI_MCP_COMMUNITY_SECRET":
        if len(value.encode()) < 32 or value != value.strip():
            raise ValueError(f"{key} must contain at least 32 bytes without surrounding whitespace")
    elif key == "MLAI_MCP_CLIENT_INSTALL_URLS":
        try:
            listings = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a JSON map") from exc
        if not isinstance(listings, dict) or set(listings) - {"chatgpt", "claude", "codex", "cursor"}:
            raise ValueError(f"{key} must contain supported client IDs")
        for url in listings.values():
            try:
                parsed = urlsplit(url)
                parsed.port
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{key} must contain public HTTPS listing URLs") from exc
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or any(c.isspace() for c in url):
                raise ValueError(f"{key} must contain public HTTPS listing URLs")
    elif key == CHALLENGE:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", value):
            raise ValueError(f"{key} must be one exact public ownership token")
    else:
        raise ValueError("Unsupported Valley MCP configuration key")


def validate(environment: dict[str, str]) -> None:
    """Validate optional rollout settings while preserving existing host gates."""
    enabled = environment.get("VALLEY_MCP_ENABLED", "false")
    startup_gate = environment.get("COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", "")
    validate_single("VALLEY_MCP_ENABLED", enabled)
    validate_single("COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", startup_gate, optional=True)
    validate_single(PUBLIC_BASE, environment.get(PUBLIC_BASE, "https://api.mlai.au"))
    validate_single(CHALLENGE, environment.get(CHALLENGE, ""), optional=True)
    for setting in ("MLAI_MCP_ENABLED", "MLAI_MCP_COMMUNITY_ENABLED", "VALLEY_MCP_UNIFIED_ALIAS_ENABLED"):
        validate_single(setting, environment.get(setting, "false"))
    for setting in ("MLAI_MCP_COMMUNITY_RELAY_URL", "MLAI_MCP_COMMUNITY_SECRET", "MLAI_MCP_CLIENT_INSTALL_URLS"):
        validate_single(setting, environment.get(setting, ""), optional=True)
    if environment.get("MLAI_MCP_ENABLED") == "true" and startup_gate == "false":
        raise ValueError("MLAI_MCP_ENABLED=true requires the startup update gate")
    if environment.get("MLAI_MCP_COMMUNITY_ENABLED") == "true" and environment.get("MLAI_MCP_ENABLED", "false") != "true":
        raise ValueError("Native community MCP requires the unified server gate")
    if environment.get("VALLEY_MCP_UNIFIED_ALIAS_ENABLED") == "true" and (
            environment.get("MLAI_MCP_ENABLED", "false") != "true" or enabled != "true"):
        raise ValueError("The registered transport alias requires both explicit MCP gates")
    if enabled == "true" and startup_gate == "false":
        raise ValueError("VALLEY_MCP_ENABLED=true requires the startup update gate")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif len(sys.argv) == 1:
            validate(dict(os.environ))
        else:
            raise ValueError("Use no arguments, or --stdin with a configuration key")
    except ValueError as error:
        raise SystemExit(str(error)) from error
