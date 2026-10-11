"""Targeted metadata probes; source activity never fabricates unread counts."""
import math

from django.conf import settings


def timestamp(value, *, now):
    """Normalize untrusted activity/observation times within Slack clock tolerance."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return number if math.isfinite(number) and 0 <= number <= now + 300 else 0


def possibly_unread(target, snapshot, *, now):
    """Scheduling evidence: confirmed source activity is beyond the last read."""
    return (snapshot.get("excluded") is not True
            and timestamp(getattr(target, "source_activity_ts", ""), now=now)
            > timestamp(snapshot.get("last_read", "0"), now=now))


def select_targeted(ordered, snapshots, cache_key, cursor, *, now, turn):
    """Reserve one in ten turns for oldest safety work, spending spare turns too.

    Eligibility still obeys the worker's per-target provider retry deadlines.
    Stable oldest-first selection preserves continuation when ages tie.
    """
    from .read_priority import KEY, hint_pending
    hints = (cursor or {}).get(KEY) or {}
    retries = ((cursor or {}).get("message_sync_read_state_v1") or {}).get("retries") or {}
    eligible = [t for t in ordered if retries.get(t.slack_id, 0) <= now]
    def state(target):
        value = snapshots.get(cache_key(target))
        return value if isinstance(value, dict) else {}
    def age(target):
        return max(0, now - timestamp(state(target).get("fetched_at", 0), now=now))
    def urgent(target):
        hint = hints.get(target.slack_id) or {}
        if not hint_pending(hint, now):
            return False
        return ((hint.get("reason") == "own_message" and age(target) >= 1)
                or (hint.get("reason") == "visible" and age(target) >= 15))
    safety_seconds = max(60, float(getattr(settings, "READ_STATE_SAFETY_SWEEP_HOURS", 6)) * 3600)
    safety = [t for t in eligible if not state(t).get("fetched_at") or age(t) >= safety_seconds]
    urgent_targets = [t for t in eligible if urgent(t) or (state(t).get("refresh_required") and age(t) >= 1)]
    possible = [t for t in eligible if age(t) >= 30 and possibly_unread(t, state(t), now=now)]
    known = [t for t in eligible if state(t).get("is_unread") is True and age(t) >= 60]
    candidates = safety if turn % 10 == 9 and safety else (urgent_targets or possible or known or safety)
    return max(candidates, key=age, default=None)
