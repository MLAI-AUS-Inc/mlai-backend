from __future__ import annotations

from typing import Mapping


class UnsafeMemoryMetadata(ValueError):
    pass


SENSITIVE_MEMORY_KEYS = {
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "raw_content",
    "full_text",
    "transcript",
    "message_body",
    "email_body",
}


def _is_sensitive_key(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in SENSITIVE_MEMORY_KEYS:
        return True
    if normalized.endswith(
        ("_token", "_secret", "_password", "_authorization", "_cookie")
    ):
        return True
    return normalized.startswith(
        (
            "raw_content_",
            "full_text_",
            "transcript_text",
            "message_body_",
            "email_body_",
        )
    )


def sanitize_memory_metadata(value, *, path="metadata", depth=0):
    """Keep persisted operational metadata bounded and free of secrets/bodies."""

    if depth > 5:
        raise UnsafeMemoryMetadata(f"{path} is nested too deeply.")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 2048:
            raise UnsafeMemoryMetadata(f"{path} contains an oversized string.")
        return value
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise UnsafeMemoryMetadata(f"{path} contains too many fields.")
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_sensitive_key(key):
                raise UnsafeMemoryMetadata(
                    f"{path}.{key} is not allowed in memory metadata."
                )
            result[key] = sanitize_memory_metadata(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 250:
            raise UnsafeMemoryMetadata(f"{path} contains too many items.")
        return [
            sanitize_memory_metadata(item, path=f"{path}[]", depth=depth + 1)
            for item in value
        ]
    raise UnsafeMemoryMetadata(f"{path} contains an unsupported value.")
