"""Content-free source coverage. Absence never authorizes a message deletion.

Slack can omit history due to retention, access and partial pagination. Persist
only the range actually scanned and its qualification; never claim source parity
from a timestamp watermark or an empty intermediate page.
"""
from django.utils import timezone


def record_page(state, kind, checkpoint, response, *, complete):
    """Record a bounded observation inside the leased page-write transaction."""
    messages = response.get("messages")
    if not isinstance(messages, list) or any(not isinstance(row, dict) or not row.get("ts") for row in messages):
        raise ValueError("invalid_history_page")
    checkpoint = dict(checkpoint)
    checkpoint["observed_messages"] = bool(checkpoint.get("observed_messages") or messages)
    checkpoint["source_limited"] = bool(checkpoint.get("source_limited") or response.get("is_limited"))
    classification = (
        "incomplete" if not complete else
        "source_limited" if checkpoint["source_limited"] else
        "accessible_range" if checkpoint["observed_messages"] else
        "empty_accessible_range"
    )
    ranges = dict(state.verified_ranges or {})
    # A constant number of records: each thread already owns its durable job.
    ranges[kind] = {
        "oldest": checkpoint.get("oldest", ""),
        "latest": checkpoint.get("upper_bound", ""),
        "classification": classification,
        "checked_at": timezone.now().isoformat(),
        "absence": "unknown",  # Even a complete scan cannot prove deletion.
    }
    state.verified_ranges = ranges
    state.status = "syncing" if not complete else "current"
    state.save(update_fields=["verified_ranges", "status"])
    return checkpoint


def failure_code(exc):
    """Extract provider machine codes without logging an exception/body/token."""
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "get"):
        from .scheduler import safe_error_code
        return safe_error_code(response.get("error", type(exc).__name__))
    return type(exc).__name__


def failure_classification(code):
    if code in {"channel_not_found", "not_in_channel", "missing_scope", "no_permission", "access_denied", "token_revoked", "token_expired", "invalid_auth", "account_inactive"}:
        return "access_unavailable"
    if code == "thread_not_found":
        return "source_unavailable"  # Can also mean an unthreadable system row.
    return "unknown"
