from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Iterable, Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    GoogleConnection,
)
from startup_updates.models import UserStartupBinding

from .connectors.base import DryRunResult, ScopePage, SourcePreview
from .connectors.registry import connector_registry
from .drive_inventory import DriveInventoryError
from .governance import GovernancePolicyError, assert_provider_ingestion_allowed
from .models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryClassification,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryPreviewStatus,
    MemoryScopeStatus,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySourcePolicy,
    MemorySourcePreview,
    MemorySourceScope,
)
from .safety import UnsafeMemoryMetadata, sanitize_memory_metadata


class SourceControlError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_source_control_request"):
        self.code = code
        super().__init__(message)


def _safe_control_metadata(value, *, path="metadata", depth=0):
    """Keep control-plane JSON bounded and free of credentials/source bodies."""
    try:
        return sanitize_memory_metadata(value, path=path, depth=depth)
    except UnsafeMemoryMetadata as exc:
        raise SourceControlError(str(exc)) from exc


def _canonical_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _policy_payload(policy: Optional[MemorySourcePolicy]) -> dict:
    if policy is None:
        return {}
    return {
        "id": policy.pk,
        "policy_key": policy.policy_key,
        "provider": policy.provider,
        "scope_type": policy.scope_type,
        "classification": policy.classification,
        "authority_score": policy.authority_score,
        "volatility": policy.volatility,
        "stale_after_seconds": policy.stale_after_seconds,
        "allowed_memory_kinds": policy.allowed_memory_kinds,
        "auto_activation_rules": policy.auto_activation_rules,
        "review_rules": policy.review_rules,
        "retention_policy": policy.retention_policy,
        "historical_cutoff": (
            policy.historical_cutoff.isoformat() if policy.historical_cutoff else None
        ),
        "updated_at": policy.updated_at.isoformat(),
    }


def selected_scope_snapshot(configuration: MemoryConnectionConfiguration) -> list[dict]:
    rows = configuration.source_scopes.filter(
        selected=True,
        status=MemoryScopeStatus.SELECTED,
    ).select_related("policy")
    return [
        {
            "id": scope.pk,
            "scope_type": scope.scope_type,
            "external_id": scope.external_id,
            "name": scope.name,
            "canonical_url": scope.canonical_url,
            "classification": scope.default_classification,
            "policy": _policy_payload(scope.policy),
            "updated_at": scope.updated_at.isoformat(),
        }
        for scope in rows.order_by("scope_type", "external_id")
    ]


def configuration_fingerprint(configuration: MemoryConnectionConfiguration) -> str:
    return _canonical_hash(
        {
            "provider": configuration.provider,
            "default_classification": configuration.default_classification,
            "allowed_memory_kinds": configuration.allowed_memory_kinds,
            "historical_cutoff": configuration.historical_cutoff,
            "retention_policy": configuration.retention_policy,
            "configuration": configuration.configuration,
            "default_policy": _policy_payload(configuration.default_policy),
            "scopes": selected_scope_snapshot(configuration),
        }
    )


def serialize_scope(scope: MemorySourceScope) -> dict:
    return {
        "id": scope.pk,
        "scope_type": scope.scope_type,
        "external_id": scope.external_id,
        "name": scope.name,
        "canonical_url": scope.canonical_url,
        "selected": scope.selected,
        "status": scope.status,
        "classification": scope.default_classification,
        "policy_id": scope.policy_id,
        "metadata": scope.metadata,
        "last_seen_at": scope.last_seen_at,
    }


def serialize_preview(preview: Optional[MemorySourcePreview]) -> Optional[dict]:
    if preview is None:
        return None
    return {
        "id": preview.pk,
        "version": preview.version,
        "status": preview.status,
        "is_current": preview.is_current,
        "selection_fingerprint": preview.selection_fingerprint,
        "summary": preview.summary,
        "warnings": preview.warnings,
        "dry_run_summary": preview.dry_run_summary,
        "dry_run_completed_at": preview.dry_run_completed_at,
        "created_at": preview.created_at,
    }


def serialize_configuration(configuration: MemoryConnectionConfiguration) -> dict:
    connection = configuration.connection
    current_preview = configuration.previews.filter(is_current=True).first()
    return {
        "id": str(configuration.pk),
        "organization_id": configuration.organization_id,
        "provider": configuration.provider,
        "lifecycle_state": configuration.lifecycle_state,
        "connection_type": "gmail" if configuration.google_connection_id else "external",
        "underlying_connection_id": connection.pk,
        "account_label": str(
            getattr(connection, "account_label", "")
            or getattr(connection, "google_email", "")
            or ""
        ),
        "default_classification": configuration.default_classification,
        "allowed_memory_kinds": configuration.allowed_memory_kinds,
        "historical_cutoff": configuration.historical_cutoff,
        "retention_policy": configuration.retention_policy,
        "selected_scope_count": configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).count(),
        "approved_at": configuration.approved_at,
        "last_discovered_at": configuration.last_discovered_at,
        "last_previewed_at": configuration.last_previewed_at,
        "last_dry_run_at": configuration.last_dry_run_at,
        "last_backfill_requested_at": configuration.last_backfill_requested_at,
        "last_sync_requested_at": configuration.last_sync_requested_at,
        "last_successful_sync_at": configuration.last_successful_sync_at,
        "next_scheduled_sync_at": configuration.next_scheduled_sync_at,
        "sync_cursor_present": bool(configuration.sync_cursor),
        "sync_checkpoint_present": bool(configuration.sync_checkpoint),
        "last_error": configuration.last_error,
        "current_preview": serialize_preview(current_preview),
    }


def _audit(
    configuration,
    *,
    actor,
    authorization,
    event_type: str,
    request_id: str,
    from_state: str = "",
    to_state: str = "",
    metadata: Optional[dict] = None,
):
    MemorySourceAuditEvent.objects.create(
        organization=configuration.organization,
        configuration=configuration,
        actor_identity=getattr(actor, "identity", None),
        actor_membership=getattr(authorization, "membership", None),
        actor_user=getattr(actor, "user", None),
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        request_id=request_id,
        metadata=metadata or {},
    )


def _invalidate_approval(configuration: MemoryConnectionConfiguration, *, state: str):
    from .kernel import cancel_configuration_runtime, revoke_configuration_sources

    cancel_configuration_runtime(
        configuration,
        reason="configuration_approval_invalidated",
        include_delete=False,
    )

    configuration.previews.filter(is_current=True).update(
        is_current=False,
        status=MemoryPreviewStatus.STALE,
    )
    configuration.approved_preview = None
    configuration.approved_by = None
    configuration.approved_at = None
    configuration.last_dry_run_at = None
    configuration.lifecycle_state = state
    revoke_configuration_sources(
        configuration,
        reason="source_configuration_changed",
    )


def attach_connection(
    *,
    organization,
    actor,
    authorization,
    provider: str,
    external_connection_id=None,
    google_connection_id=None,
    request_id: str = "",
) -> tuple[MemoryConnectionConfiguration, bool]:
    provider = str(provider).strip().lower()
    connector_registry.get(provider)
    if bool(external_connection_id) == bool(google_connection_id):
        raise SourceControlError(
            "Exactly one external_connection_id or google_connection_id is required."
        )

    defaults = {
        "organization": organization,
        "provider": provider,
        "created_by": actor.user,
    }
    if external_connection_id:
        connection = ExternalServiceConnection.objects.filter(
            pk=external_connection_id,
            organization=organization,
            provider=provider,
        ).exclude(status=ExternalServiceConnectionStatus.DISCONNECTED).first()
        if connection is None:
            raise SourceControlError(
                "An active organisation-owned connector connection was not found.",
                code="connection_not_found",
            )
        configuration, created = MemoryConnectionConfiguration.objects.get_or_create(
            external_connection=connection,
            defaults=defaults,
        )
    else:
        if provider != "gmail":
            raise SourceControlError("google_connection_id is only valid for Gmail.")
        connection = GoogleConnection.objects.filter(pk=google_connection_id).first()
        if connection is None or not UserStartupBinding.objects.filter(
            organization=organization,
            user=connection.user,
        ).exists():
            raise SourceControlError(
                "An organisation-bound Google connection was not found.",
                code="connection_not_found",
            )
        configuration, created = MemoryConnectionConfiguration.objects.get_or_create(
            google_connection=connection,
            defaults=defaults,
        )
    if configuration.organization_id != organization.pk or configuration.provider != provider:
        raise SourceControlError("Connection is already bound to another configuration.")
    _audit(
        configuration,
        actor=actor,
        authorization=authorization,
        event_type="connection_attached" if created else "connection_attach_replayed",
        request_id=request_id,
        to_state=configuration.lifecycle_state,
        metadata={"provider": provider},
    )
    return configuration, created


def get_configuration(configuration_id, organization, *, for_update=False):
    queryset = MemoryConnectionConfiguration.objects.select_related(
        "organization",
        "external_connection",
        "google_connection",
        "default_policy",
        "approved_preview",
    )
    if for_update:
        # Nullable select_related() joins must not be part of the lock target.
        # PostgreSQL rejects FOR UPDATE on the nullable side of an outer join,
        # so lock only the configuration row while still hydrating its related
        # objects for the caller.
        queryset = queryset.select_for_update(of=("self",))
    configuration = queryset.filter(pk=configuration_id, organization=organization).first()
    if configuration is None:
        raise SourceControlError("Memory connection was not found.", code="not_found")
    return configuration


def discover_scopes(
    configuration,
    *,
    actor,
    authorization,
    request_id: str,
    cursor: Optional[str] = None,
) -> ScopePage:
    if configuration.lifecycle_state in {
        MemoryConnectionState.DELETE_PENDING,
        MemoryConnectionState.DELETED,
    }:
        raise SourceControlError("Deleted connections cannot discover scopes.")
    page = connector_registry.get(configuration.provider).discover_scopes(
        configuration,
        cursor=cursor,
    )
    if not isinstance(page, ScopePage):
        raise SourceControlError("Connector returned an invalid scope page.")
    now = timezone.now()
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        for descriptor in page.scopes:
            if not descriptor.scope_type or not descriptor.external_id:
                raise SourceControlError("Connector returned an invalid scope descriptor.")
            MemorySourceScope.objects.update_or_create(
                configuration=locked,
                scope_type=str(descriptor.scope_type)[:32],
                external_id=str(descriptor.external_id)[:512],
                defaults={
                    "name": str(descriptor.name)[:512],
                    "canonical_url": str(descriptor.canonical_url)[:1024],
                    "metadata": _safe_control_metadata(dict(descriptor.metadata)),
                    "last_seen_at": now,
                },
            )
        locked.last_discovered_at = now
        locked.save(update_fields=("last_discovered_at", "updated_at"))
        action = MemorySourceActionRequest.objects.create(
            configuration=locked,
            action=MemoryActionType.DISCOVER,
            status=MemoryActionStatus.COMPLETED,
            requested_by=actor.user,
            request_id=request_id,
            result_summary={"scope_count": len(page.scopes)},
            completed_at=now,
        )
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="scopes_discovered",
            request_id=request_id,
            metadata={"scope_count": len(page.scopes), "action_id": str(action.pk)},
        )
    return page


def _validate_scope(provider: str, row: Mapping):
    scope_type = str(row.get("scope_type") or "").strip()
    external_id = str(row.get("external_id") or "").strip()
    if not scope_type or not external_id:
        raise SourceControlError("Every scope requires scope_type and external_id.")
    if provider == "slack" and (
        scope_type == "direct_message" or external_id.startswith("D")
    ):
        raise SourceControlError("Slack direct messages are excluded from organisational memory.")
    if provider in {"stripe", "xero", "luma"}:
        from .connectors.structured_aggregates import aggregate_scope_ids

        allowed_aggregates = aggregate_scope_ids(provider)
        if provider in {"stripe", "xero"} and scope_type != "aggregate":
            raise SourceControlError(
                "Finance memory supports explicit aggregate scopes only."
            )
        if provider == "luma" and scope_type not in {"aggregate", "event"}:
            raise SourceControlError(
                "Luma memory supports explicit aggregate and event scopes only."
            )
        if scope_type == "aggregate" and external_id not in allowed_aggregates:
            raise SourceControlError(
                f"Unsupported {provider} memory aggregate: {external_id}."
            )
    if provider == "google_drive":
        from .drive_inventory import DriveInventoryError, validate_drive_id

        if scope_type not in {"folder", "shared_drive"}:
            raise SourceControlError(
                "Google Drive memory supports folder and shared_drive scopes only."
            )
        try:
            validate_drive_id(external_id)
        except DriveInventoryError as exc:
            raise SourceControlError(str(exc)) from exc
    if row.get("selected", True) and row.get("classification") == MemoryClassification.NO_AGENT:
        raise SourceControlError("A no_agent scope cannot be selected for ingestion.")
    return scope_type, external_id


def select_scopes(
    configuration,
    rows: Iterable[Mapping],
    *,
    actor,
    authorization,
    request_id: str,
):
    rows = list(rows)
    if not rows:
        raise SourceControlError("At least one scope selection is required.")
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if locked.lifecycle_state in {
            MemoryConnectionState.DELETE_PENDING,
            MemoryConnectionState.DELETED,
        }:
            raise SourceControlError("Deleted connections cannot be configured.")
        selected_keys = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise SourceControlError("Scope selections must be objects.")
            scope_type, external_id = _validate_scope(locked.provider, row)
            selected = bool(row.get("selected", True))
            policy = None
            policy_id = row.get("policy_id")
            if policy_id:
                policy = MemorySourcePolicy.objects.filter(
                    pk=policy_id,
                    organization=locked.organization,
                    provider=locked.provider,
                    is_active=True,
                ).first()
                if policy is None:
                    raise SourceControlError("Scope policy was not found.")
            classification = str(
                row.get("classification") or locked.default_classification
            )
            if classification not in MemoryClassification.values:
                raise SourceControlError("Scope classification is invalid.")
            existing_scope = MemorySourceScope.objects.filter(
                configuration=locked,
                scope_type=scope_type,
                external_id=external_id,
            ).first()
            metadata = dict(existing_scope.metadata or {}) if existing_scope else {}
            if isinstance(row.get("metadata"), Mapping):
                metadata.update(_safe_control_metadata(dict(row["metadata"])))
            metadata["configured_by_operator"] = True
            scope, _ = MemorySourceScope.objects.update_or_create(
                configuration=locked,
                scope_type=scope_type,
                external_id=external_id,
                defaults={
                    "name": str(row.get("name") or external_id)[:512],
                    "canonical_url": str(row.get("canonical_url") or "")[:1024],
                    "selected": selected,
                    "status": (
                        MemoryScopeStatus.SELECTED if selected else MemoryScopeStatus.EXCLUDED
                    ),
                    "default_classification": classification,
                    "policy": policy,
                    "metadata": metadata,
                    "last_seen_at": timezone.now(),
                },
            )
            if selected:
                selected_keys.add((scope_type, external_id))
        MemorySourceScope.objects.filter(configuration=locked, selected=True).exclude(
            pk__in=[
                scope.pk
                for scope in MemorySourceScope.objects.filter(configuration=locked)
                if (scope.scope_type, scope.external_id) in selected_keys
            ]
        ).update(selected=False, status=MemoryScopeStatus.EXCLUDED)
        if not selected_keys:
            raise SourceControlError("At least one scope must remain selected.")
        from_state = locked.lifecycle_state
        _invalidate_approval(locked, state=MemoryConnectionState.SCOPED)
        locked.save(
            update_fields=(
                "approved_preview",
                "approved_by",
                "approved_at",
                "last_dry_run_at",
                "lifecycle_state",
                "updated_at",
            )
        )
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="scopes_selected",
            request_id=request_id,
            from_state=from_state,
            to_state=locked.lifecycle_state,
            metadata={"selected_scope_count": len(selected_keys)},
        )
    return get_configuration(configuration.pk, configuration.organization)


def _current_preview(configuration):
    return configuration.previews.filter(
        is_current=True,
        status=MemoryPreviewStatus.READY,
    ).first()


def create_preview(
    configuration,
    *,
    actor,
    authorization,
    request_id: str,
) -> MemorySourcePreview:
    scopes = list(
        configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).select_related("policy")
    )
    if not scopes:
        raise SourceControlError("Select at least one scope before previewing.")
    fingerprint = configuration_fingerprint(configuration)
    try:
        result = connector_registry.get(configuration.provider).preview(
            configuration,
            scopes,
            _policy_payload(configuration.default_policy),
        )
    except GovernancePolicyError as exc:
        raise SourceControlError(str(exc), code="governance_denied") from exc
    except (ValueError, DriveInventoryError) as exc:
        raise SourceControlError(str(exc), code="connector_validation_failed") from exc
    if not isinstance(result, SourcePreview):
        raise SourceControlError("Connector returned an invalid preview.")
    summary = _safe_control_metadata(dict(result.summary), path="preview.summary")
    if summary.get("content_activated") is not False:
        raise SourceControlError("Preview attempted to activate content.")

    now = timezone.now()
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if configuration_fingerprint(locked) != fingerprint:
            raise SourceControlError("Source configuration changed during preview.")
        locked.previews.filter(is_current=True).update(
            is_current=False,
            status=MemoryPreviewStatus.STALE,
        )
        version = (locked.previews.aggregate(value=Max("version"))["value"] or 0) + 1
        preview = MemorySourcePreview.objects.create(
            configuration=locked,
            version=version,
            selection_fingerprint=fingerprint,
            selection_snapshot=selected_scope_snapshot(locked),
            policy_snapshot=_policy_payload(locked.default_policy),
            summary=summary,
            warnings=_safe_control_metadata(list(result.warnings), path="preview.warnings"),
            requested_by=actor.user,
        )
        from_state = locked.lifecycle_state
        locked.lifecycle_state = MemoryConnectionState.PREVIEWED
        locked.approved_preview = None
        locked.approved_by = None
        locked.approved_at = None
        locked.last_dry_run_at = None
        locked.last_previewed_at = now
        locked.save(
            update_fields=(
                "lifecycle_state",
                "approved_preview",
                "approved_by",
                "approved_at",
                "last_dry_run_at",
                "last_previewed_at",
                "updated_at",
            )
        )
        MemorySourceActionRequest.objects.create(
            configuration=locked,
            action=MemoryActionType.PREVIEW,
            status=MemoryActionStatus.COMPLETED,
            requested_by=actor.user,
            request_id=request_id,
            result_summary=summary,
            completed_at=now,
        )
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="preview_completed",
            request_id=request_id,
            from_state=from_state,
            to_state=locked.lifecycle_state,
            metadata={"preview_id": preview.pk, "preview_version": preview.version},
        )
    return preview


def run_dry_run(
    configuration,
    *,
    actor,
    authorization,
    request_id: str,
) -> MemorySourcePreview:
    preview = _current_preview(configuration)
    fingerprint = configuration_fingerprint(configuration)
    if preview is None or preview.selection_fingerprint != fingerprint:
        raise SourceControlError("A current preview is required before dry-run.")
    scopes = list(
        configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).select_related("policy")
    )
    try:
        result = connector_registry.get(configuration.provider).dry_run(
            configuration,
            scopes,
            preview.policy_snapshot,
        )
    except GovernancePolicyError as exc:
        raise SourceControlError(str(exc), code="governance_denied") from exc
    except (ValueError, DriveInventoryError) as exc:
        raise SourceControlError(str(exc), code="connector_validation_failed") from exc
    if not isinstance(result, DryRunResult):
        raise SourceControlError("Connector returned an invalid dry-run result.")
    summary = _safe_control_metadata(dict(result.summary), path="dry_run.summary")
    if summary.get("active_memory_created") is not False:
        raise SourceControlError("Dry-run attempted to create active memory.")
    now = timezone.now()
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        current = _current_preview(locked)
        if (
            current is None
            or current.pk != preview.pk
            or configuration_fingerprint(locked) != fingerprint
        ):
            raise SourceControlError("Source configuration changed during dry-run.")
        current.dry_run_summary = summary
        current.warnings = list(current.warnings or []) + _safe_control_metadata(
            list(result.warnings),
            path="dry_run.warnings",
        )
        current.dry_run_completed_at = now
        current.dry_run_by = actor.user
        current.save(
            update_fields=(
                "dry_run_summary",
                "warnings",
                "dry_run_completed_at",
                "dry_run_by",
            )
        )
        from_state = locked.lifecycle_state
        locked.lifecycle_state = MemoryConnectionState.DRY_RUN_READY
        locked.last_dry_run_at = now
        locked.save(update_fields=("lifecycle_state", "last_dry_run_at", "updated_at"))
        MemorySourceActionRequest.objects.create(
            configuration=locked,
            action=MemoryActionType.DRY_RUN,
            status=MemoryActionStatus.COMPLETED,
            requested_by=actor.user,
            request_id=request_id,
            result_summary=summary,
            completed_at=now,
        )
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="dry_run_completed",
            request_id=request_id,
            from_state=from_state,
            to_state=locked.lifecycle_state,
            metadata={"preview_id": current.pk},
        )
    return current


def approve_configuration(
    configuration,
    *,
    actor,
    authorization,
    request_id: str,
):
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        preview = _current_preview(locked)
        if (
            preview is None
            or preview.selection_fingerprint != configuration_fingerprint(locked)
            or preview.dry_run_completed_at is None
            or locked.lifecycle_state != MemoryConnectionState.DRY_RUN_READY
        ):
            raise SourceControlError("A current completed dry-run is required before approval.")
        if (preview.dry_run_summary or {}).get("approval_ready") is False:
            raise SourceControlError(
                "The dry-run is partial and cannot be approved; increase its reviewed inventory ceilings."
            )
        now = timezone.now()
        from_state = locked.lifecycle_state
        locked.lifecycle_state = MemoryConnectionState.APPROVED
        locked.approved_preview = preview
        locked.approved_by = actor.user
        locked.approved_at = now
        locked.save(
            update_fields=(
                "lifecycle_state",
                "approved_preview",
                "approved_by",
                "approved_at",
                "updated_at",
            )
        )
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="connection_approved",
            request_id=request_id,
            from_state=from_state,
            to_state=locked.lifecycle_state,
            metadata={"preview_id": preview.pk},
        )
    return get_configuration(configuration.pk, configuration.organization)


def _assert_provider_ingestion_enabled(configuration):
    enablement = connector_registry.enablement(
        configuration.organization,
        configuration.provider,
    )
    if not enablement["enabled"]:
        raise SourceControlError(
            "Provider ingestion is disabled: " + ", ".join(enablement["reasons"]),
            code="provider_disabled",
        )
    try:
        assert_provider_ingestion_allowed(
            configuration.provider,
            production=bool(getattr(settings, "IS_PRODUCTION_ENV", False)),
        )
    except GovernancePolicyError as exc:
        raise SourceControlError(str(exc), code="governance_denied") from exc


def assert_configuration_ready_for_backfill(configuration):
    preview = configuration.approved_preview
    if (
        configuration.lifecycle_state != MemoryConnectionState.APPROVED
        or preview is None
        or not preview.is_current
        or preview.status != MemoryPreviewStatus.READY
        or preview.dry_run_completed_at is None
        or preview.selection_fingerprint != configuration_fingerprint(configuration)
    ):
        raise SourceControlError(
            "Backfill requires a current preview, completed dry-run, and explicit approval.",
            code="backfill_not_approved",
        )
    _assert_provider_ingestion_enabled(configuration)


def _create_pending_action(
    configuration,
    *,
    action: str,
    actor,
    request_id: str,
    idempotency_key: Optional[str],
    scope_external_ids: Optional[list[str]] = None,
    parameters: Optional[dict] = None,
):
    if idempotency_key:
        existing = MemorySourceActionRequest.objects.filter(
            configuration=configuration,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            if existing.action != action:
                raise SourceControlError("Idempotency key was already used for another action.")
            return existing, False
    return MemorySourceActionRequest.objects.create(
        configuration=configuration,
        action=action,
        requested_by=actor.user,
        request_id=request_id,
        idempotency_key=idempotency_key or None,
        scope_external_ids=scope_external_ids or [],
        parameters=parameters or {},
    ), True


def request_backfill(
    configuration,
    *,
    actor,
    authorization,
    request_id: str,
    idempotency_key: Optional[str] = None,
):
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if idempotency_key:
            existing = MemorySourceActionRequest.objects.filter(
                configuration=locked,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                if existing.action != MemoryActionType.BACKFILL:
                    raise SourceControlError(
                        "Idempotency key was already used for another action."
                    )
                return existing, False
        assert_configuration_ready_for_backfill(locked)
        action, created = _create_pending_action(
            locked,
            action=MemoryActionType.BACKFILL,
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
            parameters={"approved_preview_id": locked.approved_preview_id},
        )
        if created:
            now = timezone.now()
            from_state = locked.lifecycle_state
            locked.lifecycle_state = MemoryConnectionState.BACKFILL_PENDING
            locked.last_backfill_requested_at = now
            locked.save(
                update_fields=(
                    "lifecycle_state",
                    "last_backfill_requested_at",
                    "updated_at",
                )
            )
            _audit(
                locked,
                actor=actor,
                authorization=authorization,
                event_type="backfill_requested",
                request_id=request_id,
                from_state=from_state,
                to_state=locked.lifecycle_state,
                metadata={"action_id": str(action.pk)},
            )
    return action, created


def request_runtime_action(
    configuration,
    *,
    action: str,
    actor,
    authorization,
    request_id: str,
    idempotency_key: Optional[str] = None,
    scope_external_ids: Optional[list[str]] = None,
):
    if action not in {
        MemoryActionType.SYNC,
        MemoryActionType.REPROCESS,
        MemoryActionType.REFRESH_PERMISSIONS,
    }:
        raise SourceControlError("Unsupported runtime action.")
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if locked.lifecycle_state != MemoryConnectionState.ACTIVE:
            raise SourceControlError("Runtime actions require an active connection.")
        _assert_provider_ingestion_enabled(locked)
        selected_ids = set(
            locked.source_scopes.filter(
                selected=True,
                status=MemoryScopeStatus.SELECTED,
            ).values_list("external_id", flat=True)
        )
        requested_ids = set(scope_external_ids or [])
        if requested_ids - selected_ids:
            raise SourceControlError("Runtime action includes an unselected scope.")
        action_row, created = _create_pending_action(
            locked,
            action=action,
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
            scope_external_ids=sorted(requested_ids),
        )
        if created:
            if action == MemoryActionType.SYNC:
                locked.last_sync_requested_at = timezone.now()
                locked.save(update_fields=("last_sync_requested_at", "updated_at"))
            _audit(
                locked,
                actor=actor,
                authorization=authorization,
                event_type=f"{action}_requested",
                request_id=request_id,
                metadata={"action_id": str(action_row.pk), "scope_count": len(requested_ids)},
            )
    return action_row, created


def pause_configuration(configuration, *, actor, authorization, request_id: str):
    from .kernel import suspend_configuration_runtime

    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if locked.lifecycle_state in {
            MemoryConnectionState.DRAFT,
            MemoryConnectionState.SCOPED,
            MemoryConnectionState.PREVIEWED,
            MemoryConnectionState.DRY_RUN_READY,
            MemoryConnectionState.PAUSED,
            MemoryConnectionState.DELETE_PENDING,
            MemoryConnectionState.DELETED,
        }:
            raise SourceControlError("Connection cannot be paused from its current state.")
        from_state = locked.lifecycle_state
        suspend_configuration_runtime(locked)
        locked.state_before_pause = from_state
        locked.lifecycle_state = MemoryConnectionState.PAUSED
        locked.save(update_fields=("state_before_pause", "lifecycle_state", "updated_at"))
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="connection_paused",
            request_id=request_id,
            from_state=from_state,
            to_state=locked.lifecycle_state,
        )
    return get_configuration(configuration.pk, configuration.organization)


def resume_configuration(configuration, *, actor, authorization, request_id: str):
    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if locked.lifecycle_state != MemoryConnectionState.PAUSED:
            raise SourceControlError("Only a paused connection can be resumed.")
        _assert_provider_ingestion_enabled(locked)
        target = locked.state_before_pause
        if target not in {
            MemoryConnectionState.APPROVED,
            MemoryConnectionState.BACKFILL_PENDING,
            MemoryConnectionState.ACTIVE,
            MemoryConnectionState.ERROR,
        }:
            target = MemoryConnectionState.APPROVED
        from_state = locked.lifecycle_state
        locked.lifecycle_state = target
        locked.state_before_pause = ""
        locked.save(update_fields=("lifecycle_state", "state_before_pause", "updated_at"))
        _audit(
            locked,
            actor=actor,
            authorization=authorization,
            event_type="connection_resumed",
            request_id=request_id,
            from_state=from_state,
            to_state=target,
        )
    return get_configuration(configuration.pk, configuration.organization)


def request_delete(configuration, *, actor, authorization, request_id: str, idempotency_key=None):
    from .kernel import tombstone_connection_memory

    with transaction.atomic():
        locked = get_configuration(configuration.pk, configuration.organization, for_update=True)
        if locked.lifecycle_state == MemoryConnectionState.DELETED:
            raise SourceControlError("Connection is already deleted.")
        action, created = _create_pending_action(
            locked,
            action=MemoryActionType.DELETE,
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        if created:
            from_state = locked.lifecycle_state
            locked.lifecycle_state = MemoryConnectionState.DELETE_PENDING
            locked.source_scopes.filter(selected=True).update(
                selected=False,
                status=MemoryScopeStatus.REMOVED,
            )
            tombstone_connection_memory(
                locked,
                reason="connection_delete_requested",
                requested_by=actor.user,
                request_id=request_id,
            )
            locked.save(update_fields=("lifecycle_state", "updated_at"))
            _audit(
                locked,
                actor=actor,
                authorization=authorization,
                event_type="connection_delete_requested",
                request_id=request_id,
                from_state=from_state,
                to_state=locked.lifecycle_state,
                metadata={"action_id": str(action.pk)},
            )
    return action, created


def validate_action_for_execution(action: MemorySourceActionRequest):
    """Mandatory worker-side gate; queued state alone never grants execution."""

    configuration = action.configuration
    if action.status not in {MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING}:
        raise SourceControlError("Only pending or resumable running actions may execute.")
    if action.action == MemoryActionType.BACKFILL:
        if configuration.lifecycle_state != MemoryConnectionState.BACKFILL_PENDING:
            raise SourceControlError("Backfill connection is not pending.")
        preview = configuration.approved_preview
        if (
            preview is None
            or not preview.is_current
            or preview.dry_run_completed_at is None
            or preview.selection_fingerprint != configuration_fingerprint(configuration)
        ):
            raise SourceControlError("Backfill approval is stale or missing.")
        _assert_provider_ingestion_enabled(configuration)
    elif action.action in {
        MemoryActionType.SYNC,
        MemoryActionType.REPROCESS,
        MemoryActionType.REFRESH_PERMISSIONS,
    }:
        if configuration.lifecycle_state != MemoryConnectionState.ACTIVE:
            raise SourceControlError("Runtime action connection is not active.")
        _assert_provider_ingestion_enabled(configuration)
    elif action.action == MemoryActionType.DELETE:
        if configuration.lifecycle_state != MemoryConnectionState.DELETE_PENDING:
            raise SourceControlError("Deletion is no longer pending.")
    else:
        raise SourceControlError("Action type is not worker-executable.")
    return configuration
