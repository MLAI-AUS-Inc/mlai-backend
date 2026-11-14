#!/usr/bin/env python3
"""Validate SLACK_AGENT_BOTS / SLACK_AGENT_BOTS_ENABLED without Django or host settings.

Usage: printf '%s' "$value" | python3 scripts/validate_slack_agent_bots_config.py --stdin KEY
"""

import json
import re
import sys

PATTERNS = {
    "workspace_id": re.compile(r"T[A-Z0-9]{1,30}"),
    "bot_user_id": re.compile(r"[UW][A-Z0-9]{1,30}"),
    "bot_id": re.compile(r"B[A-Z0-9]{1,30}"),
    "app_id": re.compile(r"A[A-Z0-9]{1,30}"),
}
ALLOWED = {"workspace_id", "bot_user_id", "owner_user_id", "name", "bot_id", "app_id", "kind"}


def validate(key, value):
    if key == "SLACK_AGENT_BOTS_ENABLED":
        if value not in {"true", "false"}:
            raise SystemExit("SLACK_AGENT_BOTS_ENABLED must be true or false")
        return
    if key != "SLACK_AGENT_BOTS":
        raise SystemExit("Unsupported key")
    if not value.strip():
        return
    try:
        entries = json.loads(value)
    except ValueError:
        raise SystemExit("SLACK_AGENT_BOTS must be a JSON list") from None
    if not isinstance(entries, list) or len(entries) > 20:
        raise SystemExit("SLACK_AGENT_BOTS must be a JSON list of at most 20 bots")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - ALLOWED:
            raise SystemExit("SLACK_AGENT_BOTS entries use only the documented keys")
        for field in ("workspace_id", "bot_user_id"):
            if not PATTERNS[field].fullmatch(str(entry.get(field) or "")):
                raise SystemExit(f"SLACK_AGENT_BOTS {field} must be a Slack ID")
        for field in ("bot_id", "app_id"):
            if entry.get(field) and not PATTERNS[field].fullmatch(str(entry[field])):
                raise SystemExit(f"SLACK_AGENT_BOTS {field} must be a Slack ID")
        owner = entry.get("owner_user_id")
        if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0:
            raise SystemExit("SLACK_AGENT_BOTS owner_user_id must be a positive integer")
        name = entry.get("name")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(ord(c) < 32 or c in "`<>@" for c in name)):
            raise SystemExit("SLACK_AGENT_BOTS name must be 1-80 plain characters")
        if entry.get("kind", "openai_dot") != "openai_dot":
            raise SystemExit("SLACK_AGENT_BOTS kind must be openai_dot")
        pair = (entry["workspace_id"], entry["bot_user_id"])
        if pair in seen:
            raise SystemExit("SLACK_AGENT_BOTS lists a bot twice")
        seen.add(pair)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--stdin":
        raise SystemExit("usage: --stdin KEY")
    validate(sys.argv[2], sys.stdin.read())
