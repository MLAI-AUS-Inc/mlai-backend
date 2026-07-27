from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .action_adapters import (
    ActionAdapterError,
    ActionExecutionResult,
    ActionExecutionUncertain,
    action_adapter_registry,
)
from .connectors.registry import connector_registry
from .models import (
    AgentActionEvent,
    AgentActionEventType,
    AgentActionProposal,
    AgentActionRiskLevel,
    AgentActionStatus,
    AgentActionType,
    MemoryActionStatus,
    MemoryActionType,
    MemoryClaim,
    MemoryClaimStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryEvidence,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySourceLifecycle,
)
from .retrieval import allowed_memory_classifications


IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,255}$")
ACTION_POLICY = {
    AgentActionType.DRAFT_GMAIL: {
        "risk": AgentActionRiskLevel.LOW,
        "approval": False,
    },
    AgentActionType.DRAFT_SLACK_POST: {
        "risk": AgentActionRiskLevel.LOW,
        "approval": False,
    },
    AgentActionType.DRAFT_NOTION_UPDATE: {
        "risk": AgentActionRiskLevel.LOW,
        "approval": False,
    },
    AgentActionType.CREATE_LINEAR_ISSUE: {
        "risk": AgentActionRiskLevel.MEDIUM,
        "approval": True,
    },
    AgentActionType.UPDATE_LINEAR_ISSUE: {
        "risk": AgentActionRiskLevel.HIGH,
        "approval": True,
    },
}


class AgentActionError(ValueError):
    pass


class AgentActionPreconditionStale(AgentActionError):
    pass


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _idempotency_key(value: str, *, field_name: str = "Idempotency-Key") -> str:
    key = str(value or "").strip()
    if not IDEMPOTENCY_PATTERN.fullmatch(key):
        raise AgentActionError(
            f"{field_name} must contain 8-255 safe characters."
        )
    return key


def _uuid_values(values: Iterable, *, field_name: str, maximum: int = 100) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise AgentActionError(f"{field_name} must be a list.")
    normalized = []
    for value in values:
        try:
            item = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AgentActionError(
                f"{field_name} contains an invalid identifier."
            ) from exc
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > maximum:
        raise AgentActionError(f"{field_name} is limited to {maximum} identifiers.")
    return normalized


def _validate_configuration(
    *,
    organization,
    action_type: str,
    configuration_id=None,
) -> MemoryConnectionConfiguration | None:
    adapter = action_adapter_registry.get(action_type)
    if adapter.target_system != "linear":
        if configuration_id not in (None, ""):
            raise AgentActionError("Draft-only actions do not accept a connection.")
        return None
    try:
        configuration = (
            MemoryConnectionConfiguration.objects.select_related(
                "external_connection",
                "google_connection",
            )
            .get(pk=configuration_id, organization=organization)
        )
    except (MemoryConnectionConfiguration.DoesNotExist, ValueError, TypeError) as exc:
        raise AgentActionError(
            "A valid organization Linear connection is required."
        ) from exc
    if (
        configuration.provider != "linear"
        or configuration.lifecycle_state != MemoryConnectionState.ACTIVE
        or configuration.deleted_at is not None
    ):
        raise AgentActionError("Linear actions require an active Linear connection.")
    connection = configuration.connection
    if connection is None or str(getattr(connection, "status", "")) != "connected":
        raise AgentActionError("The Linear connection is not currently connected.")
    if not connector_registry.enablement(organization, "linear")["enabled"]:
        raise AgentActionError(
            "The Linear connector is not enabled for this organization."
        )
    return configuration


def _validate_linear_scope(configuration, payload: dict) -> None:
    if configuration is None:
        return
    project_id = str(payload.get("project_id") or "")
    if not project_id:
        return
    selected = configuration.source_scopes.filter(
        scope_type="project",
        external_id=project_id,
        selected=True,
        status=MemoryScopeStatus.SELECTED,
    ).exists()
    if not selected:
        raise AgentActionError(
            "The Linear project is outside this connection's approved scope."
        )


def _validate_evidence(
    *,
    organization,
    authorization,
    claim_ids: list[str],
    source_ids: list[str],
) -> tuple[list[str], list[str]]:
    allowed = set(allowed_memory_classifications(authorization))
    claims = list(
        MemoryClaim.objects.filter(
            organization=organization,
            pk__in=claim_ids,
        )
    )
    if len(claims) != len(claim_ids):
        raise AgentActionError("Action evidence includes an unknown claim.")
    if any(
        claim.status != MemoryClaimStatus.ACTIVE
        or claim.classification not in allowed
        for claim in claims
    ):
        raise AgentActionError(
            "Action evidence includes inactive or inaccessible claims."
        )

    derived_source_ids = set(
        str(value)
        for value in MemoryEvidence.objects.filter(claim__in=claims).values_list(
            "source_id", flat=True
        )
    )
    all_source_ids = sorted(set(source_ids) | derived_source_ids)
    sources = list(
        MemorySource.objects.filter(
            organization=organization,
            pk__in=all_source_ids,
        ).select_related("current_version", "current_version__acl_snapshot")
    )
    if len(sources) != len(all_source_ids):
        raise AgentActionError("Action evidence includes an unknown source.")
    for source in sources:
        version = source.current_version
        acl = getattr(version, "acl_snapshot", None) if version else None
        if (
            source.lifecycle_state != MemorySourceLifecycle.ACTIVE
            or source.access_revoked_at is not None
            or version is None
            or version.classification not in allowed
            or version.tombstoned_at is not None
            or acl is None
            or not acl.is_accessible
            or acl.revoked_at is not None
        ):
            raise AgentActionError(
                "Action evidence includes inaccessible or stale sources."
            )
    return sorted(str(claim.pk) for claim in claims), all_source_ids


def _event(
    proposal: AgentActionProposal,
    event_type: str,
    *,
    actor=None,
    request_id: str = "",
    payload_hash: str = "",
    metadata: dict | None = None,
) -> AgentActionEvent:
    safe_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key)
        not in {
            "input_payload",
            "result_payload",
            "precondition_snapshot",
            "reversal_payload",
            "body",
            "text",
            "description",
        }
    }
    return AgentActionEvent.objects.create(
        proposal=proposal,
        event_type=event_type,
        actor_user=actor,
        request_id=str(request_id or "")[:128],
        payload_hash=str(payload_hash or "")[:64],
        metadata=safe_metadata,
    )


def refresh_action_preconditions(proposal: AgentActionProposal) -> tuple[dict, str]:
    try:
        snapshot = action_adapter_registry.get(
            proposal.action_type
        ).refresh_preconditions(proposal)
    except ActionAdapterError as exc:
        raise AgentActionError(str(exc)) from exc
    if not isinstance(snapshot, dict):
        raise AgentActionError("Action adapter returned an invalid precondition snapshot.")
    return snapshot, canonical_hash(snapshot)


def _creation_hash(
    *,
    action_type,
    configuration_id,
    payload,
    claim_ids,
    source_ids,
) -> str:
    return canonical_hash(
        {
            "action_type": action_type,
            "configuration_id": str(configuration_id or ""),
            "input_payload": payload,
            "evidence_claim_ids": claim_ids,
            "evidence_source_ids": source_ids,
        }
    )


def _requested_slack_id(actor) -> str:
    identity = getattr(actor, "identity", None)
    if identity and identity.provider == "slack":
        return str(identity.external_user_id or "")[:32]
    return ""


def create_action_proposal(
    *,
    organization,
    authorization,
    actor,
    action_type: str,
    input_payload,
    idempotency_key: str,
    configuration_id=None,
    evidence_claim_ids=None,
    evidence_source_ids=None,
    request_id: str = "",
) -> tuple[AgentActionProposal, bool]:
    action_type = str(action_type or "").strip()
    policy = ACTION_POLICY.get(action_type)
    if policy is None:
        raise AgentActionError(
            "This action type is unsupported. Finance, payment, contract, role, "
            "governance, send-email, and direct-post actions are excluded."
        )
    key = _idempotency_key(idempotency_key)
    try:
        adapter = action_adapter_registry.get(action_type)
        payload = adapter.validate_payload(input_payload)
    except ActionAdapterError as exc:
        raise AgentActionError(str(exc)) from exc
    configuration = _validate_configuration(
        organization=organization,
        action_type=action_type,
        configuration_id=configuration_id,
    )
    _validate_linear_scope(configuration, payload)
    claim_ids = _uuid_values(
        evidence_claim_ids or [],
        field_name="evidence_claim_ids",
    )
    source_ids = _uuid_values(
        evidence_source_ids or [],
        field_name="evidence_source_ids",
    )
    claim_ids, source_ids = _validate_evidence(
        organization=organization,
        authorization=authorization,
        claim_ids=claim_ids,
        source_ids=source_ids,
    )
    creation_hash = _creation_hash(
        action_type=action_type,
        configuration_id=getattr(configuration, "pk", None),
        payload=payload,
        claim_ids=claim_ids,
        source_ids=source_ids,
    )
    existing = AgentActionProposal.objects.filter(
        organization=organization,
        idempotency_key=key,
    ).first()
    if existing is not None:
        if existing.creation_request_hash != creation_hash:
            raise AgentActionError(
                "Idempotency-Key was already used for a different action proposal."
            )
        return existing, False

    proposal = AgentActionProposal(
        organization=organization,
        configuration=configuration,
        requested_by=actor.user,
        requested_by_slack_id=_requested_slack_id(actor),
        action_type=action_type,
        target_system=adapter.target_system,
        input_payload=payload,
        input_hash=canonical_hash(payload),
        evidence_claim_ids=claim_ids,
        evidence_source_ids=source_ids,
        risk_level=policy["risk"],
        requires_approval=policy["approval"],
        status=(
            AgentActionStatus.AWAITING_APPROVAL
            if policy["approval"]
            else AgentActionStatus.PROPOSED
        ),
        idempotency_key=key,
        creation_request_hash=creation_hash,
    )
    snapshot, snapshot_hash = refresh_action_preconditions(proposal)
    proposal.precondition_snapshot = snapshot
    proposal.precondition_hash = snapshot_hash
    proposal.preconditions_refreshed_at = timezone.now()
    proposal.full_clean()
    try:
        with transaction.atomic():
            proposal.save()
            _event(
                proposal,
                AgentActionEventType.PROPOSED,
                actor=actor.user,
                request_id=request_id,
                payload_hash=proposal.input_hash,
                metadata={
                    "action_type": action_type,
                    "target_system": proposal.target_system,
                    "risk_level": proposal.risk_level,
                    "requires_approval": proposal.requires_approval,
                    "evidence_claim_count": len(claim_ids),
                    "evidence_source_count": len(source_ids),
                },
            )
            _event(
                proposal,
                AgentActionEventType.PRECONDITIONS_REFRESHED,
                actor=actor.user,
                request_id=request_id,
                payload_hash=snapshot_hash,
                metadata={"stage": "proposal"},
            )
    except IntegrityError:
        existing = AgentActionProposal.objects.filter(
            organization=organization,
            idempotency_key=key,
        ).first()
        if existing and existing.creation_request_hash == creation_hash:
            return existing, False
        raise AgentActionError(
            "Idempotency-Key was already used for a different action proposal."
        )
    return proposal, True


def _assert_evidence_still_valid(proposal, authorization) -> None:
    _validate_evidence(
        organization=proposal.organization,
        authorization=authorization,
        claim_ids=list(proposal.evidence_claim_ids or []),
        source_ids=list(proposal.evidence_source_ids or []),
    )


def _assert_proposal_target_still_enabled(proposal) -> None:
    configuration = _validate_configuration(
        organization=proposal.organization,
        action_type=proposal.action_type,
        configuration_id=proposal.configuration_id,
    )
    _validate_linear_scope(configuration, proposal.input_payload)


def approve_action_proposal(
    *,
    proposal: AgentActionProposal,
    authorization,
    actor,
    idempotency_key: str,
    request_id: str = "",
) -> tuple[AgentActionProposal, bool]:
    if not proposal.requires_approval:
        raise AgentActionError("This draft-only action does not require approval.")
    key = _idempotency_key(idempotency_key)
    current = AgentActionProposal.objects.filter(pk=proposal.pk).first()
    if (
        current is not None
        and current.status == AgentActionStatus.APPROVED
        and current.approval_idempotency_key == key
    ):
        return current, False
    _assert_proposal_target_still_enabled(proposal)
    _assert_evidence_still_valid(proposal, authorization)
    snapshot, snapshot_hash = refresh_action_preconditions(proposal)
    with transaction.atomic():
        locked = AgentActionProposal.objects.select_for_update().get(pk=proposal.pk)
        if (
            locked.status == AgentActionStatus.APPROVED
            and locked.approval_idempotency_key == key
        ):
            return locked, False
        if locked.status not in {
            AgentActionStatus.AWAITING_APPROVAL,
            AgentActionStatus.STALE,
        }:
            raise AgentActionError("This action is not awaiting approval.")
        if (
            getattr(settings, "ORG_MEMORY_ACTION_REQUIRE_SEPARATE_APPROVER", True)
            and locked.requested_by_id == actor.user.pk
        ):
            raise AgentActionError(
                "Action approval requires a different authorized reviewer."
            )
        locked.precondition_snapshot = snapshot
        locked.precondition_hash = snapshot_hash
        locked.preconditions_refreshed_at = timezone.now()
        locked.status = AgentActionStatus.APPROVED
        locked.approved_by = actor.user
        locked.approved_at = timezone.now()
        locked.approval_idempotency_key = key
        locked.rejected_by = None
        locked.rejected_at = None
        locked.rejection_reason = ""
        locked.error_text = ""
        locked.save(
            update_fields=(
                "precondition_snapshot",
                "precondition_hash",
                "preconditions_refreshed_at",
                "status",
                "approved_by",
                "approved_at",
                "approval_idempotency_key",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
                "error_text",
                "updated_at",
            )
        )
        _event(
            locked,
            AgentActionEventType.PRECONDITIONS_REFRESHED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=snapshot_hash,
            metadata={"stage": "approval"},
        )
        _event(
            locked,
            AgentActionEventType.APPROVED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=locked.input_hash,
            metadata={"risk_level": locked.risk_level},
        )
    return locked, True


def reject_action_proposal(
    *,
    proposal: AgentActionProposal,
    actor,
    reason: str,
    idempotency_key: str,
    request_id: str = "",
) -> tuple[AgentActionProposal, bool]:
    key = _idempotency_key(idempotency_key)
    reason = str(reason or "").strip()
    if not reason or len(reason) > 512:
        raise AgentActionError("A rejection reason of up to 512 characters is required.")
    with transaction.atomic():
        locked = AgentActionProposal.objects.select_for_update().get(pk=proposal.pk)
        if (
            locked.status == AgentActionStatus.REJECTED
            and locked.approval_idempotency_key == key
        ):
            return locked, False
        if locked.status not in {
            AgentActionStatus.PROPOSED,
            AgentActionStatus.AWAITING_APPROVAL,
            AgentActionStatus.STALE,
        }:
            raise AgentActionError("This action can no longer be rejected.")
        locked.status = AgentActionStatus.REJECTED
        locked.rejected_by = actor.user
        locked.rejected_at = timezone.now()
        locked.rejection_reason = reason
        locked.approval_idempotency_key = key
        locked.save(
            update_fields=(
                "status",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
                "approval_idempotency_key",
                "updated_at",
            )
        )
        _event(
            locked,
            AgentActionEventType.REJECTED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=locked.input_hash,
            metadata={"reason_code": "reviewer_rejected"},
        )
    return locked, True


def _mark_stale(
    proposal: AgentActionProposal,
    *,
    actor,
    request_id: str,
    live_hash: str,
) -> None:
    proposal.status = AgentActionStatus.STALE
    proposal.approved_by = None
    proposal.approved_at = None
    proposal.approval_idempotency_key = ""
    proposal.error_text = "Live preconditions changed after approval."
    proposal.save(
        update_fields=(
            "status",
            "approved_by",
            "approved_at",
            "approval_idempotency_key",
            "error_text",
            "updated_at",
        )
    )
    _event(
        proposal,
        AgentActionEventType.APPROVAL_INVALIDATED,
        actor=actor,
        request_id=request_id,
        payload_hash=live_hash,
        metadata={"reason": "live_precondition_changed"},
    )


def _enqueue_result_ingestion(
    proposal: AgentActionProposal,
    *,
    external_id: str,
    actor,
    request_id: str,
) -> MemorySourceActionRequest | None:
    if proposal.configuration_id is None or not external_id:
        return None
    key = f"agent-action:{proposal.pk}"
    action, action_created = MemorySourceActionRequest.objects.get_or_create(
        configuration=proposal.configuration,
        idempotency_key=key,
        defaults={
            "action": MemoryActionType.SYNC,
            "status": MemoryActionStatus.PENDING,
            "requested_by": actor,
            "request_id": str(request_id or "")[:128],
            "parameters": {
                "agent_action_proposal_id": str(proposal.pk),
                "target_external_id": external_id,
            },
        },
    )
    MemoryConnectionConfiguration.objects.filter(pk=proposal.configuration_id).update(
        last_sync_requested_at=timezone.now()
    )
    if action_created:
        MemorySourceAuditEvent.objects.create(
            configuration=proposal.configuration,
            organization=proposal.organization,
            actor_user=actor,
            event_type="agent_action_result_ingestion_requested",
            request_id=str(request_id or "")[:128],
            metadata={
                "proposal_id": str(proposal.pk),
                "action_request_id": str(action.pk),
                "target_system": proposal.target_system,
            },
        )
    return action


def execute_action_proposal(
    *,
    proposal: AgentActionProposal,
    authorization,
    actor,
    idempotency_key: str,
    request_id: str = "",
) -> tuple[AgentActionProposal, bool]:
    key = _idempotency_key(idempotency_key)
    current = AgentActionProposal.objects.filter(pk=proposal.pk).first()
    if (
        current is not None
        and current.status == AgentActionStatus.COMPLETED
        and current.execution_idempotency_key == key
    ):
        return current, False
    _assert_proposal_target_still_enabled(proposal)
    _assert_evidence_still_valid(proposal, authorization)
    snapshot, snapshot_hash = refresh_action_preconditions(proposal)
    stale = False
    with transaction.atomic():
        locked = AgentActionProposal.objects.select_for_update().select_related(
            "configuration",
            "configuration__external_connection",
            "configuration__google_connection",
        ).get(pk=proposal.pk)
        if (
            locked.status == AgentActionStatus.COMPLETED
            and locked.execution_idempotency_key == key
        ):
            return locked, False
        expected = (
            AgentActionStatus.APPROVED
            if locked.requires_approval
            else AgentActionStatus.PROPOSED
        )
        if locked.status != expected:
            raise AgentActionError("This action is not executable in its current state.")
        if locked.requires_approval and snapshot_hash != locked.precondition_hash:
            _mark_stale(
                locked,
                actor=actor.user,
                request_id=request_id,
                live_hash=snapshot_hash,
            )
            stale = True
        else:
            locked.precondition_snapshot = snapshot
            locked.precondition_hash = snapshot_hash
            locked.preconditions_refreshed_at = timezone.now()
            locked.status = AgentActionStatus.EXECUTING
            locked.execution_idempotency_key = key
            locked.execution_attempts += 1
            locked.executed_by = actor.user
            locked.error_text = ""
            locked.save(
                update_fields=(
                    "precondition_snapshot",
                    "precondition_hash",
                    "preconditions_refreshed_at",
                    "status",
                    "execution_idempotency_key",
                    "execution_attempts",
                    "executed_by",
                    "error_text",
                    "updated_at",
                )
            )
            _event(
                locked,
                AgentActionEventType.EXECUTION_STARTED,
                actor=actor.user,
                request_id=request_id,
                payload_hash=locked.input_hash,
                metadata={"execution_attempt": locked.execution_attempts},
            )
    if stale:
        raise AgentActionPreconditionStale(
            "Live preconditions changed; the action requires fresh approval."
        )

    try:
        result = action_adapter_registry.get(locked.action_type).execute(locked)
        if not isinstance(result, ActionExecutionResult):
            raise ActionAdapterError("Action adapter returned an invalid execution result.")
    except Exception as exc:
        uncertain = isinstance(exc, ActionExecutionUncertain)
        with transaction.atomic():
            failed = AgentActionProposal.objects.select_for_update().get(pk=locked.pk)
            if failed.status == AgentActionStatus.EXECUTING:
                failed.status = AgentActionStatus.FAILED
                failed.error_text = (
                    f"{exc.__class__.__name__}: {exc}"
                )[:10000]
                failed.result_payload = {
                    "manual_reconciliation_required": uncertain,
                    "retry_safe": False,
                }
                failed.save(
                    update_fields=(
                        "status",
                        "error_text",
                        "result_payload",
                        "updated_at",
                    )
                )
                _event(
                    failed,
                    AgentActionEventType.EXECUTION_FAILED,
                    actor=actor.user,
                    request_id=request_id,
                    payload_hash=failed.input_hash,
                    metadata={
                        "error_type": exc.__class__.__name__,
                        "manual_reconciliation_required": uncertain,
                    },
                )
        raise AgentActionError(
            "Action execution failed and will not be retried automatically: "
            f"{exc.__class__.__name__}."
        ) from exc

    with transaction.atomic():
        completed = AgentActionProposal.objects.select_for_update().get(pk=locked.pk)
        if completed.status != AgentActionStatus.EXECUTING:
            raise AgentActionError("Action execution state changed unexpectedly.")
        ingestion = _enqueue_result_ingestion(
            completed,
            external_id=result.external_id,
            actor=actor.user,
            request_id=request_id,
        )
        completed.status = AgentActionStatus.COMPLETED
        completed.result_payload = result.result
        completed.reversal_payload = result.reversal_payload
        completed.reversal_supported = bool(result.reversal_payload)
        completed.executed_at = timezone.now()
        completed.ingestion_action_request = ingestion
        completed.error_text = ""
        completed.save(
            update_fields=(
                "status",
                "result_payload",
                "reversal_payload",
                "reversal_supported",
                "executed_at",
                "ingestion_action_request",
                "error_text",
                "updated_at",
            )
        )
        _event(
            completed,
            AgentActionEventType.EXECUTION_COMPLETED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=canonical_hash(result.result),
            metadata={
                "target_system": completed.target_system,
                "reversal_supported": completed.reversal_supported,
            },
        )
        if ingestion:
            _event(
                completed,
                AgentActionEventType.INGESTION_ENQUEUED,
                actor=actor.user,
                request_id=request_id,
                payload_hash=canonical_hash(str(ingestion.pk)),
                metadata={"action_request_id": str(ingestion.pk)},
            )
    return completed, True


def reverse_action_proposal(
    *,
    proposal: AgentActionProposal,
    actor,
    idempotency_key: str,
    request_id: str = "",
) -> tuple[AgentActionProposal, bool]:
    key = _idempotency_key(idempotency_key)
    _assert_proposal_target_still_enabled(proposal)
    try:
        action_adapter_registry.get(proposal.action_type).validate_reversal(proposal)
    except ActionAdapterError as exc:
        raise AgentActionError(str(exc)) from exc
    with transaction.atomic():
        locked = AgentActionProposal.objects.select_for_update().select_related(
            "configuration",
            "configuration__external_connection",
            "configuration__google_connection",
        ).get(pk=proposal.pk)
        if (
            locked.status == AgentActionStatus.REVERSED
            and locked.reversal_idempotency_key == key
        ):
            return locked, False
        if (
            locked.status != AgentActionStatus.COMPLETED
            or not locked.reversal_supported
        ):
            raise AgentActionError("This completed action has no supported reversal.")
        locked.status = AgentActionStatus.REVERSING
        locked.reversal_idempotency_key = key
        locked.save(
            update_fields=("status", "reversal_idempotency_key", "updated_at")
        )
        _event(
            locked,
            AgentActionEventType.REVERSAL_STARTED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=canonical_hash(locked.reversal_payload),
        )
    try:
        result = action_adapter_registry.get(locked.action_type).reverse(locked)
    except Exception as exc:
        with transaction.atomic():
            failed = AgentActionProposal.objects.select_for_update().get(pk=locked.pk)
            if failed.status == AgentActionStatus.REVERSING:
                failed.status = AgentActionStatus.COMPLETED
                failed.error_text = f"Reversal failed: {exc.__class__.__name__}: {exc}"[
                    :10000
                ]
                failed.save(update_fields=("status", "error_text", "updated_at"))
                _event(
                    failed,
                    AgentActionEventType.REVERSAL_FAILED,
                    actor=actor.user,
                    request_id=request_id,
                    metadata={"error_type": exc.__class__.__name__},
                )
        raise AgentActionError(
            f"Action reversal failed: {exc.__class__.__name__}."
        ) from exc
    with transaction.atomic():
        reversed_action = AgentActionProposal.objects.select_for_update().get(
            pk=locked.pk
        )
        reversed_action.status = AgentActionStatus.REVERSED
        reversed_action.reversed_by = actor.user
        reversed_action.reversed_at = timezone.now()
        reversed_action.result_payload = {
            **dict(reversed_action.result_payload or {}),
            "reversal": result,
        }
        reversed_action.error_text = ""
        reversed_action.save(
            update_fields=(
                "status",
                "reversed_by",
                "reversed_at",
                "result_payload",
                "error_text",
                "updated_at",
            )
        )
        _event(
            reversed_action,
            AgentActionEventType.REVERSED,
            actor=actor.user,
            request_id=request_id,
            payload_hash=canonical_hash(result),
        )
    return reversed_action, True
