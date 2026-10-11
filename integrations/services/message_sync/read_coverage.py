"""Unread coverage is independent of archive completion and cache occupancy."""
import math
import time

MAX_SNAPSHOT_AGE_SECONDS = 120


def source_excluded(value):
    """Distinguish a confirmed source exclusion from an unavailable cursor."""
    return (isinstance(value, dict) and value.get("available") is False
            and value.get("excluded") is True)


def read_coverage(snapshots, *, discovery_complete, pending_channels=0, now=None, source_activity=None):
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
    from django.conf import settings
    targeted = getattr(settings, "MESSAGE_SYNC_TARGETED_READ_POLLING", False)
    risky = None
    if targeted:
        from .targeted_reads import timestamp
        activity = source_activity or {}
        risky = [value for key, value in snapshots.items() if isinstance(value, dict)
                 and not source_excluded(value) and (value.get("is_unread") is True
                 or timestamp(activity.get(key, ""), now=now) > timestamp(value.get("last_read", "0"), now=now))]
    timestamps = [value.get("fetched_at") for value in (risky if targeted else available + excluded)]
    fresh = all(
        type(stamp) in (int, float) and math.isfinite(stamp)
        and 0 <= now - stamp <= MAX_SNAPSHOT_AGE_SECONDS
        for stamp in timestamps
    )
    complete = bool(
        discovery_complete and not pending_channels
        and len(available) + len(excluded) == len(snapshots)
    )
    if targeted:
        # A cached boolean alone does not establish a baseline observation.
        complete = complete and all(
            type(value.get("fetched_at")) in (int, float)
            and math.isfinite(value["fetched_at"])
            and 0 <= value["fetched_at"] <= now
            for value in available + excluded
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
        **({"freshness_basis": "possibly_unread", "possibly_unread_channels": len(risky)} if targeted else {}),
    }
