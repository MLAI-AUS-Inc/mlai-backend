from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Mapping, Optional

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryAclSnapshot,
    MemoryChunk,
    MemoryClassification,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryDeadLetter,
    MemoryDeletionRequest,
    MemoryDeletionStatus,
    MemoryDeletionTargetType,
    MemoryOutboxEvent,
    MemoryOutboxEventType,
    MemoryOutboxStatus,
    MemoryProvider,
    MemoryReviewItem,
    MemoryReviewSeverity,
    MemoryReviewStatus,
    MemoryReviewType,
    MemoryRuntimeLane,
    MemoryRuntimeLaneScope,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceScope,
    MemorySourceVersion,
    MemorySyncRun,
    MemorySyncRunStatus,
    MemoryWorkItem,
    MemoryWorkStatus,
    MemoryWorkTaskType,
    MemoryWorkerLease,
)
from .safety import UnsafeMemoryMetadata, sanitize_memory_metadata


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EvidenceKernelError(ValueError):
    pass


def _ensure_work_lanes(*, organization, provider: str) -> None:
    MemoryRuntimeLane.objects.get_or_create(
        key=f"provider:{provider}",
        defaults={
            "scope": MemoryRuntimeLaneScope.PROVIDER,
            "provider": provider,
        },
    )
    MemoryRuntimeLane.objects.get_or_create(
        key=f"organization:{organization.pk}",
        defaults={
            "scope": MemoryRuntimeLaneScope.ORGANIZATION,
            "organization": organization,
        },
    )


def cancel_configuration_runtime(
    configuration: MemoryConnectionConfiguration,
    *,
    reason: str,
    include_delete: bool = False,
) -> dict:
    """Cancel queued/in-flight connection work after a fail-closed state change."""

    now = timezone.now()
    actions = configuration.action_requests.filter(
        status__in=(MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING)
    )
    runs = configuration.sync_runs.filter(
        status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)
    )
    work = configuration.work_items.filter(
        status__in=(MemoryWorkStatus.PENDING, MemoryWorkStatus.PROCESSING)
    )
    if not include_delete:
        actions = actions.exclude(action=MemoryActionType.DELETE)
        runs = runs.exclude(action_type=MemoryActionType.DELETE)
        work = work.exclude(action_request__action=MemoryActionType.DELETE)
    work_ids = list(work.values_list("pk", flat=True))
    leases_released = MemoryWorkerLease.objects.filter(
        work_item_id__in=work_ids,
        released_at__isnull=True,
    ).update(released_at=now)
    from .cost_control import release_cost_reservations

    cost_reservations_released = release_cost_reservations(work_ids, now=now)
    work_cancelled = work.update(
        status=MemoryWorkStatus.CANCELLED,
        completed_at=now,
        locked_at=None,
        last_error=str(reason or "configuration_runtime_cancelled")[:10000],
        updated_at=now,
    )
    actions_cancelled = actions.update(
        status=MemoryActionStatus.CANCELLED,
        completed_at=now,
        last_error=str(reason or "configuration_runtime_cancelled")[:10000],
    )
    runs_cancelled = runs.update(
        status=MemorySyncRunStatus.CANCELLED,
        completed_at=now,
        last_error=str(reason or "configuration_runtime_cancelled")[:10000],
        updated_at=now,
    )
    return {
        "work_cancelled": work_cancelled,
        "actions_cancelled": actions_cancelled,
        "runs_cancelled": runs_cancelled,
        "leases_released": leases_released,
        "cost_reservations_released": cost_reservations_released,
    }


def suspend_configuration_runtime(configuration: MemoryConnectionConfiguration) -> dict:
    """Release in-flight action work so it can resume only after the connection does."""

    now = timezone.now()
    processing = configuration.work_items.filter(
        status=MemoryWorkStatus.PROCESSING,
        action_request__isnull=False,
    ).exclude(action_request__action=MemoryActionType.DELETE)
    work_ids = list(processing.values_list("pk", flat=True))
    leases_released = MemoryWorkerLease.objects.filter(
        work_item_id__in=work_ids,
        released_at__isnull=True,
    ).update(released_at=now)
    work_suspended = processing.update(
        status=MemoryWorkStatus.PENDING,
        available_at=now,
        locked_at=None,
        last_error="Connection paused; work is waiting for resume.",
        updated_at=now,
    )
    return {
        "work_suspended": work_suspended,
        "leases_released": leases_released,
    }


def _safe_metadata(value, *, path):
    try:
        return sanitize_memory_metadata(value, path=path)
    except UnsafeMemoryMetadata as exc:
        raise EvidenceKernelError(str(exc)) from exc


def _canonical_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise EvidenceKernelError(f"{field_name} must be a lowercase SHA-256 digest.")
    return normalized


def _required_text(value, *, field_name: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceKernelError(f"{field_name} is required.")
    if len(normalized) > max_length:
        raise EvidenceKernelError(f"{field_name} exceeds {max_length} characters.")
    return normalized


def _outbox_event(
    *,
    source: MemorySource,
    event_type: str,
    source_version: Optional[MemorySourceVersion] = None,
    payload: Optional[dict] = None,
) -> MemoryOutboxEvent:
    version_component = str(source_version.pk) if source_version else "none"
    idempotency_key = f"{event_type}:{source.pk}:{version_component}"
    event, _created = MemoryOutboxEvent.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "organization": source.organization,
            "source": source,
            "source_version": source_version,
            "event_type": event_type,
            "payload": _safe_metadata(payload or {}, path="outbox.payload"),
        },
    )
    return event


def _normalized_acl(acl: Mapping) -> dict:
    if not isinstance(acl, Mapping):
        raise EvidenceKernelError("acl must be an object.")
    accessible = acl.get("is_accessible")
    if not isinstance(accessible, bool):
        raise EvidenceKernelError("acl.is_accessible must be explicitly true or false.")
    principal_refs = _safe_metadata(
        acl.get("principal_refs") or [],
        path="acl.principal_refs",
    )
    group_refs = _safe_metadata(acl.get("group_refs") or [], path="acl.group_refs")
    link_sharing = _safe_metadata(
        acl.get("link_sharing") or {},
        path="acl.link_sharing",
    )
    metadata = _safe_metadata(acl.get("metadata") or {}, path="acl.metadata")
    if not isinstance(principal_refs, list) or not isinstance(group_refs, list):
        raise EvidenceKernelError("ACL principal and group references must be lists.")
    if not isinstance(link_sharing, dict) or not isinstance(metadata, dict):
        raise EvidenceKernelError("ACL link-sharing and metadata values must be objects.")
    provider_revision = str(acl.get("provider_revision") or "").strip()
    if len(provider_revision) > 512:
        raise EvidenceKernelError("acl.provider_revision exceeds 512 characters.")
    fingerprint_payload = {
        "provider_revision": provider_revision,
        "principal_refs": principal_refs,
        "group_refs": group_refs,
        "link_sharing": link_sharing,
        "metadata": metadata,
        "is_accessible": accessible,
    }
    return {
        **fingerprint_payload,
        "fingerprint": _canonical_hash(fingerprint_payload),
    }


def _validate_source_links(
    *,
    organization,
    provider: str,
    configuration: Optional[MemoryConnectionConfiguration],
    source_scope: Optional[MemorySourceScope],
):
    if configuration and (
        configuration.organization_id != organization.pk
        or configuration.provider != provider
    ):
        raise EvidenceKernelError("Configuration must match organisation and provider.")
    if source_scope:
        if (
            source_scope.configuration.organization_id != organization.pk
            or source_scope.configuration.provider != provider
        ):
            raise EvidenceKernelError("Source scope must match organisation and provider.")
        if not source_scope.selected or source_scope.status != MemoryScopeStatus.SELECTED:
            raise EvidenceKernelError("Source scope is not selected for ingestion.")
        if configuration and source_scope.configuration_id != configuration.pk:
            raise EvidenceKernelError("Source scope belongs to a different configuration.")


@transaction.atomic
def capture_source_version(
    *,
    organization,
    provider: str,
    external_account_id: str,
    source_type: str,
    external_id: str,
    version_key: str,
    content_hash: str,
    classification: str,
    acl: Mapping,
    chunks: Iterable[Mapping],
    configuration: Optional[MemoryConnectionConfiguration] = None,
    source_scope: Optional[MemorySourceScope] = None,
    canonical_url: str = "",
    title: str = "",
    author_user=None,
    author_external_id: str = "",
    source_created_at=None,
    source_updated_at=None,
    occurred_at=None,
    bounded_excerpt: str = "",
    metadata: Optional[dict] = None,
    captured_at=None,
    restore_access: bool = False,
) -> tuple[MemorySource, MemorySourceVersion, bool]:
    """Capture one immutable version and atomically make its chunks current."""

    provider = str(provider or "").strip().lower()
    if provider not in MemoryProvider.values:
        raise EvidenceKernelError("Provider is not supported by organisational memory.")
    if classification not in MemoryClassification.values:
        raise EvidenceKernelError("Source classification is invalid.")
    external_account_id = _required_text(
        external_account_id,
        field_name="external_account_id",
        max_length=512,
    )
    source_type = _required_text(source_type, field_name="source_type", max_length=64)
    external_id = _required_text(external_id, field_name="external_id", max_length=1024)
    version_key = _required_text(version_key, field_name="version_key", max_length=512)
    content_hash = _sha256(content_hash, field_name="content_hash")
    canonical_url = str(canonical_url or "").strip()
    title = str(title or "").strip()
    author_external_id = str(author_external_id or "").strip()
    if len(canonical_url) > 2048 or len(title) > 512 or len(author_external_id) > 512:
        raise EvidenceKernelError("Source identity metadata exceeds its field limit.")
    bounded_excerpt = str(bounded_excerpt or "")
    if len(bounded_excerpt) > 4096:
        raise EvidenceKernelError("bounded_excerpt exceeds 4096 characters.")
    source_metadata = _safe_metadata(metadata or {}, path="source.metadata")
    acl_payload = _normalized_acl(acl)
    _validate_source_links(
        organization=organization,
        provider=provider,
        configuration=configuration,
        source_scope=source_scope,
    )

    source = (
        MemorySource.objects.select_for_update()
        .filter(
            organization=organization,
            provider=provider,
            external_account_id=external_account_id,
            source_type=source_type,
            external_id=external_id,
        )
        .first()
    )
    now = captured_at or timezone.now()
    if source is None:
        source = MemorySource.objects.create(
            organization=organization,
            configuration=configuration,
            source_scope=source_scope,
            provider=provider,
            external_account_id=external_account_id,
            source_type=source_type,
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            author_user=author_user,
            author_external_id=author_external_id,
            first_seen_at=now,
            last_seen_at=now,
            metadata=source_metadata,
        )
    elif source.lifecycle_state == MemorySourceLifecycle.TOMBSTONED:
        raise EvidenceKernelError("A tombstoned source cannot be silently reactivated.")
    else:
        source.configuration = configuration or source.configuration
        source.source_scope = source_scope or source.source_scope
        source.canonical_url = canonical_url or source.canonical_url
        source.title = title or source.title
        source.author_user = author_user or source.author_user
        source.author_external_id = author_external_id or source.author_external_id
        source.last_seen_at = now
        source.metadata = source_metadata
        source.save(
            update_fields=(
                "configuration",
                "source_scope",
                "canonical_url",
                "title",
                "author_user",
                "author_external_id",
                "last_seen_at",
                "metadata",
                "updated_at",
            )
        )

    existing = source.versions.filter(version_key=version_key).first()
    if existing is not None:
        if existing.content_hash != content_hash or existing.classification != classification:
            raise EvidenceKernelError(
                "A provider version key cannot identify different evidence."
            )
        existing_acl = getattr(existing, "acl_snapshot", None)
        if existing_acl is None or existing_acl.fingerprint != acl_payload["fingerprint"]:
            raise EvidenceKernelError(
                "A provider version key cannot identify a different ACL snapshot."
            )
        return source, existing, False

    previous_current_version_id = source.current_version_id
    source.versions.filter(is_current=True).update(
        is_current=False,
        retired_at=now,
    )
    MemoryChunk.objects.filter(
        source_version__source=source,
        active_for_retrieval=True,
    ).update(active_for_retrieval=False, updated_at=now)

    version = MemorySourceVersion(
        source=source,
        version_key=version_key,
        content_hash=content_hash,
        source_created_at=source_created_at,
        source_updated_at=source_updated_at,
        occurred_at=occurred_at,
        bounded_excerpt=bounded_excerpt,
        metadata=source_metadata,
        classification=classification,
        captured_at=now,
    )
    version.full_clean()
    version.save()
    acl_snapshot = MemoryAclSnapshot(
        source_version=version,
        provider_revision=acl_payload["provider_revision"],
        principal_refs=acl_payload["principal_refs"],
        group_refs=acl_payload["group_refs"],
        link_sharing=acl_payload["link_sharing"],
        metadata=acl_payload["metadata"],
        fingerprint=acl_payload["fingerprint"],
        is_accessible=acl_payload["is_accessible"],
        captured_at=now,
        revoked_at=None if acl_payload["is_accessible"] else now,
    )
    acl_snapshot.full_clean()
    acl_snapshot.save()

    if source.access_revoked_at and restore_access and acl_snapshot.is_accessible:
        source.access_revoked_at = None
        source.lifecycle_state = MemorySourceLifecycle.ACTIVE
    elif not acl_snapshot.is_accessible:
        source.access_revoked_at = source.access_revoked_at or now
        source.lifecycle_state = MemorySourceLifecycle.ACCESS_REVOKED

    activate_chunks = bool(
        acl_snapshot.is_accessible
        and source.access_revoked_at is None
        and classification != MemoryClassification.NO_AGENT
    )
    seen_ordinals = set()
    for raw_chunk in list(chunks):
        if not isinstance(raw_chunk, Mapping):
            raise EvidenceKernelError("Every chunk must be an object.")
        try:
            ordinal = int(raw_chunk.get("ordinal", -1))
            token_count = max(int(raw_chunk.get("token_count") or 0), 0)
        except (TypeError, ValueError) as exc:
            raise EvidenceKernelError(
                "Chunk ordinal and token count must be integers."
            ) from exc
        if ordinal < 0 or ordinal in seen_ordinals:
            raise EvidenceKernelError("Chunk ordinals must be unique non-negative integers.")
        seen_ordinals.add(ordinal)
        text = str(raw_chunk.get("text") or "")
        if not text:
            raise EvidenceKernelError("Chunk text is required.")
        if len(text) > 250000:
            raise EvidenceKernelError("A memory chunk exceeds 250,000 characters.")
        chunk_classification = str(raw_chunk.get("classification") or classification)
        if chunk_classification != classification:
            raise EvidenceKernelError(
                "A chunk cannot mix visibility with its source version."
            )
        chunk_hash = raw_chunk.get("content_hash") or hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        chunk = MemoryChunk(
            source_version=version,
            ordinal=ordinal,
            chunk_kind=str(raw_chunk.get("chunk_kind") or "text")[:64],
            source_locator=_safe_metadata(
                raw_chunk.get("source_locator") or {},
                path=f"chunks[{ordinal}].source_locator",
            ),
            text=text,
            token_count=token_count,
            start_offset=raw_chunk.get("start_offset"),
            end_offset=raw_chunk.get("end_offset"),
            occurred_at=raw_chunk.get("occurred_at"),
            content_hash=_sha256(chunk_hash, field_name="chunk.content_hash"),
            classification=classification,
            active_for_retrieval=activate_chunks,
        )
        chunk.full_clean()
        chunk.save()

    source.current_version = version
    source.last_seen_at = now
    source.save(
        update_fields=(
            "current_version",
            "last_seen_at",
            "access_revoked_at",
            "lifecycle_state",
            "updated_at",
        )
    )
    _outbox_event(
        source=source,
        source_version=version,
        event_type=MemoryOutboxEventType.SOURCE_VERSION_CAPTURED,
        payload={
            "source_id": str(source.pk),
            "source_version_id": str(version.pk),
            "classification": classification,
            "chunk_count": len(seen_ordinals),
            "acl_fingerprint": acl_snapshot.fingerprint,
        },
    )
    if not acl_snapshot.is_accessible:
        _outbox_event(
            source=source,
            source_version=version,
            event_type=MemoryOutboxEventType.SOURCE_ACCESS_REVOKED,
            payload={"reason": "captured_acl_inaccessible"},
        )
        from .review_summaries import reconcile_derived_visibility_for_source

        reconcile_derived_visibility_for_source(source)
    elif previous_current_version_id:
        from .publication import retire_publications_for_source

        retire_publications_for_source(
            source,
            reason="private_source_version_changed",
        )
    return source, version, True


@transaction.atomic
def restore_source_access(
    source: MemorySource,
    *,
    acl: Mapping,
    reason: str = "provider_access_restored",
) -> dict:
    """Restore an unchanged current version only when its captured ACL still matches."""

    # Lock only the source row. Both current_version and source_scope are nullable;
    # joining either into SELECT ... FOR UPDATE is rejected by PostgreSQL because
    # Django emits an outer join for nullable relations.
    locked = MemorySource.objects.select_for_update().get(pk=source.pk)
    if locked.lifecycle_state == MemorySourceLifecycle.TOMBSTONED:
        raise EvidenceKernelError("A tombstoned source cannot be silently reactivated.")
    current = locked.current_version
    if current is None or not current.is_current:
        raise EvidenceKernelError("Only a current source version can have access restored.")

    acl_payload = _normalized_acl(acl)
    if not acl_payload["is_accessible"]:
        raise EvidenceKernelError("Source access cannot be restored with an inaccessible ACL.")
    snapshot = MemoryAclSnapshot.objects.select_for_update().get(
        source_version=current
    )
    if snapshot.fingerprint != acl_payload["fingerprint"]:
        raise EvidenceKernelError(
            "Changed provider ACL evidence requires a new source version."
        )

    now = timezone.now()
    was_revoked = bool(
        locked.access_revoked_at
        or locked.lifecycle_state == MemorySourceLifecycle.ACCESS_REVOKED
        or not snapshot.is_accessible
    )
    if not was_revoked:
        return {"sources_restored": 0, "chunks_activated": 0}
    if (
        locked.configuration_id
        and locked.configuration.lifecycle_state != MemoryConnectionState.ACTIVE
    ):
        raise EvidenceKernelError(
            "Source access cannot be restored until its connection is active."
        )
    if (
        locked.source_scope_id
        and current.classification != locked.source_scope.default_classification
    ):
        raise EvidenceKernelError(
            "Changed source classification requires a new source version."
        )
    if not snapshot.is_accessible or snapshot.revoked_at is not None:
        snapshot.is_accessible = True
        snapshot.revoked_at = None
        snapshot.save(update_fields=("is_accessible", "revoked_at"))

    locked.lifecycle_state = MemorySourceLifecycle.ACTIVE
    locked.access_revoked_at = None
    locked.last_seen_at = now
    locked.save(
        update_fields=(
            "lifecycle_state",
            "access_revoked_at",
            "last_seen_at",
            "updated_at",
        )
    )
    chunks_activated = 0
    if current.classification != MemoryClassification.NO_AGENT:
        chunks_activated = current.chunks.filter(
            active_for_retrieval=False
        ).update(active_for_retrieval=True, updated_at=now)
    _outbox_event(
        source=locked,
        source_version=current,
        event_type=MemoryOutboxEventType.SOURCE_ACCESS_RESTORED,
        payload={"reason": str(reason or "provider_access_restored")[:512]},
    )
    from .review_summaries import reconcile_derived_visibility_for_source

    reconcile_derived_visibility_for_source(locked)
    _refresh_current_state_projection(locked.organization)
    return {
        "sources_restored": int(was_revoked),
        "chunks_activated": chunks_activated,
    }


def _revoke_locked_source(source: MemorySource, *, reason: str, now) -> dict:
    if source.lifecycle_state == MemorySourceLifecycle.TOMBSTONED:
        return {"sources_revoked": 0, "chunks_deactivated": 0}
    chunks = MemoryChunk.objects.filter(
        source_version__source=source,
        active_for_retrieval=True,
    ).update(active_for_retrieval=False, updated_at=now)
    current = source.current_version
    if current is not None:
        MemoryAclSnapshot.objects.filter(
            source_version=current,
            is_accessible=True,
        ).update(is_accessible=False, revoked_at=now)
    changed = source.lifecycle_state != MemorySourceLifecycle.ACCESS_REVOKED
    source.lifecycle_state = MemorySourceLifecycle.ACCESS_REVOKED
    source.access_revoked_at = source.access_revoked_at or now
    source.save(
        update_fields=("lifecycle_state", "access_revoked_at", "updated_at")
    )
    if changed:
        _outbox_event(
            source=source,
            source_version=current,
            event_type=MemoryOutboxEventType.SOURCE_ACCESS_REVOKED,
            payload={"reason": str(reason or "access_revoked")[:512]},
        )
    from .review_summaries import reconcile_derived_visibility_for_source

    reconcile_derived_visibility_for_source(source)
    return {"sources_revoked": int(changed), "chunks_deactivated": chunks}


def _refresh_current_state_projection(organization) -> None:
    # Imported lazily so the evidence kernel remains the lower-level dependency.
    from .consolidation import refresh_current_state

    refresh_current_state(organization)


@transaction.atomic
def revoke_source_access(source: MemorySource, *, reason: str) -> dict:
    locked = MemorySource.objects.select_for_update().get(pk=source.pk)
    result = _revoke_locked_source(locked, reason=reason, now=timezone.now())
    _refresh_current_state_projection(locked.organization)
    return result


@transaction.atomic
def revoke_configuration_sources(
    configuration: MemoryConnectionConfiguration,
    *,
    reason: str,
) -> dict:
    totals = {"sources_revoked": 0, "chunks_deactivated": 0}
    now = timezone.now()
    sources = MemorySource.objects.select_for_update().filter(
        configuration=configuration,
    )
    for source in sources:
        result = _revoke_locked_source(source, reason=reason, now=now)
        for key in totals:
            totals[key] += result[key]
    _refresh_current_state_projection(configuration.organization)
    return totals


def _tombstone_locked_source(source: MemorySource, *, reason: str, now) -> dict:
    if source.lifecycle_state == MemorySourceLifecycle.TOMBSTONED:
        return {"sources_tombstoned": 0, "versions_retired": 0, "chunks_deactivated": 0}
    chunks = MemoryChunk.objects.filter(
        source_version__source=source,
        active_for_retrieval=True,
    ).update(active_for_retrieval=False, updated_at=now)
    current_versions = source.versions.filter(
        is_current=True,
        tombstoned_at__isnull=True,
    ).update(
        is_current=False,
        retired_at=now,
        tombstoned_at=now,
    )
    historical_versions = source.versions.filter(
        is_current=False,
        tombstoned_at__isnull=True,
    ).update(tombstoned_at=now)
    versions = current_versions + historical_versions
    MemoryWorkItem.objects.filter(
        Q(source=source) | Q(source_version__source=source),
        status__in=(MemoryWorkStatus.PENDING, MemoryWorkStatus.FAILED),
    ).update(status=MemoryWorkStatus.CANCELLED, completed_at=now, updated_at=now)
    previous_version = source.current_version
    source.lifecycle_state = MemorySourceLifecycle.TOMBSTONED
    source.current_version = None
    source.tombstoned_at = now
    source.tombstone_reason = str(reason or "source_deleted")[:512]
    source.save(
        update_fields=(
            "lifecycle_state",
            "current_version",
            "tombstoned_at",
            "tombstone_reason",
            "updated_at",
        )
    )
    _outbox_event(
        source=source,
        source_version=previous_version,
        event_type=MemoryOutboxEventType.SOURCE_TOMBSTONED,
        payload={"reason": source.tombstone_reason},
    )
    from .review_summaries import reconcile_derived_visibility_for_source

    reconcile_derived_visibility_for_source(source)
    return {
        "sources_tombstoned": 1,
        "versions_retired": versions,
        "chunks_deactivated": chunks,
    }


@transaction.atomic
def tombstone_source(
    source: MemorySource,
    *,
    reason: str,
    requested_by=None,
    request_id: str = "",
    idempotency_key: Optional[str] = None,
) -> tuple[MemoryDeletionRequest, dict]:
    locked = MemorySource.objects.select_for_update().get(pk=source.pk)
    key = idempotency_key or f"source-tombstone:{locked.pk}"
    deletion, created = MemoryDeletionRequest.objects.get_or_create(
        organization=locked.organization,
        idempotency_key=key,
        defaults={
            "target_type": MemoryDeletionTargetType.SOURCE,
            "target_id": str(locked.pk),
            "reason": str(reason or "source_deleted")[:512],
            "requested_by": requested_by,
            "request_id": str(request_id or "")[:128],
        },
    )
    if not created and deletion.status == MemoryDeletionStatus.COMPLETED:
        return deletion, dict(deletion.result_summary or {})
    now = timezone.now()
    deletion.status = MemoryDeletionStatus.PROCESSING
    deletion.started_at = deletion.started_at or now
    deletion.save(update_fields=("status", "started_at", "updated_at"))
    result = _tombstone_locked_source(locked, reason=reason, now=now)
    deletion.status = MemoryDeletionStatus.COMPLETED
    deletion.result_summary = result
    deletion.completed_at = now
    deletion.save(
        update_fields=("status", "result_summary", "completed_at", "updated_at")
    )
    _refresh_current_state_projection(locked.organization)
    return deletion, result


@transaction.atomic
def tombstone_connection_memory(
    configuration: MemoryConnectionConfiguration,
    *,
    reason: str,
    requested_by=None,
    request_id: str = "",
) -> dict:
    key = f"connection-tombstone:{configuration.pk}"
    deletion, created = MemoryDeletionRequest.objects.get_or_create(
        organization=configuration.organization,
        idempotency_key=key,
        defaults={
            "target_type": MemoryDeletionTargetType.CONNECTION,
            "target_id": str(configuration.pk),
            "reason": str(reason or "connection_deleted")[:512],
            "requested_by": requested_by,
            "request_id": str(request_id or "")[:128],
        },
    )
    if not created and deletion.status == MemoryDeletionStatus.COMPLETED:
        return dict(deletion.result_summary or {})
    now = timezone.now()
    cancel_configuration_runtime(
        configuration,
        reason=reason,
        include_delete=False,
    )
    deletion.status = MemoryDeletionStatus.PROCESSING
    deletion.started_at = deletion.started_at or now
    deletion.save(update_fields=("status", "started_at", "updated_at"))
    totals = {"sources_tombstoned": 0, "versions_retired": 0, "chunks_deactivated": 0}
    for source in MemorySource.objects.select_for_update().filter(
        configuration=configuration
    ):
        result = _tombstone_locked_source(source, reason=reason, now=now)
        for key_name in totals:
            totals[key_name] += result[key_name]
    deletion.status = MemoryDeletionStatus.COMPLETED
    deletion.result_summary = totals
    deletion.completed_at = now
    deletion.save(
        update_fields=("status", "result_summary", "completed_at", "updated_at")
    )
    _refresh_current_state_projection(configuration.organization)
    return totals


@transaction.atomic
def tombstone_organization_memory(
    organization,
    *,
    reason: str,
    requested_by=None,
    request_id: str = "",
) -> dict:
    idempotency_key = f"organization-tombstone:{organization.pk}:{request_id or 'default'}"
    deletion, created = MemoryDeletionRequest.objects.get_or_create(
        organization=organization,
        idempotency_key=idempotency_key,
        defaults={
            "target_type": MemoryDeletionTargetType.ORGANIZATION,
            "target_id": str(organization.pk),
            "reason": str(reason or "organization_data_deleted")[:512],
            "requested_by": requested_by,
            "request_id": str(request_id or "")[:128],
        },
    )
    if not created and deletion.status == MemoryDeletionStatus.COMPLETED:
        return dict(deletion.result_summary or {})
    now = timezone.now()
    deletion.status = MemoryDeletionStatus.PROCESSING
    deletion.started_at = deletion.started_at or now
    deletion.save(update_fields=("status", "started_at", "updated_at"))
    totals = {
        "sources_tombstoned": 0,
        "versions_retired": 0,
        "chunks_deactivated": 0,
        "configurations_disabled": 0,
    }
    for source in MemorySource.objects.select_for_update().filter(
        organization=organization
    ):
        result = _tombstone_locked_source(source, reason=reason, now=now)
        for key_name in ("sources_tombstoned", "versions_retired", "chunks_deactivated"):
            totals[key_name] += result[key_name]
    configurations = MemoryConnectionConfiguration.objects.filter(
        organization=organization,
    ).exclude(lifecycle_state=MemoryConnectionState.DELETED)
    for configuration in configurations.select_for_update():
        cancel_configuration_runtime(
            configuration,
            reason=reason,
            include_delete=True,
        )
    totals["configurations_disabled"] = configurations.update(
        lifecycle_state=MemoryConnectionState.DELETE_PENDING,
        updated_at=now,
    )
    MemorySourceScope.objects.filter(
        configuration__organization=organization,
        selected=True,
    ).update(selected=False, status=MemoryScopeStatus.REMOVED, updated_at=now)
    deletion.status = MemoryDeletionStatus.COMPLETED
    deletion.result_summary = totals
    deletion.completed_at = now
    deletion.save(
        update_fields=("status", "result_summary", "completed_at", "updated_at")
    )
    _refresh_current_state_projection(organization)
    return totals


def open_review_item(
    *,
    organization,
    target,
    review_type: str,
    reason: str,
    idempotency_key: str,
    severity: str = MemoryReviewSeverity.NORMAL,
    assigned_to=None,
    due_at=None,
) -> tuple[MemoryReviewItem, bool]:
    if review_type not in MemoryReviewType.values:
        raise EvidenceKernelError("Review type is invalid.")
    if severity not in MemoryReviewSeverity.values:
        raise EvidenceKernelError("Review severity is invalid.")
    target_organization_id = getattr(target, "organization_id", None)
    if target_organization_id is None and hasattr(target, "source"):
        target_organization_id = target.source.organization_id
    if target_organization_id is None and hasattr(target, "source_version"):
        target_organization_id = target.source_version.source.organization_id
    if target_organization_id != organization.pk:
        raise EvidenceKernelError("Review target belongs to another organisation.")
    content_type = ContentType.objects.get_for_model(target, for_concrete_model=False)
    return MemoryReviewItem.objects.get_or_create(
        organization=organization,
        idempotency_key=_required_text(
            idempotency_key,
            field_name="idempotency_key",
            max_length=255,
        ),
        defaults={
            "review_type": review_type,
            "target_content_type": content_type,
            "target_object_id": str(target.pk),
            "severity": severity,
            "reason": _required_text(reason, field_name="reason", max_length=10000),
            "assigned_to": assigned_to,
            "due_at": due_at,
        },
    )


def create_work_item(
    *,
    organization,
    provider: str,
    task_type: str,
    idempotency_key: str,
    source: Optional[MemorySource] = None,
    source_version: Optional[MemorySourceVersion] = None,
    configuration: Optional[MemoryConnectionConfiguration] = None,
    action_request=None,
    sync_run: Optional[MemorySyncRun] = None,
    payload: Optional[dict] = None,
    max_attempts: int = 5,
) -> tuple[MemoryWorkItem, bool]:
    if provider not in MemoryProvider.values or task_type not in MemoryWorkTaskType.values:
        raise EvidenceKernelError("Work provider or task type is invalid.")
    if source and (
        source.organization_id != organization.pk or source.provider != provider
    ):
        raise EvidenceKernelError("Work source belongs to another organisation or provider.")
    if source_version and (
        source_version.source.organization_id != organization.pk
        or source_version.source.provider != provider
    ):
        raise EvidenceKernelError(
            "Work source version belongs to another organisation or provider."
        )
    if source and source_version and source_version.source_id != source.pk:
        raise EvidenceKernelError("Work source version does not belong to its source.")
    if configuration and (
        configuration.organization_id != organization.pk
        or configuration.provider != provider
    ):
        raise EvidenceKernelError(
            "Work configuration belongs to another organisation or provider."
        )
    if action_request and (
        configuration is None or action_request.configuration_id != configuration.pk
    ):
        raise EvidenceKernelError("Work action does not belong to its configuration.")
    if sync_run and (
        configuration is None
        or sync_run.configuration_id != configuration.pk
        or sync_run.action_request_id != getattr(action_request, "pk", None)
    ):
        raise EvidenceKernelError("Work sync run does not belong to its action.")
    if int(max_attempts) < 1:
        raise EvidenceKernelError("max_attempts must be positive.")
    normalized_key = _required_text(
        idempotency_key,
        field_name="idempotency_key",
        max_length=255,
    )
    existing = MemoryWorkItem.objects.filter(idempotency_key=normalized_key).first()
    if existing is not None:
        if (
            existing.organization_id != organization.pk
            or existing.provider != provider
            or existing.task_type != task_type
            or existing.source_id != getattr(source, "pk", None)
            or existing.source_version_id != getattr(source_version, "pk", None)
            or existing.configuration_id != getattr(configuration, "pk", None)
            or existing.action_request_id != getattr(action_request, "pk", None)
            or existing.sync_run_id != getattr(sync_run, "pk", None)
        ):
            raise EvidenceKernelError(
                "Work idempotency key was already used for different work."
            )
        _ensure_work_lanes(organization=organization, provider=provider)
        return existing, False
    work_item, created = MemoryWorkItem.objects.get_or_create(
        idempotency_key=normalized_key,
        defaults={
            "organization": organization,
            "provider": provider,
            "task_type": task_type,
            "source": source,
            "source_version": source_version,
            "configuration": configuration,
            "action_request": action_request,
            "sync_run": sync_run,
            "payload": _safe_metadata(payload or {}, path="work.payload"),
            "max_attempts": int(max_attempts),
        },
    )
    _ensure_work_lanes(organization=organization, provider=provider)
    return work_item, created


def validate_work_item_for_execution(work_item: MemoryWorkItem) -> MemoryWorkItem:
    """PR6 workers must call this after claiming and immediately before work."""

    if work_item.status not in {MemoryWorkStatus.PENDING, MemoryWorkStatus.PROCESSING}:
        raise EvidenceKernelError("Work item is not executable from its current status.")
    if work_item.source and work_item.source.lifecycle_state != MemorySourceLifecycle.ACTIVE:
        raise EvidenceKernelError("Work source is not active.")
    if work_item.source_version:
        version = work_item.source_version
        if (
            not version.is_current
            or version.tombstoned_at is not None
            or version.source.lifecycle_state != MemorySourceLifecycle.ACTIVE
        ):
            raise EvidenceKernelError("Work source version is no longer current and active.")
    return work_item


def kernel_health_snapshot(*, organization=None) -> dict:
    sources = MemorySource.objects.all()
    chunks = MemoryChunk.objects.all()
    outbox = MemoryOutboxEvent.objects.all()
    work = MemoryWorkItem.objects.all()
    leases = MemoryWorkerLease.objects.all()
    dead_letters = MemoryDeadLetter.objects.all()
    reviews = MemoryReviewItem.objects.all()
    sync_runs = MemorySyncRun.objects.all()
    if organization is not None:
        sources = sources.filter(organization=organization)
        chunks = chunks.filter(source_version__source__organization=organization)
        outbox = outbox.filter(organization=organization)
        work = work.filter(organization=organization)
        leases = leases.filter(work_item__organization=organization)
        dead_letters = dead_letters.filter(organization=organization)
        reviews = reviews.filter(organization=organization)
        sync_runs = sync_runs.filter(organization=organization)

    daily_reports = MemoryDailyReconciliationReport.objects.all()
    if organization is not None:
        daily_reports = daily_reports.filter(organization=organization)
    latest_daily_report = daily_reports.order_by("-report_date", "-started_at").first()

    active_chunks = chunks.filter(active_for_retrieval=True)
    violations = {
        "active_chunk_noncurrent_version": active_chunks.filter(
            source_version__is_current=False
        ).count(),
        "active_chunk_inactive_source": active_chunks.exclude(
            source_version__source__lifecycle_state=MemorySourceLifecycle.ACTIVE
        ).count(),
        "active_chunk_missing_acl": active_chunks.filter(
            Q(source_version__acl_snapshot__isnull=True)
            | Q(source_version__acl_snapshot__is_accessible=False)
            | Q(source_version__acl_snapshot__revoked_at__isnull=False)
        ).count(),
        "active_chunk_no_agent": active_chunks.filter(
            classification=MemoryClassification.NO_AGENT
        ).count(),
        "active_source_pointer_invalid": sources.filter(
            lifecycle_state=MemorySourceLifecycle.ACTIVE
        ).filter(
            Q(current_version__isnull=True)
            | Q(current_version__is_current=False)
            | ~Q(current_version__source_id=F("id"))
        ).count(),
    }
    now = timezone.now()
    status = "ok" if not any(violations.values()) else "error"
    runtime_degraded = (
        dead_letters.filter(resolved_at__isnull=True).exists()
        or outbox.filter(status=MemoryOutboxStatus.FAILED).exists()
        or leases.filter(released_at__isnull=True, expires_at__lte=now).exists()
        or (
            latest_daily_report is not None
            and latest_daily_report.status
            in {
                MemoryDailyReconciliationStatus.DEGRADED,
                MemoryDailyReconciliationStatus.FAILED,
            }
        )
    )
    if status == "ok" and runtime_degraded:
        status = "degraded"
    payload = {
        "status": status,
        "invariant_violations": violations,
        "counts": {
            "sources": sources.count(),
            "active_chunks": active_chunks.count(),
            "pending_outbox": outbox.filter(status=MemoryOutboxStatus.PENDING).count(),
            "failed_outbox": outbox.filter(status=MemoryOutboxStatus.FAILED).count(),
            "pending_work": work.filter(status=MemoryWorkStatus.PENDING).count(),
            "processing_work": work.filter(status=MemoryWorkStatus.PROCESSING).count(),
            "expired_leases": leases.filter(
                released_at__isnull=True,
                expires_at__lte=now,
            ).count(),
            "open_dead_letters": dead_letters.filter(resolved_at__isnull=True).count(),
            "active_sync_runs": sync_runs.filter(
                status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)
            ).count(),
            "open_reviews": reviews.filter(
                status__in=(MemoryReviewStatus.OPEN, MemoryReviewStatus.IN_REVIEW)
            ).count(),
            "cost_deferred_work": work.filter(
                status=MemoryWorkStatus.PENDING,
                last_error__startswith="Daily model cost ",
            ).count(),
        },
    }
    if latest_daily_report is not None:
        from .reconciliation import serialize_daily_reconciliation_report

        payload["daily_reconciliation"] = serialize_daily_reconciliation_report(
            latest_daily_report,
            include_connections=False,
        )
    else:
        payload["daily_reconciliation"] = None
    return payload
