from __future__ import annotations

import logging
import socket
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Mapping, Optional

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from .connectors.base import ConnectorExecutionDeferred, SyncPage
from .connectors.registry import connector_registry
from .control_plane import SourceControlError, validate_action_for_execution
from .cost_control import (
    consume_memory_work_cost,
    release_memory_work_cost,
    reserve_memory_work_cost,
)
from .kernel import (
    EvidenceKernelError,
    capture_source_version,
    create_work_item,
    revoke_source_access,
    tombstone_source,
    validate_work_item_for_execution,
)
from .models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryChunk,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryDeadLetter,
    MemoryOutboxEvent,
    MemoryOutboxEventType,
    MemoryOutboxStatus,
    MemoryRuntimeLane,
    MemoryRuntimeLaneScope,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySyncRun,
    MemorySyncRunStatus,
    MemorySyncRunTrigger,
    MemoryWorkItem,
    MemoryWorkerLease,
    MemoryWorkStatus,
    MemoryWorkTaskType,
)
from .scheduling import provider_sync_interval_seconds


logger = logging.getLogger(__name__)


class MemoryRuntimeError(RuntimeError):
    pass


class PermanentMemoryRuntimeError(MemoryRuntimeError):
    pass


class RetryableMemoryRuntimeError(MemoryRuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: Optional[int] = None):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class MemoryLeaseLost(MemoryRuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedMemoryWork:
    work_item_id: uuid.UUID
    lease_token: uuid.UUID
    worker_id: str


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError) as exc:
        raise MemoryRuntimeError(f"{name} must be an integer.") from exc
    if value < 1:
        raise MemoryRuntimeError(f"{name} must be positive.")
    return value


def _locked(queryset, *, skip_locked: bool = False):
    options = {}
    if skip_locked and connection.features.has_select_for_update_skip_locked:
        options["skip_locked"] = True
    return queryset.select_for_update(**options)


def _bounded_error(exc) -> str:
    value = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
    return value[:10000]


def _provider_lane_key(provider: str) -> str:
    return f"provider:{provider}"


def _organization_lane_key(organization_id) -> str:
    return f"organization:{organization_id}"


def _ensure_runtime_lanes(work_item: MemoryWorkItem) -> None:
    MemoryRuntimeLane.objects.get_or_create(
        key=_provider_lane_key(work_item.provider),
        defaults={
            "scope": MemoryRuntimeLaneScope.PROVIDER,
            "provider": work_item.provider,
        },
    )
    MemoryRuntimeLane.objects.get_or_create(
        key=_organization_lane_key(work_item.organization_id),
        defaults={
            "scope": MemoryRuntimeLaneScope.ORGANIZATION,
            "organization_id": work_item.organization_id,
        },
    )


def _lease_for_claim(claim: ClaimedMemoryWork, *, require_unexpired: bool = True):
    lease = _locked(
        MemoryWorkerLease.objects.select_related("work_item")
    ).filter(
        lease_token=claim.lease_token,
        work_item_id=claim.work_item_id,
        worker_id=claim.worker_id,
        released_at__isnull=True,
    ).first()
    if lease is None:
        raise MemoryLeaseLost("The worker no longer owns this work item.")
    if require_unexpired and lease.expires_at <= timezone.now():
        raise MemoryLeaseLost("The worker lease has expired.")
    return lease


def _lock_claim_configuration(claim: ClaimedMemoryWork):
    configuration_id = MemoryWorkItem.objects.filter(pk=claim.work_item_id).values_list(
        "configuration_id", flat=True
    ).first()
    if configuration_id is None:
        return None
    return _locked(MemoryConnectionConfiguration.objects.select_related("organization")).get(
        pk=configuration_id
    )


def _release_lease(lease: MemoryWorkerLease, *, now) -> None:
    lease.released_at = now
    lease.save(update_fields=("released_at",))


def _mark_action_failed(work_item: MemoryWorkItem, *, error: str, now) -> None:
    if not work_item.action_request_id:
        return
    action = _locked(MemorySourceActionRequest.objects).filter(
        pk=work_item.action_request_id
    ).first()
    if action and action.status not in {
        MemoryActionStatus.COMPLETED,
        MemoryActionStatus.CANCELLED,
    }:
        action.status = MemoryActionStatus.FAILED
        action.completed_at = now
        action.last_error = error
        action.save(update_fields=("status", "completed_at", "last_error"))
    if work_item.sync_run_id:
        run = _locked(MemorySyncRun.objects).filter(pk=work_item.sync_run_id).first()
        if run and run.status not in {
            MemorySyncRunStatus.COMPLETED,
            MemorySyncRunStatus.CANCELLED,
        }:
            run.status = MemorySyncRunStatus.FAILED
            run.completed_at = now
            run.last_error = error
            run.save(
                update_fields=("status", "completed_at", "last_error", "updated_at")
            )
    if work_item.configuration_id:
        configuration = _locked(MemoryConnectionConfiguration.objects).filter(
            pk=work_item.configuration_id
        ).first()
        if configuration:
            configuration.last_error = error
            update_fields = ["last_error", "updated_at"]
            if configuration.lifecycle_state not in {
                MemoryConnectionState.DELETE_PENDING,
                MemoryConnectionState.DELETED,
                MemoryConnectionState.PAUSED,
            }:
                configuration.lifecycle_state = MemoryConnectionState.ERROR
                update_fields.append("lifecycle_state")
            configuration.save(update_fields=tuple(update_fields))


def _dead_letter(work_item: MemoryWorkItem, *, error: str, now) -> MemoryDeadLetter:
    release_memory_work_cost(work_item, now=now)
    work_item.status = MemoryWorkStatus.DEAD
    work_item.completed_at = now
    work_item.locked_at = None
    work_item.last_error = error
    work_item.save(
        update_fields=(
            "status",
            "completed_at",
            "locked_at",
            "last_error",
            "updated_at",
        )
    )
    dead_letter, _created = MemoryDeadLetter.objects.get_or_create(
        work_item=work_item,
        defaults={
            "organization": work_item.organization,
            "task_type": work_item.task_type,
            "payload_snapshot": work_item.payload,
            "attempts": work_item.attempts,
            "last_error": error,
            "dead_at": now,
        },
    )
    _mark_action_failed(work_item, error=error, now=now)
    return dead_letter


@transaction.atomic
def recover_expired_leases(*, limit: int = 100) -> dict:
    now = timezone.now()
    leases = list(
        _locked(
            MemoryWorkerLease.objects.select_related("work_item", "work_item__organization"),
            skip_locked=True,
        )
        .filter(released_at__isnull=True, expires_at__lte=now)
        .order_by("expires_at")[: max(int(limit), 0)]
    )
    recovered = 0
    dead = 0
    for lease in leases:
        work_item = lease.work_item
        _release_lease(lease, now=now)
        if work_item.status != MemoryWorkStatus.PROCESSING:
            continue
        if work_item.attempts >= work_item.max_attempts:
            _dead_letter(
                work_item,
                error="Worker lease expired after the final permitted attempt.",
                now=now,
            )
            dead += 1
        else:
            work_item.status = MemoryWorkStatus.PENDING
            work_item.available_at = now
            work_item.locked_at = None
            work_item.last_error = "Worker lease expired; work was recovered."
            work_item.save(
                update_fields=(
                    "status",
                    "available_at",
                    "locked_at",
                    "last_error",
                    "updated_at",
                )
            )
            recovered += 1
    return {"expired_leases": len(leases), "recovered": recovered, "dead": dead}


@transaction.atomic
def claim_memory_work(
    *,
    worker_id: str,
    lease_seconds: Optional[int] = None,
    organization_concurrency: Optional[int] = None,
    provider_concurrency: Optional[int] = None,
) -> Optional[ClaimedMemoryWork]:
    now = timezone.now()
    lease_seconds = lease_seconds or _positive_setting(
        "ORG_MEMORY_WORKER_LEASE_SECONDS", 120
    )
    organization_limit = organization_concurrency or _positive_setting(
        "ORG_MEMORY_ORGANIZATION_CONCURRENCY", 1
    )
    provider_limit = provider_concurrency or _positive_setting(
        "ORG_MEMORY_PROVIDER_CONCURRENCY", 4
    )
    scan_limit = _positive_setting("ORG_MEMORY_WORKER_CLAIM_SCAN_LIMIT", 50)
    executable_configuration = (
        Q(action_request__isnull=True)
        | Q(
            action_request__action=MemoryActionType.BACKFILL,
            configuration__lifecycle_state=MemoryConnectionState.BACKFILL_PENDING,
        )
        | Q(
            action_request__action__in=(
                MemoryActionType.SYNC,
                MemoryActionType.REPROCESS,
                MemoryActionType.REFRESH_PERMISSIONS,
            ),
            configuration__lifecycle_state=MemoryConnectionState.ACTIVE,
        )
        | Q(
            action_request__action=MemoryActionType.DELETE,
            configuration__lifecycle_state=MemoryConnectionState.DELETE_PENDING,
        )
    )
    candidates = list(
        _locked(
            MemoryWorkItem.objects.select_related("organization", "source_version"),
            skip_locked=True,
        )
        .filter(
            executable_configuration,
            status=MemoryWorkStatus.PENDING,
            available_at__lte=now,
        )
        .order_by("available_at", "created_at")[:scan_limit]
    )
    for work_item in candidates:
        if work_item.attempts >= work_item.max_attempts:
            _dead_letter(
                work_item,
                error=work_item.last_error or "Work exhausted its permitted attempts.",
                now=now,
            )
            continue
        _ensure_runtime_lanes(work_item)
        provider_lane = _locked(MemoryRuntimeLane.objects).get(
            pk=_provider_lane_key(work_item.provider)
        )
        organization_lane = _locked(MemoryRuntimeLane.objects).get(
            pk=_organization_lane_key(work_item.organization_id)
        )
        if provider_lane.blocked_until and provider_lane.blocked_until > now:
            continue
        active_leases = MemoryWorkerLease.objects.filter(
            released_at__isnull=True,
            expires_at__gt=now,
            work_item__status=MemoryWorkStatus.PROCESSING,
        )
        if active_leases.filter(
            work_item__organization_id=work_item.organization_id
        ).count() >= int(organization_limit):
            continue
        if active_leases.filter(
            work_item__provider=work_item.provider
        ).count() >= int(provider_limit):
            continue
        budget = reserve_memory_work_cost(work_item, now=now)
        if not budget.allowed:
            work_item.available_at = budget.retry_at
            work_item.last_error = (
                "Daily model cost pricing is not configured; work is deferred to "
                "the next budget window."
                if budget.reason == "pricing_not_configured"
                else "Daily model cost ceiling reached; work is deferred to the "
                "next budget window."
            )
            work_item.save(
                update_fields=("available_at", "last_error", "updated_at")
            )
            continue
        work_item.status = MemoryWorkStatus.PROCESSING
        work_item.attempts += 1
        work_item.locked_at = now
        work_item.last_error = ""
        work_item.save(
            update_fields=(
                "status",
                "attempts",
                "locked_at",
                "last_error",
                "updated_at",
            )
        )
        lease = MemoryWorkerLease.objects.create(
            work_item=work_item,
            worker_id=str(worker_id)[:255],
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=int(lease_seconds)),
        )
        return ClaimedMemoryWork(
            work_item_id=work_item.pk,
            lease_token=lease.lease_token,
            worker_id=lease.worker_id,
        )
    return None


@transaction.atomic
def heartbeat_memory_work(
    claim: ClaimedMemoryWork,
    *,
    lease_seconds: Optional[int] = None,
) -> timezone.datetime:
    lease_seconds = lease_seconds or _positive_setting(
        "ORG_MEMORY_WORKER_LEASE_SECONDS", 120
    )
    lease = _lease_for_claim(claim)
    now = timezone.now()
    if lease.work_item.status != MemoryWorkStatus.PROCESSING:
        raise MemoryLeaseLost("The work item is no longer processing.")
    lease.heartbeat_at = now
    lease.expires_at = now + timedelta(seconds=int(lease_seconds))
    lease.save(update_fields=("heartbeat_at", "expires_at"))
    return lease.expires_at


def _retry_delay(attempts: int) -> int:
    base = _positive_setting("ORG_MEMORY_RETRY_BASE_SECONDS", 30)
    maximum = _positive_setting("ORG_MEMORY_RETRY_MAX_SECONDS", 3600)
    return min(base * (2 ** max(int(attempts) - 1, 0)), maximum)


@transaction.atomic
def fail_memory_work(
    claim: ClaimedMemoryWork,
    error,
    *,
    retryable: bool,
    retry_after_seconds: Optional[int] = None,
) -> dict:
    _lock_claim_configuration(claim)
    lease = _lease_for_claim(claim, require_unexpired=False)
    work_item = lease.work_item
    now = timezone.now()
    message = _bounded_error(error)
    _release_lease(lease, now=now)
    if retryable and work_item.attempts < work_item.max_attempts:
        delay = int(retry_after_seconds or _retry_delay(work_item.attempts))
        delay = max(delay, 1)
        work_item.status = MemoryWorkStatus.PENDING
        work_item.available_at = now + timedelta(seconds=delay)
        work_item.locked_at = None
        work_item.last_error = message
        work_item.save(
            update_fields=(
                "status",
                "available_at",
                "locked_at",
                "last_error",
                "updated_at",
            )
        )
        if retry_after_seconds:
            _ensure_runtime_lanes(work_item)
            lane = _locked(MemoryRuntimeLane.objects).get(
                pk=_provider_lane_key(work_item.provider)
            )
            lane.blocked_until = work_item.available_at
            lane.block_reason = message[:255]
            lane.save(update_fields=("blocked_until", "block_reason", "updated_at"))
        return {"status": "retry", "available_at": work_item.available_at}
    dead_letter = _dead_letter(work_item, error=message, now=now)
    return {"status": "dead", "dead_letter_id": str(dead_letter.pk)}


@transaction.atomic
def _complete_simple_work(
    claim: ClaimedMemoryWork,
    *,
    summary: Optional[dict] = None,
    consume_cost: bool = True,
):
    lease = _lease_for_claim(claim)
    work_item = lease.work_item
    now = timezone.now()
    work_item.status = MemoryWorkStatus.COMPLETED
    work_item.completed_at = now
    work_item.locked_at = None
    work_item.last_error = ""
    if summary:
        work_item.payload = {**work_item.payload, "result": summary}
    work_item.save(
        update_fields=(
            "status",
            "completed_at",
            "locked_at",
            "last_error",
            "payload",
            "updated_at",
        )
    )
    if consume_cost:
        consume_memory_work_cost(work_item, now=now)
    else:
        release_memory_work_cost(work_item, now=now)
    _release_lease(lease, now=now)


def _action_task_type(action_type: str) -> str:
    if action_type in {MemoryActionType.BACKFILL, MemoryActionType.SYNC}:
        return MemoryWorkTaskType.INGEST
    if action_type == MemoryActionType.REFRESH_PERMISSIONS:
        return MemoryWorkTaskType.REFRESH_PERMISSIONS
    if action_type == MemoryActionType.REPROCESS:
        return MemoryWorkTaskType.RECONCILE
    if action_type == MemoryActionType.DELETE:
        return MemoryWorkTaskType.DELETE
    raise PermanentMemoryRuntimeError(f"Unsupported worker action: {action_type}")


def _capture_record(configuration, record: Mapping):
    if not isinstance(record, Mapping):
        raise PermanentMemoryRuntimeError("Connector records must be objects.")
    source_scope = None
    source_scope_id = record.get("source_scope_id")
    if source_scope_id is not None:
        source_scope = configuration.source_scopes.filter(
            pk=source_scope_id,
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).first()
        if source_scope is None:
            raise PermanentMemoryRuntimeError(
                "Connector record references an unselected source scope."
            )
    account_id = str(
        record.get("external_account_id")
        or getattr(configuration.connection, "external_account_id", "")
        or getattr(configuration.connection, "google_email", "")
        or ""
    )
    captured = capture_source_version(
        organization=configuration.organization,
        provider=configuration.provider,
        external_account_id=account_id,
        source_type=record.get("source_type"),
        external_id=record.get("external_id"),
        version_key=record.get("version_key"),
        content_hash=record.get("content_hash"),
        classification=record.get("classification")
        or configuration.default_classification,
        acl=record.get("acl"),
        chunks=record.get("chunks") or (),
        configuration=configuration,
        source_scope=source_scope,
        canonical_url=record.get("canonical_url") or "",
        title=record.get("title") or "",
        author_external_id=record.get("author_external_id") or "",
        source_created_at=record.get("source_created_at"),
        source_updated_at=record.get("source_updated_at"),
        occurred_at=record.get("occurred_at"),
        bounded_excerpt=record.get("bounded_excerpt") or "",
        metadata=record.get("metadata") or {},
        restore_access=bool(record.get("restore_access", False)),
    )
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    if isinstance(metadata.get("structured_fact"), Mapping):
        from .extraction import confirm_structured_claim_freshness

        confirm_structured_claim_freshness(
            source_version=captured[1],
            stale_after=metadata.get("stale_after"),
        )
    return captured


def _apply_removal(configuration, removal: Mapping) -> int:
    if not isinstance(removal, Mapping) or not removal.get("external_id"):
        raise PermanentMemoryRuntimeError(
            "Connector removals must identify an external source ID."
        )
    sources = MemorySource.objects.filter(
        organization=configuration.organization,
        configuration=configuration,
        provider=configuration.provider,
        external_id=str(removal["external_id"]),
    )
    if removal.get("source_type"):
        sources = sources.filter(source_type=str(removal["source_type"]))
    source = sources.first()
    if source is None:
        return 0
    if removal.get("revoke_access"):
        result = revoke_source_access(
            source,
            reason=str(removal.get("reason") or "provider_access_revoked")[:512],
        )
        return int(result.get("sources_revoked", 0))
    tombstone_source(
        source,
        reason=str(removal.get("reason") or "provider_source_removed")[:512],
    )
    return 1


def _validate_action_runtime(action: MemorySourceActionRequest):
    return validate_action_for_execution(action)


@transaction.atomic
def _start_action(claim: ClaimedMemoryWork):
    configuration = _lock_claim_configuration(claim)
    lease = _lease_for_claim(claim)
    work_item = _locked(
        MemoryWorkItem.objects.select_related(
            "configuration",
            "action_request",
            "sync_run",
        )
    ).get(pk=lease.work_item_id)
    if not work_item.action_request_id or not work_item.sync_run_id:
        raise PermanentMemoryRuntimeError("Action work is missing its durable sync run.")
    work_item.configuration = configuration
    action = work_item.action_request
    run = work_item.sync_run
    _validate_action_runtime(action)
    now = timezone.now()
    if action.status == MemoryActionStatus.PENDING:
        action.status = MemoryActionStatus.RUNNING
        action.started_at = action.started_at or now
        action.save(update_fields=("status", "started_at"))
    if run.status == MemorySyncRunStatus.PENDING:
        run.status = MemorySyncRunStatus.RUNNING
        run.started_at = run.started_at or now
        run.save(update_fields=("status", "started_at", "updated_at"))
    elif run.status != MemorySyncRunStatus.RUNNING:
        raise PermanentMemoryRuntimeError("The sync run is no longer executable.")
    return work_item, action, run


def _fetch_sync_page(work_item, action, run) -> SyncPage:
    configuration = work_item.configuration
    connector = connector_registry.get(configuration.provider)
    selected_scopes = list(
        configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).select_related("policy")
    )
    try:
        if action.action in {MemoryActionType.BACKFILL, MemoryActionType.REPROCESS}:
            checkpoint = configuration.sync_checkpoint
            if str((checkpoint or {}).get("sync_run_id") or "") != str(run.pk):
                checkpoint = {}
            page = connector.backfill(
                configuration,
                selected_scopes,
                checkpoint,
            )
            page = replace(
                page,
                checkpoint={
                    **dict(page.checkpoint or {}),
                    "sync_run_id": str(run.pk),
                    "reprocess": action.action == MemoryActionType.REPROCESS,
                },
            )
        elif action.action == MemoryActionType.SYNC:
            page = connector.incremental_sync(configuration, configuration.sync_cursor or None)
        elif action.action == MemoryActionType.REFRESH_PERMISSIONS:
            page = connector.refresh_permissions(
                configuration,
                configuration.sync_checkpoint,
            )
        else:
            raise PermanentMemoryRuntimeError(
                f"Action {action.action} requires a later processing adapter."
            )
    except ConnectorExecutionDeferred as exc:
        raise PermanentMemoryRuntimeError(str(exc)) from exc
    if not isinstance(page, SyncPage):
        raise PermanentMemoryRuntimeError("Connector execution must return a SyncPage.")
    return page


@transaction.atomic
def _commit_sync_page(claim: ClaimedMemoryWork, page: SyncPage) -> dict:
    configuration = _lock_claim_configuration(claim)
    lease = _lease_for_claim(claim)
    work_item = _locked(
        MemoryWorkItem.objects.select_related("configuration", "action_request", "sync_run")
    ).get(pk=lease.work_item_id)
    work_item.configuration = configuration
    action = _locked(MemorySourceActionRequest.objects).get(
        pk=work_item.action_request_id
    )
    run = _locked(MemorySyncRun.objects).get(pk=work_item.sync_run_id)
    _validate_action_runtime(action)

    records_processed = 0
    removals_processed = 0
    metadata_versions_created = 0
    if configuration.provider == "google_drive":
        from .drive_processing import commit_drive_processing_page

        drive_result = commit_drive_processing_page(
            configuration,
            records=page.records,
            removals=page.removals,
            sync_run=run,
            checkpoint=page.checkpoint,
            completed=not page.has_more,
        )
        records_processed = drive_result.records_processed
        removals_processed = drive_result.removals_processed
        metadata_versions_created = drive_result.metadata_versions_created
    else:
        for record in page.records:
            _capture_record(configuration, record)
            records_processed += 1
        for removal in page.removals:
            removals_processed += _apply_removal(configuration, removal)

    now = timezone.now()
    if page.next_cursor is not None:
        configuration.sync_cursor = str(page.next_cursor)
    if page.checkpoint:
        configuration.sync_checkpoint = dict(page.checkpoint)
    configuration.last_error = ""
    configuration.save(
        update_fields=("sync_cursor", "sync_checkpoint", "last_error", "updated_at")
    )
    run.cursor_after = configuration.sync_cursor
    run.checkpoint_after = configuration.sync_checkpoint
    run.pages_completed += 1
    run.records_processed += records_processed
    run.removals_processed += removals_processed

    continuation = None
    if page.has_more:
        continuation, _created = create_work_item(
            organization=work_item.organization,
            provider=work_item.provider,
            task_type=work_item.task_type,
            idempotency_key=f"action:{action.pk}:page:{run.pages_completed}",
            configuration=configuration,
            action_request=action,
            sync_run=run,
            payload={"page": run.pages_completed},
            max_attempts=work_item.max_attempts,
        )
        run.save(
            update_fields=(
                "cursor_after",
                "checkpoint_after",
                "pages_completed",
                "records_processed",
                "removals_processed",
                "updated_at",
            )
        )
    else:
        action.status = MemoryActionStatus.COMPLETED
        action.completed_at = now
        action.last_error = ""
        action.result_summary = {
            "sync_run_id": str(run.pk),
            "pages": run.pages_completed,
            "records": run.records_processed,
            "removals": run.removals_processed,
        }
        reconciliation = getattr(run, "drive_reconciliation_report", None)
        if reconciliation is not None:
            action.result_summary.update(
                drive_reconciliation_report_id=str(reconciliation.pk),
                reconciliation_counts=dict(reconciliation.counts or {}),
            )
        action.save(
            update_fields=(
                "status",
                "completed_at",
                "last_error",
                "result_summary",
            )
        )
        run.status = MemorySyncRunStatus.COMPLETED
        run.completed_at = now
        run.last_error = ""
        run.save(
            update_fields=(
                "status",
                "cursor_after",
                "checkpoint_after",
                "pages_completed",
                "records_processed",
                "removals_processed",
                "completed_at",
                "last_error",
                "updated_at",
            )
        )
        configuration.last_successful_sync_at = now
        configuration.next_scheduled_sync_at = now + timedelta(
            seconds=provider_sync_interval_seconds(
                configuration.provider,
                configuration=configuration,
            )
        )
        if action.action == MemoryActionType.BACKFILL:
            configuration.lifecycle_state = MemoryConnectionState.ACTIVE
        configuration.save(
            update_fields=(
                "last_successful_sync_at",
                "next_scheduled_sync_at",
                "lifecycle_state",
                "updated_at",
            )
        )

    work_item.status = MemoryWorkStatus.COMPLETED
    work_item.completed_at = now
    work_item.locked_at = None
    work_item.last_error = ""
    work_item.save(
        update_fields=(
            "status",
            "completed_at",
            "locked_at",
            "last_error",
            "updated_at",
        )
    )
    _release_lease(lease, now=now)

    retry_after = page.rate_limit.get("retry_after_seconds") if page.rate_limit else None
    if retry_after:
        _ensure_runtime_lanes(work_item)
        lane = _locked(MemoryRuntimeLane.objects).get(
            pk=_provider_lane_key(work_item.provider)
        )
        lane.blocked_until = now + timedelta(seconds=max(int(retry_after), 1))
        lane.block_reason = "connector_rate_limit"
        lane.save(update_fields=("blocked_until", "block_reason", "updated_at"))
    return {
        "status": "continued" if continuation else "completed",
        "records": records_processed,
        "removals": removals_processed,
        "metadata_versions_created": metadata_versions_created,
        "continuation_work_item_id": str(continuation.pk) if continuation else None,
    }


@transaction.atomic
def _complete_delete_action(claim: ClaimedMemoryWork) -> dict:
    configuration = _lock_claim_configuration(claim)
    lease = _lease_for_claim(claim)
    work_item = _locked(
        MemoryWorkItem.objects.select_related("configuration", "action_request", "sync_run")
    ).get(pk=lease.work_item_id)
    action = _locked(MemorySourceActionRequest.objects).get(pk=work_item.action_request_id)
    run = _locked(MemorySyncRun.objects).get(pk=work_item.sync_run_id)
    work_item.configuration = configuration
    _validate_action_runtime(action)
    now = timezone.now()
    if configuration.memory_sources.exclude(lifecycle_state="tombstoned").exists():
        raise PermanentMemoryRuntimeError(
            "Connection deletion cannot complete while non-tombstoned sources remain."
        )
    configuration.lifecycle_state = MemoryConnectionState.DELETED
    configuration.deleted_at = now
    configuration.last_error = ""
    configuration.save(
        update_fields=("lifecycle_state", "deleted_at", "last_error", "updated_at")
    )
    action.status = MemoryActionStatus.COMPLETED
    action.started_at = action.started_at or now
    action.completed_at = now
    action.last_error = ""
    action.result_summary = {"sync_run_id": str(run.pk), "deleted": True}
    action.save(
        update_fields=(
            "status",
            "started_at",
            "completed_at",
            "last_error",
            "result_summary",
        )
    )
    run.status = MemorySyncRunStatus.COMPLETED
    run.started_at = run.started_at or now
    run.completed_at = now
    run.save(update_fields=("status", "started_at", "completed_at", "updated_at"))
    work_item.status = MemoryWorkStatus.COMPLETED
    work_item.completed_at = now
    work_item.locked_at = None
    work_item.save(
        update_fields=("status", "completed_at", "locked_at", "updated_at")
    )
    _release_lease(lease, now=now)
    return {"status": "completed", "deleted": True}


def _execute_reconciliation(claim: ClaimedMemoryWork, work_item: MemoryWorkItem) -> dict:
    event_type = str(work_item.payload.get("event_type") or "")
    summary = {"reconciled": True}
    if event_type == MemoryOutboxEventType.SOURCE_VERSION_CAPTURED:
        if work_item.source_version and not work_item.source_version.is_current:
            if work_item.source_version.chunks.filter(active_for_retrieval=True).exists():
                raise PermanentMemoryRuntimeError(
                    "A superseded source version still has active chunks."
                )
        else:
            validate_work_item_for_execution(work_item)
            from .embeddings import schedule_chunk_embeddings
            from .extraction import schedule_source_extraction
            from .search import refresh_search_vectors

            chunk_ids = tuple(
                work_item.source_version.chunks.filter(
                    active_for_retrieval=True
                ).values_list("pk", flat=True)
            )
            summary["search_vectors_refreshed"] = refresh_search_vectors(
                chunk_ids=chunk_ids
            )
            summary["embeddings"] = schedule_chunk_embeddings(
                source_version=work_item.source_version,
                limit=max(len(chunk_ids), 1),
            )
            summary["extraction"] = schedule_source_extraction(
                source_version=work_item.source_version,
            )
    elif event_type in {
        MemoryOutboxEventType.SOURCE_ACCESS_REVOKED,
        MemoryOutboxEventType.SOURCE_TOMBSTONED,
    }:
        source_is_inactive = work_item.source and work_item.source.lifecycle_state in {
            "access_revoked",
            "tombstoned",
        }
        if source_is_inactive and MemoryChunk.objects.filter(
            source_version__source_id=work_item.source_id, active_for_retrieval=True
        ).exists():
            raise PermanentMemoryRuntimeError(
                "Revoked or tombstoned evidence still has active chunks."
            )
    elif work_item.action_request_id:
        raise PermanentMemoryRuntimeError(
            "Reprocessing requires the versioned processing adapter from a later release."
        )
    else:
        validate_work_item_for_execution(work_item)
    _complete_simple_work(claim, summary=summary)
    return {"status": "completed", **summary}


def _execute_embedding(claim: ClaimedMemoryWork, work_item: MemoryWorkItem) -> dict:
    from .embeddings import (
        EmbeddingConfigurationError,
        EmbeddingInvariantError,
        process_embedding_work,
    )

    chunk_id = (work_item.payload or {}).get("chunk_id")
    if (
        chunk_id
        and
        work_item.source_version_id
        and (
            not work_item.source_version.is_current
            or not work_item.source_version.chunks.filter(
                pk=chunk_id,
                active_for_retrieval=True,
            ).exists()
        )
    ):
        summary = {"skipped": True, "reason": "evidence_no_longer_current"}
        _complete_simple_work(claim, summary=summary, consume_cost=False)
        return {"status": "completed", **summary}
    validate_work_item_for_execution(work_item)
    try:
        summary = process_embedding_work(work_item)
    except (EmbeddingConfigurationError, EmbeddingInvariantError) as exc:
        raise PermanentMemoryRuntimeError(str(exc)) from exc
    _complete_simple_work(claim, summary=summary)
    return {"status": "completed", **summary}


def _execute_extraction(claim: ClaimedMemoryWork, work_item: MemoryWorkItem) -> dict:
    from .extraction import (
        ExtractionConfigurationError,
        ExtractionInvariantError,
        process_extraction_work,
    )

    if (
        work_item.source_version_id
        and (
            not work_item.source_version.is_current
            or not work_item.source_version.chunks.filter(active_for_retrieval=True).exists()
        )
    ):
        summary = {"skipped": True, "reason": "evidence_no_longer_current"}
        _complete_simple_work(claim, summary=summary, consume_cost=False)
        return {"status": "completed", **summary}
    validate_work_item_for_execution(work_item)
    try:
        summary = process_extraction_work(work_item)
    except (ExtractionConfigurationError, ExtractionInvariantError) as exc:
        raise PermanentMemoryRuntimeError(str(exc)) from exc
    if summary.get("claims_created"):
        from .consolidation import schedule_extraction_consolidation
        from .models import MemoryExtractionRun

        extraction_run = MemoryExtractionRun.objects.get(pk=summary["extraction_run_id"])
        summary["consolidation"] = schedule_extraction_consolidation(extraction_run)
    _complete_simple_work(claim, summary=summary)
    return {"status": "completed", **summary}


def _execute_consolidation(claim: ClaimedMemoryWork, work_item: MemoryWorkItem) -> dict:
    from .consolidation import (
        ConsolidationConfigurationError,
        ConsolidationInvariantError,
        process_consolidation_work,
    )

    if work_item.source_version_id and (
        not work_item.source_version.is_current
        or not work_item.source_version.chunks.filter(active_for_retrieval=True).exists()
    ):
        summary = {"skipped": True, "reason": "evidence_no_longer_current"}
        _complete_simple_work(claim, summary=summary, consume_cost=False)
        return {"status": "completed", **summary}
    validate_work_item_for_execution(work_item)
    try:
        summary = process_consolidation_work(work_item)
    except (ConsolidationConfigurationError, ConsolidationInvariantError) as exc:
        raise PermanentMemoryRuntimeError(str(exc)) from exc
    _complete_simple_work(claim, summary=summary)
    return {"status": "completed", **summary}


def execute_claimed_memory_work(claim: ClaimedMemoryWork) -> dict:
    try:
        work_item = MemoryWorkItem.objects.select_related(
            "configuration",
            "action_request",
            "sync_run",
            "source",
            "source_version__source",
        ).get(pk=claim.work_item_id)
        if work_item.action_request_id:
            work_item, action, run = _start_action(claim)
            if action.action == MemoryActionType.DELETE:
                return _complete_delete_action(claim)
            page = _fetch_sync_page(work_item, action, run)
            return _commit_sync_page(claim, page)
        if work_item.task_type == MemoryWorkTaskType.RECONCILE:
            return _execute_reconciliation(claim, work_item)
        if work_item.task_type == MemoryWorkTaskType.EMBED:
            return _execute_embedding(claim, work_item)
        if work_item.task_type == MemoryWorkTaskType.EXTRACT:
            return _execute_extraction(claim, work_item)
        if work_item.task_type == MemoryWorkTaskType.CONSOLIDATE:
            return _execute_consolidation(claim, work_item)
        raise PermanentMemoryRuntimeError(
            f"No runtime handler is registered for task type {work_item.task_type}."
        )
    except MemoryLeaseLost as exc:
        return {"status": "lost_lease", "error": str(exc)}
    except (PermanentMemoryRuntimeError, EvidenceKernelError, SourceControlError) as exc:
        return fail_memory_work(claim, exc, retryable=False)
    except RetryableMemoryRuntimeError as exc:
        return fail_memory_work(
            claim,
            exc,
            retryable=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
    except Exception as exc:  # defensive: unknown provider/network faults retry boundedly
        logger.exception("Organisational-memory work failed: %s", claim.work_item_id)
        return fail_memory_work(claim, exc, retryable=True)


def execute_claimed_memory_work_with_heartbeat(
    claim: ClaimedMemoryWork,
    *,
    heartbeat_seconds: Optional[int] = None,
    lease_seconds: Optional[int] = None,
) -> dict:
    """Execute work while a separate DB connection renews ownership."""

    interval = heartbeat_seconds or _positive_setting(
        "ORG_MEMORY_WORKER_HEARTBEAT_SECONDS", 30
    )
    effective_lease = lease_seconds or _positive_setting(
        "ORG_MEMORY_WORKER_LEASE_SECONDS", 120
    )
    if interval >= effective_lease:
        raise MemoryRuntimeError(
            "ORG_MEMORY_WORKER_HEARTBEAT_SECONDS must be shorter than the lease."
        )
    stopped = threading.Event()

    def renew():
        close_old_connections()
        try:
            while not stopped.wait(interval):
                try:
                    heartbeat_memory_work(claim, lease_seconds=effective_lease)
                except MemoryLeaseLost:
                    return
                except Exception:
                    logger.exception(
                        "Organisational-memory lease heartbeat failed: %s",
                        claim.work_item_id,
                    )
        finally:
            close_old_connections()

    thread = threading.Thread(
        target=renew,
        name=f"memory-heartbeat-{claim.work_item_id}",
        daemon=True,
    )
    thread.start()
    try:
        return execute_claimed_memory_work(claim)
    finally:
        stopped.set()
        thread.join(timeout=min(interval, 5))


def run_memory_worker_once(*, worker_id: str, lease_seconds: Optional[int] = None) -> dict:
    recover_expired_leases(limit=100)
    claim = claim_memory_work(worker_id=worker_id, lease_seconds=lease_seconds)
    if claim is None:
        return {"status": "idle"}
    result = execute_claimed_memory_work_with_heartbeat(
        claim,
        lease_seconds=lease_seconds,
    )
    return {"work_item_id": str(claim.work_item_id), **result}


def _outbox_task(event: MemoryOutboxEvent):
    return create_work_item(
        organization=event.organization,
        provider=event.source.provider,
        task_type=MemoryWorkTaskType.RECONCILE,
        idempotency_key=f"outbox:{event.pk}:reconcile",
        source=event.source,
        source_version=(
            event.source_version
            if event.event_type == MemoryOutboxEventType.SOURCE_VERSION_CAPTURED
            else None
        ),
        configuration=event.source.configuration,
        payload={"outbox_event_id": str(event.pk), "event_type": event.event_type},
    )


def dispatch_outbox_events(*, limit: int = 100) -> dict:
    published = 0
    failed = 0
    maximum_attempts = _positive_setting("ORG_MEMORY_OUTBOX_MAX_ATTEMPTS", 5)
    for _index in range(max(int(limit), 0)):
        with transaction.atomic():
            now = timezone.now()
            event = (
                _locked(
                    MemoryOutboxEvent.objects.select_related(
                        "organization",
                        "source",
                        "source__configuration",
                        "source_version",
                    ),
                    skip_locked=True,
                )
                .filter(status=MemoryOutboxStatus.PENDING, available_at__lte=now)
                .order_by("available_at", "created_at")
                .first()
            )
            if event is None:
                break
            try:
                _outbox_task(event)
                event.status = MemoryOutboxStatus.PUBLISHED
                event.published_at = now
                event.last_error = ""
                event.save(
                    update_fields=(
                        "status",
                        "published_at",
                        "last_error",
                        "updated_at",
                    )
                )
                published += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = _bounded_error(exc)
                if event.attempts >= maximum_attempts:
                    event.status = MemoryOutboxStatus.FAILED
                    failed += 1
                else:
                    event.available_at = now + timedelta(
                        seconds=_retry_delay(event.attempts)
                    )
                event.save(
                    update_fields=(
                        "status",
                        "attempts",
                        "last_error",
                        "available_at",
                        "updated_at",
                    )
                )
    return {"published": published, "failed": failed}


def _create_sync_run(action: MemorySourceActionRequest, *, trigger: str) -> MemorySyncRun:
    configuration = action.configuration
    run = MemorySyncRun(
        organization=configuration.organization,
        configuration=configuration,
        action_request=action,
        provider=configuration.provider,
        action_type=action.action,
        trigger=trigger,
        cursor_before=configuration.sync_cursor,
        cursor_after=configuration.sync_cursor,
        checkpoint_before=configuration.sync_checkpoint,
        checkpoint_after=configuration.sync_checkpoint,
    )
    run.full_clean()
    run.save()
    return run


def dispatch_pending_actions(*, limit: int = 100) -> dict:
    dispatched = 0
    cancelled = 0
    blocked_action_ids = set()
    for _index in range(max(int(limit), 0)):
        with transaction.atomic():
            action_query = MemorySourceActionRequest.objects.select_related(
                "configuration",
                "configuration__organization",
            ).filter(status=MemoryActionStatus.PENDING, work_items__isnull=True)
            if blocked_action_ids:
                action_query = action_query.exclude(pk__in=blocked_action_ids)
            action = _locked(action_query, skip_locked=True).order_by("requested_at").first()
            if action is None:
                break
            configuration = _locked(
                MemoryConnectionConfiguration.objects.select_related("organization")
            ).get(pk=action.configuration_id)
            action.configuration = configuration
            if MemorySyncRun.objects.filter(
                configuration=configuration,
                status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING),
            ).exists():
                blocked_action_ids.add(action.pk)
                continue
            try:
                validate_action_for_execution(action)
            except SourceControlError as exc:
                action.status = MemoryActionStatus.CANCELLED
                action.completed_at = timezone.now()
                action.last_error = _bounded_error(exc)
                action.save(update_fields=("status", "completed_at", "last_error"))
                cancelled += 1
                continue
            trigger = (
                MemorySyncRunTrigger.SCHEDULED
                if str(action.idempotency_key or "").startswith(
                    ("scheduled-sync:", "daily-reconcile:")
                )
                else MemorySyncRunTrigger.MANUAL
            )
            run = _create_sync_run(action, trigger=trigger)
            create_work_item(
                organization=action.configuration.organization,
                provider=action.configuration.provider,
                task_type=_action_task_type(action.action),
                idempotency_key=f"action:{action.pk}:page:0",
                configuration=action.configuration,
                action_request=action,
                sync_run=run,
                payload={"page": 0},
                max_attempts=_positive_setting("ORG_MEMORY_WORKER_MAX_ATTEMPTS", 5),
            )
            dispatched += 1
    return {"dispatched": dispatched, "cancelled": cancelled}


def schedule_due_connections(
    *,
    limit: int = 100,
    now=None,
    configuration_id=None,
    force: bool = False,
) -> dict:
    now = now or timezone.now()
    query = MemoryConnectionConfiguration.objects.filter(
        lifecycle_state=MemoryConnectionState.ACTIVE,
        source_scopes__selected=True,
        source_scopes__status=MemoryScopeStatus.SELECTED,
    ).distinct()
    if configuration_id:
        query = query.filter(pk=configuration_id)
    if not force:
        query = query.filter(
            Q(next_scheduled_sync_at__isnull=True) | Q(next_scheduled_sync_at__lte=now)
        )
    configuration_ids = list(
        query.order_by("next_scheduled_sync_at", "created_at").values_list(
            "pk", flat=True
        )[: max(int(limit), 0)]
    )
    scheduled = 0
    skipped = 0
    for pk in configuration_ids:
        with transaction.atomic():
            configuration = _locked(
                MemoryConnectionConfiguration.objects.select_related("organization")
            ).get(pk=pk)
            if configuration.lifecycle_state != MemoryConnectionState.ACTIVE:
                skipped += 1
                continue
            if not connector_registry.enablement(
                configuration.organization, configuration.provider
            )["enabled"]:
                skipped += 1
                continue
            if configuration.action_requests.filter(
                status__in=(MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING)
            ).exists() or configuration.sync_runs.filter(
                status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)
            ).exists():
                skipped += 1
                continue
            interval_seconds = provider_sync_interval_seconds(
                configuration.provider,
                configuration=configuration,
            )
            bucket = int(now.timestamp()) // interval_seconds
            idempotency_key = (
                f"scheduled-sync:force:{uuid.uuid4().hex}"
                if force
                else f"scheduled-sync:{bucket}"
            )
            action, created = MemorySourceActionRequest.objects.get_or_create(
                configuration=configuration,
                idempotency_key=idempotency_key,
                defaults={
                    "action": MemoryActionType.SYNC,
                    "status": MemoryActionStatus.PENDING,
                    "request_id": f"memory-scheduler-{bucket}",
                },
            )
            if not created:
                skipped += 1
                continue
            configuration.last_sync_requested_at = now
            configuration.next_scheduled_sync_at = now + timedelta(
                seconds=interval_seconds
            )
            configuration.save(
                update_fields=(
                    "last_sync_requested_at",
                    "next_scheduled_sync_at",
                    "updated_at",
                )
            )
            MemorySourceAuditEvent.objects.create(
                organization=configuration.organization,
                configuration=configuration,
                event_type="scheduled_sync_requested",
                request_id=action.request_id,
                metadata={
                    "action_id": str(action.pk),
                    "bucket": bucket,
                    "interval_seconds": interval_seconds,
                },
            )
            scheduled += 1
    return {"scheduled": scheduled, "skipped": skipped}


def schedule_memory_cycle(
    *,
    limit: int = 100,
    configuration_id=None,
    force: bool = False,
    now=None,
    organization_id=None,
    run_daily: bool = True,
    force_daily: bool = False,
) -> dict:
    from .consolidation import mark_stale_claims
    from .reconciliation import run_daily_reconciliation

    recovery = recover_expired_leases(limit=limit)
    hygiene = mark_stale_claims(limit=limit)
    outbox = dispatch_outbox_events(limit=limit)
    daily = (
        run_daily_reconciliation(
            now=now,
            organization_id=organization_id,
            force=force_daily,
        )
        if run_daily and configuration_id is None
        else {"status": "skipped", "reports": []}
    )
    scheduled = schedule_due_connections(
        limit=limit,
        now=now,
        configuration_id=configuration_id,
        force=force,
    )
    actions = dispatch_pending_actions(limit=limit)
    return {
        "recovery": recovery,
        "hygiene": hygiene,
        "outbox": outbox,
        "daily_reconciliation": daily,
        "scheduled": scheduled,
        "actions": actions,
        "queue": memory_queue_snapshot(),
    }


def memory_queue_snapshot() -> dict:
    now = timezone.now()
    pending = MemoryWorkItem.objects.filter(status=MemoryWorkStatus.PENDING)
    oldest_due = pending.filter(available_at__lte=now).order_by("available_at").first()
    return {
        "pending": pending.count(),
        "due": pending.filter(available_at__lte=now).count(),
        "processing": MemoryWorkItem.objects.filter(
            status=MemoryWorkStatus.PROCESSING
        ).count(),
        "dead": MemoryWorkItem.objects.filter(status=MemoryWorkStatus.DEAD).count(),
        "pending_outbox": MemoryOutboxEvent.objects.filter(
            status=MemoryOutboxStatus.PENDING
        ).count(),
        "failed_outbox": MemoryOutboxEvent.objects.filter(
            status=MemoryOutboxStatus.FAILED
        ).count(),
        "expired_leases": MemoryWorkerLease.objects.filter(
            released_at__isnull=True,
            expires_at__lte=now,
        ).count(),
        "active_sync_runs": MemorySyncRun.objects.filter(
            status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)
        ).count(),
        "throttled_provider_lanes": MemoryRuntimeLane.objects.filter(
            scope=MemoryRuntimeLaneScope.PROVIDER,
            blocked_until__gt=now,
        ).count(),
        "oldest_due_seconds": (
            max(int((now - oldest_due.available_at).total_seconds()), 0)
            if oldest_due
            else None
        ),
    }


@transaction.atomic
def requeue_dead_letter(dead_letter_id, *, resolved_by=None) -> MemoryWorkItem:
    dead_letter = _locked(
        MemoryDeadLetter.objects.select_related(
            "work_item",
            "work_item__organization",
        )
    ).get(pk=dead_letter_id)
    if dead_letter.resolved_at is not None:
        raise MemoryRuntimeError("This dead letter has already been resolved.")
    original = dead_letter.work_item
    if original.action_request_id:
        raise MemoryRuntimeError(
            "Connection-action failures must be resubmitted after repairing the connection."
        )
    requeued, _created = create_work_item(
        organization=original.organization,
        provider=original.provider,
        task_type=original.task_type,
        idempotency_key=f"dead-letter:{dead_letter.pk}:requeue",
        source=original.source,
        source_version=original.source_version,
        configuration=original.configuration,
        payload=original.payload,
        max_attempts=original.max_attempts,
    )
    dead_letter.resolved_at = timezone.now()
    dead_letter.resolved_by = resolved_by
    dead_letter.requeued_work_item = requeued
    dead_letter.save(
        update_fields=("resolved_at", "resolved_by", "requeued_work_item")
    )
    return requeued
