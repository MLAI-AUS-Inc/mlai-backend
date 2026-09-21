"""Unread coverage is independent of archive completion and cache occupancy."""
import math
import time

MAX_SNAPSHOT_AGE_SECONDS = 120


def source_excluded(value):
    """Distinguish a confirmed source exclusion from an unavailable cursor."""
    return (isinstance(value, dict) and value.get("available") is False
            and value.get("excluded") is True)


def read_coverage(snapshots, *, discovery_complete, pending_channels=0, now=None):
    """Describe missing or stale source observations without inventing reads."""
    now = time.time() if now is None else now
    available = [
        value for value in snapshots.values()
        if isinstance(value, dict) and value.get("available") is True
        and type(value.get("is_unread")) is bool
    ]
    # An explicit source membership exclusion is resolved, unlike an unknown
    # cursor or a consent-limited history page. Recheck its freshness as well.
    excluded = [
        value for value in snapshots.values()
        if source_excluded(value)
    ]
    timestamps = [value.get("fetched_at") for value in available + excluded]
    fresh = all(
        type(stamp) in (int, float) and math.isfinite(stamp)
        and 0 <= now - stamp <= MAX_SNAPSHOT_AGE_SECONDS
        for stamp in timestamps
    )
    complete = bool(
        discovery_complete and not pending_channels
        and len(available) + len(excluded) == len(snapshots)
    )
    return {
        "complete": complete,
        "fresh": complete and fresh,
        "discovery_complete": bool(discovery_complete),
        "expected_channels": len(snapshots) - len(excluded) + pending_channels,
        "available_channels": len(available),
        "excluded_channels": len(excluded),
        "pending_channels": pending_channels,
        "checked_at": now,
    }
