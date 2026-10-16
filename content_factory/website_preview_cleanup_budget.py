"""Durable wall-time cap for lifecycle operations waiting on preview cleanup."""
from datetime import timedelta

from . import website_goal as goals

KEY = "preview_cleanup_budget"
WALL_TIME = timedelta(hours=24)
FIELDS = {"version", "operation_id", "connection_id", "generation", "started_at", "deadline", "status"}


class PreviewCleanupBudgetReached(Exception):
    """Stop this cleanup attempt before another remote mutation, retaining its scope."""


def valid(op, saved, *, now):
    """Only the same operation's persisted causal window can stop its retries."""
    if (not isinstance(saved, dict) or not isinstance(saved.get("status"), str)
            or saved["status"] not in {"pending", "completed", "abandoned"}):
        return False
    fields = FIELDS | ({"abandoned_at"} if saved["status"] == "abandoned" else set())
    if (set(saved) != fields or type(saved.get("version")) is not int or saved["version"] != 1
            or saved.get("operation_id") != str(op.pk) or saved.get("connection_id") != str(op.connection_id)
            or type(saved.get("generation")) is not int or saved["generation"] != op.generation):
        return False
    started, deadline = goals.stamp(saved.get("started_at")), goals.stamp(saved.get("deadline"))
    if not started or not deadline or deadline - started != WALL_TIME or started > now + timedelta(minutes=5):
        return False
    at = goals.stamp(saved.get("abandoned_at")) if saved["status"] == "abandoned" else None
    return saved["status"] != "abandoned" or bool(at and deadline <= at <= now + timedelta(minutes=5))


def start(op, *, now):
    """Reserve the original window under the operation claim, before remote I/O."""
    identifiers = goals.mapping(op.payload).get("stop_preview_run_ids", [])
    if not identifiers:
        return
    saved = op.receipt.get(KEY)
    if KEY not in op.receipt:
        saved = {"version": 1, "operation_id": str(op.pk), "connection_id": str(op.connection_id),
                 "generation": op.generation, "started_at": now.isoformat(),
                 "deadline": (now + WALL_TIME).isoformat(), "status": "pending"}
        op.receipt[KEY] = saved


def begin(op, *, now):
    """Never repeat remote cleanup after a saved deadline or reset its budget."""
    if not goals.mapping(op.payload).get("stop_preview_run_ids"):
        return
    start(op, now=now)
    saved = op.receipt.get(KEY)
    if not valid(op, saved, now=now) or saved["status"] == "completed":
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="preview_cleanup_budget_invalid", cleanup_stop_reason="preview_cleanup_budget_invalid")
        op.receipt.pop("user_action", None)
        raise PreviewCleanupBudgetReached()
    if saved["status"] == "abandoned" or goals.stamp(saved["deadline"]) <= now:
        if saved["status"] != "abandoned":
            saved = {**saved, "status": "abandoned", "abandoned_at": now.isoformat()}
            op.receipt[KEY] = saved
        op.state, op.next_attempt_at = "needs_attention", None
        op.receipt.update(status="preview_cleanup_abandoned", cleanup_stop_reason="preview_cleanup_abandoned",
                         preview_cleanup_pending=True, remote_cleanup_pending=True)
        op.receipt.pop("user_action", None)
        # The original payload retains every run/resource ID for an owner sweep;
        # abandonment is not evidence of absence or successful deletion.
        raise PreviewCleanupBudgetReached()


def completed(op, *, now):
    """Close a successful window without changing its original start/deadline."""
    saved = op.receipt.get(KEY)
    if op.state == "completed" and valid(op, saved, now=now) and saved["status"] == "pending":
        op.receipt[KEY] = {**saved, "status": "completed"}


def abandonment(op, *, now):
    """Return only a validated saved deadline stop, never a guessed incident."""
    saved = op.receipt.get(KEY)
    return saved if (op.state == "needs_attention" and op.receipt.get("status") == "preview_cleanup_abandoned"
        and valid(op, saved, now=now) and saved["status"] == "abandoned") else None


def persist(op, *, claimed_at, now):
    """Keep lifecycle CAS semantics and reserve a deadline incident in the same commit."""
    from django.db import transaction
    from django.utils import timezone
    from .website_models import WebsiteConnection, WebsiteConnectionOperation

    def compare_and_set():
        return WebsiteConnectionOperation.objects.filter(pk=op.pk, state="pending", attempts=op.attempts,
            updated_at=claimed_at).update(receipt=op.receipt, payload=op.payload, state=op.state,
                next_attempt_at=op.next_attempt_at, updated_at=timezone.now())

    if abandonment(op, now=now) is None:
        return compare_and_set()
    with transaction.atomic():
        # Owner lifecycle writes lock the connection before operations. Take the
        # same order before CAS and inserting an outbox row referencing it.
        connection = WebsiteConnection.objects.select_for_update().filter(pk=op.connection_id).first()
        if connection is None:
            return 0
        op.connection = connection
        applied = compare_and_set()
        if applied:
            from .website_operational_alert_delivery import reserve_cleanup
            reserve_cleanup(op, now=now)
        return applied
