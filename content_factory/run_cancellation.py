"""Why a cancelled run stopped and who asked (IF-20).

Every backend cancel records ``cancellation_reason`` and ``cancelled_by`` on
``run.result`` and sends the same pair to Content Factory, so an owner's own
cancel is never mistaken for a failure and a system stop is never silent.

These are descriptive fields, not failure codes. They are never stored under
``code``, ``error_code`` or ``reason_code``, so the failure registry and its
scanner are untouched. The allowlists match Content Factory's
``backend/run_cancellation.py``.
"""

CANCELLATION_REASONS = frozenset({
    "owner_cancelled",
    "draft_deleted",
    "article_discarded",
    "worker_heartbeat_lost",
    "superseded",
    "system_stop",
    "unspecified",
})
CANCELLED_BY = frozenset({"owner", "system", "staff"})
OWNER_CANCELLATION_REASONS = frozenset({"owner_cancelled", "draft_deleted", "article_discarded"})

OWNER_STEP_MESSAGE = "Cancelled by user."
SYSTEM_STEP_MESSAGE = "Stopped."


def _text(value) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def normalize_cancellation(cancellation_reason, cancelled_by) -> tuple:
    """Return an allowlisted ``(cancellation_reason, cancelled_by)`` pair.

    An unknown or empty reason becomes ``unspecified``, which the founder-journey
    sweep alerts on, so a mislabelled stop surfaces instead of hiding. An
    unknown actor is ``owner`` for the owner-only reasons and ``system`` otherwise.
    """
    reason = _text(cancellation_reason)
    reason = reason if reason in CANCELLATION_REASONS else "unspecified"
    actor = _text(cancelled_by)
    if actor not in CANCELLED_BY:
        actor = "owner" if reason in OWNER_CANCELLATION_REASONS else "system"
    return reason, actor


def cancellation_request_fields(cancellation_reason, cancelled_by) -> dict:
    """The two fields every backend cancel sends in its Content Factory POST body."""
    reason, actor = normalize_cancellation(cancellation_reason, cancelled_by)
    return {"cancellation_reason": reason, "cancelled_by": actor}


def step_message(cancelled_by) -> str:
    """Text for a cancelled step: the owner's own words only for an owner cancel."""
    return OWNER_STEP_MESSAGE if cancelled_by == "owner" else SYSTEM_STEP_MESSAGE


def receipt_cancellation_fields(local_result, incoming_result) -> dict:
    """Pick the pair for a worker cancellation receipt; a saved non-empty value wins.

    The backend records the reason before (or without) Content Factory's
    receipt, so a receipt never relabels it. Without a saved value the
    receipt's allowlisted value is used, else ``unspecified``/``system``.
    """
    local = local_result if isinstance(local_result, dict) else {}
    incoming = incoming_result if isinstance(incoming_result, dict) else {}
    reason = local.get("cancellation_reason") or incoming.get("cancellation_reason")
    actor = local.get("cancelled_by") or incoming.get("cancelled_by")
    return cancellation_request_fields(reason, actor)


def cancellation_payload(result) -> dict:
    """Return the client run fields ``cancellationReason`` and ``cancelledBy``.

    Both are ``None`` when the run recorded no reason (a live run, or a run
    cancelled before reasons were recorded).
    """
    result = result if isinstance(result, dict) else {}
    reason = result.get("cancellation_reason")
    actor = result.get("cancelled_by")
    if not reason and not actor:
        return {"cancellationReason": None, "cancelledBy": None}
    reason, actor = normalize_cancellation(reason, actor)
    return {"cancellationReason": reason, "cancelledBy": actor}
