#!/usr/bin/env python3
"""Validate public Valley MCP release settings before production mutation."""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit


BOOL_KEYS = {"VALLEY_MCP_ENABLED", "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED"}
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
    elif key == PUBLIC_BASE:
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{key} must be a public HTTPS origin") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or value != value.strip() or any(char.isspace() for char in value)):
            raise ValueError(f"{key} must be a public HTTPS origin")
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
