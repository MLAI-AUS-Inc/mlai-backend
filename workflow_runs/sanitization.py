"""Sanitizers for remote workflow payloads before database persistence."""
from __future__ import annotations

import re
from typing import Any

NUL_REPLACEMENT = "[NUL]"
_ESCAPED_NUL_RE = re.compile(r"\\u0000", re.IGNORECASE)


def sanitize_json_for_postgres(value: Any) -> Any:
    """Return a recursive copy with NUL content replaced by readable text."""
    if isinstance(value, str):
        return _ESCAPED_NUL_RE.sub(NUL_REPLACEMENT, value.replace("\x00", NUL_REPLACEMENT))
    if isinstance(value, dict):
        return {
            sanitize_json_for_postgres(key) if isinstance(key, str) else key: sanitize_json_for_postgres(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json_for_postgres(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_for_postgres(item) for item in value]
    if isinstance(value, set):
        return [sanitize_json_for_postgres(item) for item in value]
    return value
