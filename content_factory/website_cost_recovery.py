"""Bounded DB-only recovery for accounting; no repository or worker dispatch."""
from copy import deepcopy
from datetime import timedelta
from uuid import UUID
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import website_cost_ledger as ledger, website_goal as goals

ACTION = "usage-refresh"


def identifiers(value):
    """Only a bounded list of original run references can enter the retry queue."""
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(item, str) or not 0 < len(item) <= 100 for item in value):
        raise ValueError("Invalid accounting recovery references")
    return sorted(set(value))


def pending_for(op):
    """The accepted goal transition owns pending observations, not caller input."""
    if not ledger.enabled():
        return []
    goal = goals.mapping(goals.mapping(op.receipt).get("goal"))
    if not goals.valid_goal(goal, goal.get("scope")):
        return []
    try:
        return identifiers(goal.get("cost_pending_run_ids") or [])
    except ValueError:
        return []


def lock_connection(op):
    """Keep connection-before-parent lock order for an accepted retry reservation."""
    from .website_models import WebsiteConnection
    return WebsiteConnection.objects.select_for_update().filter(pk=op.connection_id,
        generation=op.generation, state="connected").first() is not None


def reserve(op, *, now):
    """Reserve with the successful prepare CAS in its existing transaction."""
    from .website_models import WebsiteConnectionOperation
    selected = pending_for(op)
    if not selected:
        return None
    payload = {"version": 1, "goal_operation_id": str(op.pk),
        "scope": deepcopy(op.receipt["goal"]["scope"]), "run_ids": selected}
    row, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key="articles:usage-refresh:" + str(op.pk), defaults={
        "connection": op.connection, "generation": op.generation, "action": ACTION, "state": "pending",
        "payload": payload, "receipt": {"failed_attempts": 0}, "next_attempt_at": now})
    if created:
        return row
    row = WebsiteConnectionOperation.objects.select_for_update().get(pk=row.pk)
    if (row.action != ACTION or row.connection_id != op.connection_id or row.generation != op.generation
            or goals.mapping(row.payload).get("goal_operation_id") != str(op.pk)):
        raise ValueError("Accounting recovery attribution changed")
    if row.payload == payload and row.state in {"pending", "running", "needs_attention"}:
        return row
    row.payload = payload
    if row.state != "needs_attention":
        row.state, row.next_attempt_at = "pending", now
    # A changed batch invalidates any older worker's compare-and-set claim.
    row.updated_at = now
    row.save(update_fields=["payload", "state", "next_attempt_at", "updated_at"])
    return row


def advance(identifier, *, now=None):
    """Recover retained original reports under a lease with bounded retries."""
    if not ledger.enabled():
        return None
    from .website_models import WebsiteConnectionOperation
    from workflow_runs.models import ContentFactoryRun
    now = now or timezone.now()
    with transaction.atomic():
        row = WebsiteConnectionOperation.objects.select_for_update(of=("self",)).select_related("connection").filter(pk=identifier, action=ACTION).first()
        if row is None or row.state not in {"pending", "running"} or row.next_attempt_at and row.next_attempt_at > now:
            return None
        payload = goals.mapping(row.payload)
        try:
            selected = identifiers(payload.get("run_ids"))
            parent_id = UUID(str(payload.get("goal_operation_id")))
            failures = goals.mapping(row.receipt).get("failed_attempts", 0)
            if (type(payload.get("version")) is not int or payload["version"] != 1
                    or payload.get("scope", {}).get("connection_id") != str(row.connection_id)
                    or type(failures) is not int or failures < 0):
                raise ValueError("Invalid accounting recovery scope")
        except (ValueError, AttributeError, TypeError):
            row.state, row.next_attempt_at = "needs_attention", None
            row.receipt = {"status": "invalid_accounting_recovery"}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        if row.state == "running":
            failures += 1  # An expired claim is a failed attempt even after a crash.
        limit = getattr(settings, "ARTICLES_GOAL_TRANSIENT_ATTEMPTS", 20)
        if type(limit) is not int or limit < 1 or failures >= limit:
            row.state, row.next_attempt_at = "needs_attention", None
            row.receipt = {"status": "accounting_recovery_exhausted", "failed_attempts": failures}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        row.state, row.attempts = "running", row.attempts + 1
        row.receipt = {**goals.mapping(row.receipt), "failed_attempts": failures}
        row.next_attempt_at = now + timedelta(seconds=120)
        row.save(update_fields=["state", "attempts", "receipt", "next_attempt_at", "updated_at"])
        claim, attempt = row.updated_at, row.attempts
    recovered, remaining = [], []
    for run_id in selected:
        run = ContentFactoryRun.objects.filter(run_id=run_id, organization_id=row.connection.organization_id).first()
        original = goals.mapping(getattr(run, "run_request", None))
        if (run is None or original.get("website_connection_id") != str(row.connection_id)
                or type(original.get("connection_generation")) is not int or original["connection_generation"] != row.generation):
            remaining.append(run_id)
            continue
        try:
            if ledger.record(run, now=now) is None:
                remaining.append(run_id)
            else:
                recovered.append(run_id)
        except Exception:
            remaining.append(run_id)
    # Match prepare's lock order: parent before retry row. A newer batch wins.
    with transaction.atomic():
        parent = WebsiteConnectionOperation.objects.select_for_update().filter(pk=parent_id, action="prepare", connection_id=row.connection_id, generation=row.generation).first()
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=row.pk, action=ACTION, state="running", attempts=attempt, updated_at=claim).first()
        if current is None:
            return None
        failures = goals.mapping(current.receipt).get("failed_attempts", 0)
        failures = failures + 1 if remaining and type(failures) is int and failures >= 0 else 0
        limit = getattr(settings, "ARTICLES_GOAL_TRANSIENT_ATTEMPTS", 20)
        exhausted = type(limit) is not int or limit < 1 or failures >= limit
        current.state = "needs_attention" if remaining and exhausted else "pending" if remaining else "completed"
        current.next_attempt_at = now + timedelta(seconds=min(3600, 15 * 2 ** min(failures, 8))) if current.state == "pending" else None
        current.payload = {**payload, "run_ids": remaining}
        current.receipt = {"status": "accounting_recovery_exhausted" if current.state == "needs_attention" else current.state,
            "failed_attempts": failures, "recovered_runs": len(recovered)}
        current.save(update_fields=["state", "payload", "receipt", "next_attempt_at", "updated_at"])
        if parent and parent.state != "running":
            goal = deepcopy(goals.mapping(goals.mapping(parent.receipt).get("goal")))
            if goals.valid_goal(goal, goal.get("scope")):
                goal["cost_pending_run_ids"] = [value for value in identifiers(goal.get("cost_pending_run_ids") or []) if value not in recovered]
                goal["revision"] += 1
                parent.receipt = {**parent.receipt, "goal": goal}
                parent.save(update_fields=["receipt", "updated_at"])
        return current.state
