from __future__ import annotations

import re
from typing import Any


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
LEGACY_GMAIL_READONLY_SCOPE = "gmail.readonly"
GMAIL_INSUFFICIENT_SCOPE_CODE = "gmail_insufficient_scope"
GMAIL_RECONNECT_WARNING = "Reconnect Gmail to grant read access."


def normalize_google_scope_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = re.split(r"[\s,]+", str(value or ""))

    scopes: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        scope = str(item or "").strip()
        if not scope or scope in seen:
            continue
        seen.add(scope)
        scopes.append(scope)
    return scopes


def has_gmail_read_scope(value: Any) -> bool:
    scope_value = getattr(value, "scope", value)
    scopes = set(normalize_google_scope_list(scope_value))
    return bool({GMAIL_READONLY_SCOPE, LEGACY_GMAIL_READONLY_SCOPE} & scopes)


def gmail_scope_status_payload(connection: Any) -> dict[str, Any]:
    has_scope = bool(connection) and has_gmail_read_scope(connection)
    return {
        "hasGmailScope": has_scope,
        "has_gmail_scope": has_scope,
        "needsGmailReconnect": bool(connection) and not has_scope,
        "needs_gmail_reconnect": bool(connection) and not has_scope,
    }
