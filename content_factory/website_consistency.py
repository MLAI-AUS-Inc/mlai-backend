"""Bounded local consistency reporting and explicitly scoped, idempotent repairs."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import website_goal as goals

TERMINAL = {"completed", "failed", "cancelled", "denied", "deleted", "stopped", "needs_attention"}
ACTIVE_CHILD = {"queued", "running", "waiting", "awaiting_approval", "code_review_ready"}


def findings_for(op, child=None):
    """Inspect local records only; no provider calls, scheduling, or writes."""
    findings = []
    if op.state in {"pending", "running"} and op.next_attempt_at is None:
        findings.append("missing_progress_timer")
    if child and op.state in TERMINAL and child.status in ACTIVE_CHILD:
        findings.append("terminal_parent_active_child")
    if child:
        result = goals.mapping(child.result)
        batches = goals.mapping(result.get("revision_batches"))
        if any(goals.mapping(batch).get("status") == "applied" and goals.mapping(batch).get("revision_applied") is not True
                for batch in list(batches.values())[:32]):
            findings.append("applied_revision_without_proof")
        if result.get("adopted_existing") is True:
            from .website_connections import _verified_setup_run_base
            head = result.get("branch_commit_sha")
            if not isinstance(head, str) or not goals.SHA.fullmatch(head) or not _verified_setup_run_base(child, head, op.connection, automatic_approval=True):
                findings.append("unverifiable_adopted_setup")
    goal = goals.mapping(op.receipt).get("goal")
    if "goal" in goals.mapping(op.receipt):
        scope = goals.scope_for(op.connection, goals.mapping(op.payload).get("company_id"))
        if not goals.valid_goal(goal, scope):
            findings.append("invalid_goal_scope")
        elif op.state == "completed" and not all(not item["required"] or goals.valid_proof(item["name"],
                item.get("proof"), scope=scope, source_sha=op.connection.verified_sha, now=timezone.now())
                for item in goal["milestones"]):
            findings.append("unverifiable_completed_goal")
    return findings


def _child(op, *, lock=False):
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import deletion_epoch
    identifier = goals.mapping(op.receipt).get("child_run_id") or goals.mapping(op.payload).get("run_id")
    if not isinstance(identifier, str) or not identifier:
        return None
    rows = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
    child = rows.filter(run_id=identifier, organization_id=op.connection.organization_id).first()
    saved = goals.mapping(getattr(child, "run_request", None))
    if (saved.get("website_connection_id") != str(op.connection_id)
            or saved.get("connection_generation") != op.generation
            or saved.get("repository_id") != op.connection.repository_id
            or saved.get("deletion_epoch") != deletion_epoch(op.connection)):
        return None
    return child


def repair_operation(operation_id, *, expected_generation, expected_updated_at, now=None):
    """Recheck saved authority and row revision before a local-only repair."""
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    now = now or timezone.now()
    snapshot = WebsiteConnectionOperation.objects.select_related("connection").filter(pk=operation_id).first()
    if not snapshot:
        return []
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=snapshot.connection.organization_id)
        connection = WebsiteConnection.objects.select_for_update().get(pk=snapshot.connection_id)
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation_id,
            generation=expected_generation, updated_at=expected_updated_at).first()
        if not op or connection.generation != expected_generation:
            return []
        op.connection = connection
        child = _child(op, lock=True)
        findings = findings_for(op, child)
        applied = []
        if "missing_progress_timer" in findings and op.action in {"prepare", "disconnect"}:
            op.next_attempt_at = now
            applied.append("missing_progress_timer")
        if "terminal_parent_active_child" in findings:
            child.status, child.resume_available = "cancelled", False
            child.error = "The owning website operation is terminal."
            child.save(update_fields=["status", "resume_available", "error", "updated_at"])
            # Keep an original-scope remote stop durable; no network call or refund.
            WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{connection.pk}:consistency-stop:{op.pk}:{child.run_id}",
                defaults={"connection": connection, "generation": expected_generation, "action": "cancel-operation",
                    "state": "pending", "next_attempt_at": now, "payload": {"cancel_run_ids": [child.run_id],
                        "stop_preview_run_ids": [child.run_id], "previous_generation": expected_generation,
                        "cancelled_operation_id": str(op.pk)}, "receipt": {"status": "consistency_repair"}})
            applied.append("terminal_parent_active_child")
        if applied:
            repairs = list(goals.mapping(op.receipt).get("consistency_repairs") or [])[-15:]
            repairs.append({"id": goals.digest({"operation": str(op.pk), "generation": expected_generation,
                "revision": str(expected_updated_at), "findings": applied}), "at": now.isoformat(), "findings": applied})
            op.receipt = {**goals.mapping(op.receipt), "consistency_repairs": repairs}
            op.save(update_fields=["receipt", "next_attempt_at", "updated_at"])
        return applied


def report_consistency(*, connection_id=None, limit=50, apply=False):
    """Default dry-run reports an explicitly bounded inventory of current records."""
    from .website_models import WebsiteConnectionOperation
    if not getattr(settings, "ARTICLES_WEBSITE_CONSISTENCY_ENABLED", False):
        return {"status": "disabled", "examined": 0, "findings": [], "applied": 0}
    if apply and not connection_id:
        raise ValueError("Apply requires an exact website connection.")
    rows = WebsiteConnectionOperation.objects.select_related("connection").filter(action__in=["prepare", "disconnect"])
    if connection_id:
        rows = rows.filter(connection_id=connection_id)
    reports, examined, applied = [], 0, 0
    for op in rows.order_by("-updated_at", "-pk")[:max(1, min(int(limit), 100))]:
        examined += 1
        if op.connection.generation != op.generation:
            continue
        findings = findings_for(op, _child(op))
        if findings:
            repaired = repair_operation(op.pk, expected_generation=op.generation, expected_updated_at=op.updated_at) if apply else []
            applied += len(repaired)
            reports.append({"operation_id": str(op.pk), "connection_id": str(op.connection_id),
                "generation": op.generation, "findings": findings, "repaired": repaired})
    return {"status": "completed", "examined": examined, "findings": reports, "applied": applied}


def run_website_consistency_sweep():
    """Scheduler inventory is reporting-only; applying repairs is an owner command."""
    return report_consistency()
