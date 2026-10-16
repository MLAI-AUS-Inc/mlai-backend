"""Durable task lifecycle. The web process never runs an agent loop.

Lock order is user -> installation -> grant -> task/responsibility -> children. A lease token and
monotonic epoch fence every worker update. Side effects belong to actions.py,
so recovering a computation never blindly repeats a network send.
"""

from __future__ import annotations

import re
import uuid
import hashlib
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Min, Sum
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import (AgentActionProposal, AgentArtifact, AgentAuditEvent, AgentCredential, AgentInstallation,
                     AgentGrant, AgentResponsibility, AgentSubscription, AgentTask)
from .policy import (AgentPermissionDenied, after_quiet_hours, artifact_name, authorize, bounded_json,
                     canonical_hash, instruction_text, normalize_limits,
                     validate_grant_base, feature_enabled)


class AgentRuntimeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
LEASE_SECONDS = 60


def locked(queryset, *, skip_locked=False):
    options = {}
    if connection.features.has_select_for_update_of:
        options["of"] = ("self",)
    if skip_locked and connection.features.has_select_for_update_skip_locked:
        options["skip_locked"] = True
    return queryset.select_for_update(**options)


def audit(event_type, *, grant=None, task=None, **metadata):
    return AgentAuditEvent.objects.create(
        grant=grant, task=task, event_type=event_type,
        metadata=bounded_json(metadata, max_bytes=4096),
    )


def _grant(grant_id):
    installation_id, owner_id = AgentGrant.objects.filter(pk=grant_id).values_list("installation_id", "installation__owner_id").get()
    owner = locked(get_user_model().objects.all()).get(pk=owner_id)
    installation = locked(AgentInstallation.objects.all()).get(pk=installation_id)
    if installation.owner_id != owner_id:
        raise AgentPermissionDenied("installation_owner_changed")
    installation.owner = owner
    grant = locked(AgentGrant.objects.all()).get(pk=grant_id, installation=installation)
    grant.installation = installation
    return grant


def _task_with_grant_lock(task_id):
    grant_id = AgentTask.objects.values_list("grant_id", flat=True).get(pk=task_id)
    grant = _grant(grant_id)
    task = locked(AgentTask.objects.all()).get(pk=task_id, grant=grant)
    task.grant = grant
    return task


def _authorize_task(task, *, access_checker=None, now=None):
    validate_grant_base(task.grant, expected_generation=task.generation, now=now)
    authorize(task.grant, "tasks:write", now=now)
    if task.resource:
        authorize(task.grant, "messages:read", task.resource, access_checker=access_checker, now=now)


def require_lease(task, lease_token, lease_epoch, *, now=None):
    now = now or timezone.now()
    if (task.status != AgentTask.Status.RUNNING or str(task.lease_token) != str(lease_token)
            or task.lease_epoch != lease_epoch or task.lease_expires_at is None
            or task.lease_expires_at <= now or task.deadline_at is None or task.deadline_at <= now):
        raise AgentRuntimeError("lease_lost")
    validate_grant_base(task.grant, expected_generation=task.generation, now=now)


def _clear_lease(task):
    task.lease_token = None
    task.lease_expires_at = None
    task.worker_id = ""


def _cancel_locked(task, *, now, code):
    if task.status in TERMINAL_STATUSES:
        return task
    task.status = AgentTask.Status.CANCELLED
    task.error_code = code
    task.finished_at = now
    _clear_lease(task)
    task.save()
    AgentCredential.objects.filter(task=task, revoked_at__isnull=True).update(revoked_at=now)
    AgentActionProposal.objects.filter(task=task, status__in=("proposed", "approved")).update(status="cancelled", error_code=code)
    # Sending may already have reached the provider. Preserve an explicit
    # uncertainty instead of pretending cancellation proves no external effect.
    AgentActionProposal.objects.filter(task=task, status="sending").update(status="unknown", error_code="cancelled_during_send")
    audit("task_cancelled", grant=task.grant, task=task, reason=code)
    return task


def _validate_inputs(inputs):
    if not isinstance(inputs, list) or len(inputs) > 100:
        raise AgentPermissionDenied("invalid_inputs")
    # These are approved bounded context objects, not filesystem paths or URLs
    # to fetch with ambient credentials. Resource access is resolved by tools.
    return bounded_json(inputs)


def _validate_delivery(delivery):
    if not isinstance(delivery, dict) or set(delivery) - {"resource", "thread_id", "mode"}:
        raise AgentPermissionDenied("invalid_delivery")
    if delivery.get("mode", "draft") not in {"draft", "notify"}:
        raise AgentPermissionDenied("invalid_delivery")
    return bounded_json(delivery, max_bytes=2048)


def scoped_memory(grant, *, responsibility, resource, access_checker=None, now=None):
    """Small source-labelled prior outcomes for exactly this responsibility.

    These notes are untrusted context, not executable skills or a replacement
    for a fresh source read. Revoked grants, changed generations, different
    resources and other members' tasks never enter this memory.
    """
    if responsibility is None:
        return []
    validate_grant_base(grant, expected_generation=responsibility.generation, now=now)
    if responsibility.grant_id != grant.pk or responsibility.resource != resource:
        raise AgentPermissionDenied("memory_scope_mismatch")
    if resource:
        authorize(grant, "messages:read", resource, access_checker=access_checker, now=now)
    rows = AgentTask.objects.filter(grant=grant, generation=grant.generation,
                                   responsibility=responsibility, resource=resource,
                                   status="completed").order_by("-finished_at")[:3]
    memories = []
    for previous in reversed(list(rows)):
        result = previous.result if isinstance(previous.result, dict) else {}
        text = next((result[key] for key in ("summary", "text", "message") if isinstance(result.get(key), str)), "")
        if text:
            memories.append({"prior_task_result": text[:2000], "untrusted_source": True,
                             "provenance": {"task_id": str(previous.pk), "resource": resource,
                                            "completed_at": previous.finished_at.isoformat()}})
    return bounded_json(memories, max_bytes=24576)


@transaction.atomic
def enqueue_task(grant, *, instruction, idempotency_key, resource="", inputs=None,
                 limits=None, delivery=None, access_checker=None, responsibility=None,
                 now=None):
    now = now or timezone.now()
    grant = _grant(grant.pk)
    authorize(grant, "tasks:write", now=now)
    if resource:
        authorize(grant, "messages:read", resource, access_checker=access_checker, now=now)
    if responsibility is not None and responsibility.grant_id != grant.pk:
        raise AgentPermissionDenied("responsibility_mismatch")
    clean_limits = normalize_limits(limits, max_seconds=grant.max_task_seconds)
    clean_delivery = _validate_delivery(delivery or {})
    if clean_delivery.get("resource"):
        authorize(grant, "messages:write", clean_delivery["resource"], access_checker=access_checker, now=now)
    from .policy import idempotency_key as clean_key
    key = clean_key(idempotency_key)
    material = {"instruction": instruction_text(instruction), "resource": resource,
                "inputs": _validate_inputs(inputs or []), "limits": clean_limits,
                "delivery": clean_delivery, "generation": grant.generation,
                "responsibility": str(responsibility.pk) if responsibility else None}
    digest = canonical_hash(material)
    existing = AgentTask.objects.filter(grant=grant, idempotency_key=key).first()
    if existing is not None:
        if existing.input_hash != digest:
            raise AgentRuntimeError("idempotency_conflict")
        return existing
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    admitted = AgentTask.objects.filter(grant=grant, created_at__gte=day)
    if admitted.count() >= grant.daily_task_limit:
        raise AgentRuntimeError("daily_task_limit")
    reserved = admitted.aggregate(total=Sum("reserved_cost_microunits"))["total"] or 0
    if reserved + clean_limits["max_cost_microunits"] > grant.daily_cost_limit_microunits:
        raise AgentRuntimeError("daily_cost_limit")
    memory = scoped_memory(grant, responsibility=responsibility, resource=resource,
                           access_checker=access_checker, now=now)
    # Idempotency hashes the submitted request. The accepted, server-resolved
    # context is frozen only on first creation, so future memory changes cannot
    # alter an already admitted task or make an exact retry conflict.
    resolved_inputs = bounded_json(material["inputs"] + memory)
    task = AgentTask.objects.create(
        grant=grant, generation=grant.generation, responsibility=responsibility,
        idempotency_key=key, input_hash=digest, instruction=material["instruction"],
        resource=resource, inputs=resolved_inputs, limits=clean_limits,
        delivery=clean_delivery, available_at=now,
        reserved_cost_microunits=clean_limits["max_cost_microunits"],
    )
    audit("task_enqueued", grant=grant, task=task)
    return task


def claim_task(*, worker_id, access_checker=None, now=None):
    now = now or timezone.now()
    if not feature_enabled():
        return None
    if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", worker_id):
        raise AgentRuntimeError("invalid_worker_id")
    recover_tasks(now=now)
    due = AgentTask.objects.filter(status__in=("queued", "retry"), available_at__lte=now)
    # Sample grants first: a large queue owned by one saturated member must
    # not occupy every candidate slot and prevent other members being served.
    candidate_grants = list(due.values("grant_id").annotate(oldest=Min("available_at"))
                            .order_by("oldest", "grant_id").values_list("grant_id", flat=True)[:100])
    for grant_id in candidate_grants:
        task_id = due.filter(grant_id=grant_id).order_by("available_at", "created_at").values_list("pk", flat=True).first()
        if task_id is None:
            continue
        with transaction.atomic():
            task = _task_with_grant_lock(task_id)
            if task.status not in ("queued", "retry") or task.available_at > now:
                continue
            try:
                _authorize_task(task, access_checker=access_checker, now=now)
            except AgentPermissionDenied as exc:
                if exc.code == "resource_unavailable":
                    task.available_at = now + timedelta(seconds=60)
                    task.error_code = exc.code
                    task.save(update_fields=("available_at", "error_code", "updated_at"))
                    continue
                _cancel_locked(task, now=now, code=exc.code)
                continue
            active = AgentTask.objects.filter(grant=task.grant, status="running", lease_expires_at__gt=now).count()
            if active >= task.grant.max_concurrent_tasks:
                continue
            task.deadline_at = task.deadline_at or now + timedelta(seconds=task.limits["max_seconds"])
            if task.deadline_at <= now:
                task.status, task.error_code, task.finished_at = "failed", "deadline_exceeded", now
                task.save()
                continue
            task.status = AgentTask.Status.RUNNING
            task.lease_token = uuid.uuid4()
            task.lease_epoch += 1
            task.lease_expires_at = min(task.deadline_at, now + timedelta(seconds=LEASE_SECONDS))
            task.worker_id = worker_id
            task.attempts += 1
            task.save()
            audit("task_claimed", grant=task.grant, task=task, lease_epoch=task.lease_epoch)
            return task
    return None


@transaction.atomic
def heartbeat_task(task_id, *, lease_token, lease_epoch, access_checker=None, now=None):
    now = now or timezone.now()
    task = _task_with_grant_lock(task_id)
    require_lease(task, lease_token, lease_epoch, now=now)
    _authorize_task(task, access_checker=access_checker, now=now)
    task.lease_expires_at = min(task.deadline_at, now + timedelta(seconds=LEASE_SECONDS))
    task.save(update_fields=("lease_expires_at", "updated_at"))
    return task


@transaction.atomic
def complete_task(task_id, *, lease_token, lease_epoch, result,
                  actual_cost_microunits=0, access_checker=None, now=None):
    now = now or timezone.now()
    task = _task_with_grant_lock(task_id)
    if not isinstance(result, dict) or "_completion" in result:
        raise AgentRuntimeError("invalid_result")
    if isinstance(actual_cost_microunits, bool) or not isinstance(actual_cost_microunits, int) or actual_cost_microunits < 0:
        raise AgentRuntimeError("invalid_usage")
    result = bounded_json(result)
    completion = {"request_hash": canonical_hash({"result": result, "actual_cost_microunits": actual_cost_microunits}),
                  "lease_hash": hashlib.sha256(str(lease_token).encode()).hexdigest(), "lease_epoch": lease_epoch}
    if task.status == AgentTask.Status.COMPLETED:
        _authorize_task(task, access_checker=access_checker, now=now)
        if task.result.get("_completion") != completion:
            raise AgentRuntimeError("completion_conflict")
        return task
    require_lease(task, lease_token, lease_epoch, now=now)
    _authorize_task(task, access_checker=access_checker, now=now)
    task.result = {**result, "_completion": completion}
    task.actual_cost_microunits = max(task.actual_cost_microunits, actual_cost_microunits)
    task.status, task.finished_at = AgentTask.Status.COMPLETED, now
    _clear_lease(task)
    task.save()
    AgentCredential.objects.filter(task=task, revoked_at__isnull=True).update(revoked_at=now)
    audit("task_completed", grant=task.grant, task=task)
    return task


@transaction.atomic
def fail_task(task_id, *, lease_token, lease_epoch, error_code="worker_failed", retryable=False, details=None, now=None):
    now = now or timezone.now()
    task = _task_with_grant_lock(task_id)
    require_lease(task, lease_token, lease_epoch, now=now)
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        error_code = "worker_failed"
    if details is not None:
        if error_code != "approval_required" or not isinstance(details, dict):
            raise AgentRuntimeError("invalid_failure_details")
        task.result = {"review_required": bounded_json(details, max_bytes=16384)}
    # Command-denial diagnostics are reviewable data. There is no command
    # resume/approval executor in this rollout; never automatically retry them.
    if error_code == "approval_required":
        retryable = False
    task.error_code = error_code
    task.status = "retry" if retryable and task.attempts < task.max_attempts and task.deadline_at > now else "failed"
    task.available_at = now + timedelta(seconds=min(300, 5 * 2 ** task.attempts))
    task.finished_at = now if task.status == "failed" else None
    _clear_lease(task)
    task.save()
    AgentCredential.objects.filter(task=task, revoked_at__isnull=True).update(revoked_at=now)
    audit("task_failed", grant=task.grant, task=task, retry=task.status == "retry", reason=error_code)
    return task


@transaction.atomic
def cancel_task(task_id, *, owner, now=None):
    now = now or timezone.now()
    task = _task_with_grant_lock(task_id)
    if task.grant.installation.owner_id != owner.pk or not owner.is_active:
        raise AgentPermissionDenied("owner_mismatch")
    return _cancel_locked(task, now=now, code="owner_cancelled")


@transaction.atomic
def revoke_grant(grant_id, *, owner, now=None):
    now = now or timezone.now()
    grant = _grant(grant_id)
    if grant.installation.owner_id != owner.pk or not owner.is_active:
        raise AgentPermissionDenied("owner_mismatch")
    if grant.revoked_at is None:
        grant.revoked_at, grant.generation = now, grant.generation + 1
        grant.save(update_fields=("revoked_at", "generation", "updated_at"))
    AgentCredential.objects.filter(grant=grant, revoked_at__isnull=True).update(revoked_at=now)
    AgentResponsibility.objects.filter(grant=grant).update(is_active=False)
    from .event_webhooks import stop_delivery
    for subscription in locked(AgentSubscription.objects.filter(grant=grant, is_active=True, delivery__mode="webhook")):
        stop_delivery(subscription, "grant_revoked", grant=grant, now=now)
    AgentSubscription.objects.filter(grant=grant, is_active=True).update(is_active=False)
    if getattr(grant.installation, "community_id", None) is not None:
        from .models import AgentInvocation, AgentInvocationReply
        AgentInvocation.objects.filter(grant=grant, status="pending").update(status="cancelled")
        AgentInvocationReply.objects.filter(invocation__grant=grant, status="pending").update(
            status="cancelled", error_code="grant_revoked")
        AgentInvocationReply.objects.filter(invocation__grant=grant, status="unknown").update(
            status="stopped_unknown", error_code="revoked_after_ambiguous_send")
    for task in locked(AgentTask.objects.filter(grant=grant).exclude(status__in=TERMINAL_STATUSES)):
        task.grant = grant
        _cancel_locked(task, now=now, code="grant_revoked")
    # Completed tasks may still have pending delivery proposals.
    AgentActionProposal.objects.filter(task__grant=grant, status__in=("proposed", "approved")).update(status="cancelled", error_code="grant_revoked")
    AgentActionProposal.objects.filter(task__grant=grant, status="sending").update(status="unknown", error_code="revoked_during_send")
    audit("grant_revoked", grant=grant)
    return grant


@transaction.atomic
def revoke_installation(installation_id, *, owner, now=None):
    now = now or timezone.now()
    persisted_owner = locked(get_user_model().objects.all()).get(pk=owner.pk)
    installation = locked(AgentInstallation.objects.all()).get(pk=installation_id)
    if installation.owner_id != owner.pk or not persisted_owner.is_active:
        raise AgentPermissionDenied("owner_mismatch")
    installation.is_active = False
    if getattr(installation, "community_id", None) is not None:
        installation.state = "revoked"
        installation.sealed_signing_key = ""
        installation.save(update_fields=("is_active", "state", "sealed_signing_key", "updated_at"))
    else:
        installation.save(update_fields=("is_active", "updated_at"))
    for grant_id in AgentGrant.objects.filter(installation=installation).order_by("pk").values_list("pk", flat=True):
        revoke_grant(grant_id, owner=persisted_owner, now=now)
    audit("installation_revoked", installation_id=str(installation.pk), owner_id=str(owner.pk))
    return installation


def recover_tasks(*, now=None, limit=100):
    now = now or timezone.now()
    if not feature_enabled():
        return 0
    recovered = 0
    ids = list(AgentTask.objects.filter(status="running", lease_expires_at__lte=now)
               .order_by("lease_expires_at").values_list("pk", flat=True)[:min(1000, limit)])
    for task_id in ids:
        with transaction.atomic():
            task = _task_with_grant_lock(task_id)
            if task.status != "running" or task.lease_expires_at > now:
                continue
            try:
                validate_grant_base(task.grant, expected_generation=task.generation, now=now)
            except AgentPermissionDenied as exc:
                _cancel_locked(task, now=now, code=exc.code)
                continue
            task.status = "retry" if task.attempts < task.max_attempts and task.deadline_at > now else "failed"
            task.error_code = "worker_lease_expired"
            task.available_at = now
            task.finished_at = now if task.status == "failed" else None
            _clear_lease(task)
            task.save()
            AgentCredential.objects.filter(task=task, revoked_at__isnull=True).update(revoked_at=now)
            audit("task_recovered", grant=task.grant, task=task, status=task.status)
            recovered += 1
    return recovered


def schedule_due_responsibilities(*, access_checker=None, now=None, limit=100):
    now = now or timezone.now()
    if not feature_enabled():
        return 0
    count = 0
    ids = list(AgentResponsibility.objects.filter(is_active=True, next_run_at__lte=now)
               .order_by("next_run_at").values_list("pk", flat=True)[:min(limit, 1000)])
    for responsibility_id in ids:
        with transaction.atomic():
            grant_id = AgentResponsibility.objects.values_list("grant_id", flat=True).get(pk=responsibility_id)
            grant = _grant(grant_id)
            responsibility = locked(AgentResponsibility.objects.all()).get(pk=responsibility_id)
            if not responsibility.is_active or responsibility.next_run_at > now:
                continue
            try:
                authorize(grant, "responsibilities:write", expected_generation=responsibility.generation, now=now)
            except AgentPermissionDenied:
                responsibility.is_active = False
                responsibility.save(update_fields=("is_active", "updated_at"))
                continue
            wake = after_quiet_hours(now, timezone_name=responsibility.timezone_name,
                                     quiet_start=responsibility.quiet_start, quiet_end=responsibility.quiet_end)
            if wake > now:
                responsibility.next_run_at = wake
                responsibility.save(update_fields=("next_run_at", "updated_at"))
                continue
            occurrence = responsibility.next_run_at
            try:
                enqueue_task(grant, instruction=responsibility.instruction,
                             idempotency_key=f"schedule:{responsibility.pk}:{int(occurrence.timestamp() * 1000000)}",
                             resource=responsibility.resource, limits=responsibility.limits,
                             delivery=responsibility.delivery, responsibility=responsibility,
                             access_checker=access_checker, now=now)
            except (AgentRuntimeError, AgentPermissionDenied) as exc:
                audit("schedule_deferred", grant=grant, reason=exc.code, responsibility_id=str(responsibility.pk))
                continue
            responsibility.last_scheduled_at = occurrence
            if responsibility.interval_seconds:
                if responsibility.interval_seconds < 60:
                    raise AgentRuntimeError("invalid_schedule_interval")
                responsibility.next_run_at = now + timedelta(seconds=responsibility.interval_seconds)
            else:
                responsibility.is_active = False
            responsibility.save()
            count += 1
    return count


@transaction.atomic
def record_artifact(task_id, *, lease_token, lease_epoch, name, media_type, sha256,
                    size_bytes, now=None):
    task = _task_with_grant_lock(task_id)
    require_lease(task, lease_token, lease_epoch, now=now)
    authorize(task.grant, "artifacts:write", now=now)
    name = artifact_name(name)
    if (not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256)
            or not isinstance(media_type, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", media_type)
            or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0):
        raise AgentRuntimeError("invalid_artifact")
    total = task.artifacts.aggregate(total=Sum("size_bytes"))["total"] or 0
    if total + size_bytes > task.limits["max_artifact_bytes"]:
        raise AgentRuntimeError("artifact_budget_exceeded")
    storage_key = f"agent-artifacts/{task.grant.installation_id}/{task.pk}/{uuid.uuid4().hex}"
    artifact = AgentArtifact.objects.create(task=task, name=name, media_type=media_type,
                                           sha256=sha256, size_bytes=size_bytes, storage_key=storage_key)
    audit("artifact_registered", grant=task.grant, task=task, artifact_id=str(artifact.pk))
    return artifact
