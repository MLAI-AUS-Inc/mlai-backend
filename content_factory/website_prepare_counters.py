"""Honest preparation numbers that never reset (plan B4).

``op.attempts`` counts worker claims, polls included (Pawshare showed 230), and
the founder message counted ``step_failures``, which Try again, a child
correlation and a source recheck reset, so the message said "We stopped after
1 attempt" for the fifth identical stop. These receipt counters only grow:

- ``tries``: preparation attempts started (the payload's ``attempt``);
- ``failures_total``: every failure preparation observed;
- ``child_runs``: scan, setup and check runs it dispatched;
- ``stop_streak``: ``{"key", "count"}``, consecutive stops on the same
  failure (fingerprint, else code).

``owner_can_retry`` (projected beside ``retryable``, which keeps meaning "the
system retries by itself") is whether the owner's Try again can change the
outcome: false while staff hold the operation (setup cap, scan budget), for an
unsupported stop under the failure policy, and once the same failure stopped
``OWNER_RETRY_CAP`` times; at the cap staff are told instead.
"""
from __future__ import annotations

COUNTER_KEYS = ("tries", "failures_total", "child_runs", "stop_streak")
STREAK_KEY = "stop_streak"
OWNER_RETRY_CAP = 3
STOPPED_STATES = frozenset({"needs_attention", "stopped"})


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    return value if type(value) is int and value > 0 else 0


def observe(op) -> None:
    """Fold the current state into the never-reset counters (idempotent within one claim)."""
    receipt = op.receipt if isinstance(op.receipt, dict) else {}
    attempt = op.payload.get("attempt", 1) if isinstance(op.payload, dict) else 1
    receipt["tries"] = max(_int(receipt.get("tries")), _int(attempt) or 1)
    if op.state == "completed":
        # A finished preparation breaks the streak: stops are consecutive.
        receipt.pop(STREAK_KEY, None)
    elif op.state in STOPPED_STATES:
        code = str(receipt.get("reason_code") or _mapping(receipt.get("failure")).get("code") or "")
        # One stop per claim: a second observation of the same claim is a
        # replayed save, never another stop.
        identity = f"{code}:{attempt}:{getattr(op, 'attempts', 0)}"
        streak = _mapping(receipt.get(STREAK_KEY))
        if streak.get("last") != identity:
            key = str(receipt.get("failure_fingerprint") or code)
            count = _int(streak.get("count")) + 1 if streak.get("key") == key else 1
            receipt[STREAK_KEY] = {"key": key, "code": code, "count": count, "last": identity}
    op.receipt = receipt


def count_failure(op) -> None:
    op.receipt["failures_total"] = _int(op.receipt.get("failures_total")) + 1


def count_child(op, run_id) -> None:
    """Count a dispatched child run once (a replayed dispatch keeps its count)."""
    run_id = str(run_id or "")
    if run_id and run_id != op.receipt.get("last_child_run_id"):
        op.receipt["child_runs"] = _int(op.receipt.get("child_runs")) + 1
        op.receipt["last_child_run_id"] = run_id


def same_failure_stops(receipt) -> int:
    """How many consecutive stops this failure has had (0 when unknown)."""
    receipt = _mapping(receipt)
    streak = _mapping(receipt.get(STREAK_KEY))
    code = str(receipt.get("reason_code") or "")
    if streak.get("code") != code and streak.get("key") not in (code, receipt.get("failure_fingerprint")):
        return 0
    return _int(streak.get("count"))


def at_cap(receipt) -> bool:
    return same_failure_stops(receipt) >= OWNER_RETRY_CAP


def owner_can_retry(state, receipt) -> bool:
    """Whether the owner's Try again can change this preparation's outcome."""
    receipt = _mapping(receipt)
    if state == "cancelled":
        # The owner pressed Stop: starting again is always theirs.
        return True
    if state not in STOPPED_STATES:
        return False
    if receipt.get("status") in {"staff_fixing", "cant_be_done_yet", "delivered_not_live", "handoff_prepared"}:
        # B11/B25: staff hold the fix, or it closed with an honest answer.
        # C7: delivered as content files; a retry cannot make it live.
        return False
    from . import website_prepare_setup_cap as setup_cap
    from .website_prepare_sticky_stop import budget_hold_active
    if isinstance(receipt.get(setup_cap.HELD_KEY), dict) or budget_hold_active(receipt):
        return False
    failure = _mapping(receipt.get("failure"))
    if receipt.get("failure_taxonomy_policy_applied") and failure.get("failure_class") == "unsupported":
        return False
    return not at_cap(receipt)
