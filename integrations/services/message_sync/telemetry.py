"""Short-lived, content-free throughput counters; never part of correctness."""
import hashlib
from time import time

from django.core.cache import cache

METHODS = frozenset({
    "conversations.history", "conversations.replies", "conversations.info",
    "conversations.members", "conversations.mark", "users.conversations",
    "users.info", "users.list", "apps.event.authorizations.list",
})
COUNTERS = ("admitted", "deferred", "rate_limited", "failed", "finished", "request_ms")
RETENTION_SECONDS = 1200


def scope_key(app_id, workspace_id, method):
    """Use an opaque scope digest, excluding tokens, account IDs and content."""
    return hashlib.sha256(f"{app_id}:{workspace_id}:{method}".encode()).hexdigest()[:24]


def _key(scope, minute, counter):
    return f"message-sync:throughput:v1:{scope}:{minute}:{counter}"


def record(scope, counter, amount=1):
    """Increment an atomic shared-cache counter without interrupting sync."""
    if counter not in COUNTERS:
        raise ValueError("Unknown throughput counter")
    try:
        key = _key(scope, int(time() // 60), counter)
        cache.add(key, 0, timeout=RETENTION_SECONDS)
        cache.incr(key, max(0, int(amount)))
    except Exception:
        # Losing telemetry must not lose a provider response or refund quota.
        pass


def snapshot(scopes, *, minutes=5, now=None):
    """Read completed minute buckets; missing counters are not measured zeroes."""
    if not 1 <= minutes <= 15:
        raise ValueError("Throughput window must be 1–15 minutes")
    end = int((time() if now is None else now) // 60)
    keys = {(scope, minute, counter): _key(scope, minute, counter)
            for scope in scopes for minute in range(end - minutes, end) for counter in COUNTERS}
    values = cache.get_many(list(keys.values()))
    result = {}
    for scope in scopes:
        selected = {item: values[key] for item, key in keys.items() if item[0] == scope and key in values}
        result[scope] = None if not selected else {
            counter: sum(value for (_, _, name), value in selected.items() if name == counter)
            for counter in COUNTERS
        }
    return {"window_start": (end - minutes) * 60, "window_end": end * 60, "scopes": result}
