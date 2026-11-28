"""A cap on the setup runs one website preparation dispatches.

La Vie, 16 Nov 2026 (prepare operation 61613d62): after preparation merged its
own setup PR #38 it dispatched another full setup, leaving PR #37 open beside a
new one. ``prepare_no_progress`` only stops a step that repeats on identical
facts, so a loop whose facts change on every pass (a merge, a rescan, a new
head) was never bounded.

Every setup run this operation dispatches is recorded in its receipt
(``setup_dispatch_run_ids``), across all its attempts: automatic ones, the
owner's Try again (``repair_setup``) and the setup replacing a pull request the
owner closed. Once ``ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT`` runs (default 3)
were dispatched, preparation does not dispatch another. It waits in
``needs_attention`` and queues the stuck-founder staff alert at once. A new
preparation operation (a new connection generation) starts at zero.

``ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT=0`` switches the cap off.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

RECEIPT_KEY = "setup_dispatch_run_ids"
HELD_KEY = "setup_dispatch_cap"
# An existing registered code: the operation cannot safely continue on its own.
CODE = "prepare_no_progress"
DEFAULT_LIMIT = 3


def limit() -> int:
    """The per-operation setup dispatch limit; 0 means no cap."""
    try:
        return max(0, int(getattr(settings, "ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT", DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def dispatched(op) -> list[str]:
    """Setup run ids this operation has dispatched, oldest first."""
    values = (op.receipt or {}).get(RECEIPT_KEY)
    return [str(value) for value in values if value] if isinstance(values, list) else []


def record(op, run_id) -> None:
    """Count one dispatched setup run; a replayed response for the same run counts once."""
    run_id = str(run_id or "")
    runs = dispatched(op)
    if run_id and run_id not in runs:
        op.receipt[RECEIPT_KEY] = [*runs, run_id]


def reached(op) -> bool:
    """Whether dispatching another setup would exceed this operation's cap."""
    cap = limit()
    return bool(cap) and len(dispatched(op)) >= cap


def held_for_staff(state, receipt) -> bool:
    """A saved cap stop survives an owner's Stop or a repeated Prepare request."""
    return state in {"needs_attention", "cancelled", "stopped"} and isinstance((receipt or {}).get(HELD_KEY), dict)


def handle_held_restart(op) -> bool:
    """Keep the staff hold; an owner who previously stopped can return the case to staff."""
    if not held_for_staff(op.state, op.receipt):
        return False
    message = "Website preparation reached its setup attempt limit. The MLAI team needs to review it before starting another setup."
    if op.state != "cancelled":
        from .website_contract import WebsiteAuthorityError
        raise WebsiteAuthorityError("website_prepare_blocked", message)
    # The generic preparation stop counts step failures. This stop counts
    # dispatched setup runs, including the ones saved before Stop was clicked.
    count = len(dispatched(op))
    at_limit = f"after {count} setup {'attempt' if count == 1 else 'attempts'}" if count else "at the setup attempt limit"
    message = f"We stopped setting up your website {at_limit}. The MLAI team needs to review it before starting another setup."
    op.state, op.next_attempt_at = "needs_attention", None
    op.receipt = {**op.receipt, "status": "needs_attention", "reason_code": CODE, "retryable": False,
        "message": message, "user_action": None, "dispatch_outcome": "replayed"}
    op.payload = {**op.payload, "user_clicks": op.payload.get("user_clicks", 1) + 1}
    op.save(update_fields=["state", "next_attempt_at", "receipt", "payload", "updated_at"])
    return True


def hold(op, *, now) -> None:
    """Wait for staff instead of dispatching another setup (``alert`` tells them once saved)."""
    runs = dispatched(op)
    op.state, op.next_attempt_at = "needs_attention", None
    # An earlier step failure is not why this stopped. Leaving its occurrence
    # would let goal accounting treat this stop as that failure again.
    for key in ("failure", "failure_fingerprint", "failure_occurrence_id", "failure_taxonomy_observation",
                "failure_taxonomy_policy_applied", "failure_class", "child_resume_pending"):
        op.receipt.pop(key, None)
    op.receipt.update(status="needs_attention", reason_code=CODE, retryable=False, current_step="setup",
        blocked_step="setup", user_action=None,
        message=(f"We stopped setting up your website after {len(runs)} setup attempts and told the MLAI team. "
                 "They will pick it up from here."),
        **{HELD_KEY: {"limit": limit(), "dispatched": len(runs), "last_run_id": runs[-1] if runs else None,
                      "held_at": now.isoformat()}})
    logger.warning("website_prepare setup_dispatch_cap operation=%s connection=%s generation=%s dispatched=%s limit=%s",
        op.pk, op.connection_id, op.generation, len(runs), limit())


def alert(op, *, now) -> None:
    """Queue the staff alert for a held operation once its stop is saved; never raises."""
    if op.state != "needs_attention" or not isinstance((op.receipt or {}).get(HELD_KEY), dict):
        return
    try:
        from .founder_stuck_alerts import queue_operation_stopped_alert
        queue_operation_stopped_alert(op, code=CODE, now=now)
    except Exception:  # The sweep queues the same alert later; never fail preparation here.
        logger.exception("website_prepare setup_dispatch_cap_alert_failed operation=%s", op.pk)
