from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace
from typing import Mapping

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    CapabilityGrantEffect,
    MemoryPilotDeployment,
    MemoryPilotDeploymentState,
    MemoryPilotSuspensionReason,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationIdentityProvider,
    OrganizationMembership,
    OrganizationRoleAssignment,
)
from .pilot_readiness import (
    PUBLIC_PILOT_ADMIN_CONTEXT,
    pilot_approval_manifest_hash,
    validate_pilot_approval_manifest,
)


_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class PilotDeploymentError(RuntimeError):
    pass


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime_timezone.utc)


def pilot_allowlist_key() -> tuple[str, bytes]:
    version = str(
        getattr(settings, "ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION", "") or ""
    ).strip()
    secret = str(
        getattr(settings, "ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY", "") or ""
    ).encode("utf-8")
    if not _VERSION_RE.fullmatch(version):
        raise PilotDeploymentError("Pilot allowlist key version is invalid.")
    if len(secret) < 32:
        raise PilotDeploymentError(
            "Pilot allowlist HMAC key must contain at least 32 bytes."
        )
    return version, secret


def resolve_pilot_operator(organization, email):
    operator = get_user_model().objects.filter(
        email__iexact=str(email or "").strip(),
        is_active=True,
    ).first()
    if operator is None or not operator_has_manage_sources(
        organization,
        operator,
    ):
        raise PilotDeploymentError("Pilot deployment operator is unavailable.")
    return operator


def hash_pilot_reference(
    organization,
    *,
    reference_type: str,
    reference: str,
    secret: bytes | None = None,
) -> str:
    if reference_type not in {"actor", "context"}:
        raise PilotDeploymentError("Pilot allowlist reference type is invalid.")
    if secret is None:
        _version, secret = pilot_allowlist_key()
    message = "|".join(
        (
            "org-memory-pilot-allowlist-v1",
            str(organization.pk),
            reference_type,
            str(reference),
        )
    )
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def approval_allowlist_hashes(
    organization,
    approval_manifest: Mapping,
) -> dict:
    version, secret = pilot_allowlist_key()
    actor_refs = approval_manifest.get("pilot_admin_refs") or ()
    context_refs = approval_manifest.get("allowed_slack_contexts") or ()
    return {
        "key_version": version,
        "actor_hashes": sorted(
            {
                hash_pilot_reference(
                    organization,
                    reference_type="actor",
                    reference=reference,
                    secret=secret,
                )
                for reference in actor_refs
            }
        ),
        "context_hashes": sorted(
            {
                hash_pilot_reference(
                    organization,
                    reference_type="context",
                    reference=reference,
                    secret=secret,
                )
                for reference in context_refs
            }
        ),
    }


def _active_at(queryset, at):
    return queryset.filter(valid_from__lte=at).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gt=at)
    )


def operator_has_manage_sources(organization, operator, *, at=None) -> bool:
    at = at or timezone.now()
    if operator is None or not operator.is_active:
        return False
    membership = OrganizationMembership.objects.filter(
        organization=organization,
        user=operator,
    ).first()
    if membership is None or not membership.is_effective_at(at):
        return False
    role_ids = _active_at(
        OrganizationRoleAssignment.objects.filter(
            membership=membership,
            role__organization=organization,
            role__is_active=True,
        ),
        at,
    ).values_list("role_id", flat=True)
    effects = set(
        _active_at(
            OrganizationCapabilityGrant.objects.filter(
                Q(membership=membership)
                | Q(role_id__in=role_ids, role__organization=organization),
                capability__key="manage_sources",
                capability__is_active=True,
            ),
            at,
        ).values_list("effect", flat=True)
    )
    return (
        CapabilityGrantEffect.ALLOW in effects
        and CapabilityGrantEffect.DENY not in effects
    )


def _operator_is_pilot_actor(organization, operator, approval_manifest) -> bool:
    approved_ids = {
        str(value).split(":", 1)[1]
        for value in approval_manifest.get("pilot_admin_refs") or ()
        if isinstance(value, str) and value.startswith("slack:")
    }
    return OrganizationIdentity.objects.filter(
        organization=organization,
        user=operator,
        provider=OrganizationIdentityProvider.SLACK,
        external_user_id__in=approved_ids,
        is_active=True,
        verified_at__isnull=False,
    ).exists()


def _validate_operator(
    organization,
    operator,
    *,
    approval_manifest=None,
    at=None,
) -> None:
    if not operator_has_manage_sources(organization, operator, at=at):
        raise PilotDeploymentError(
            "Pilot deployment operator lacks an active management capability."
        )
    if approval_manifest is not None and _operator_is_pilot_actor(
        organization,
        operator,
        approval_manifest,
    ):
        raise PilotDeploymentError(
            "Pilot deployment operator must be independent of pilot actors."
        )


def _approved_counts(approval_manifest):
    providers = approval_manifest.get("approved_providers") or ()
    source_scopes = approval_manifest.get("approved_source_scopes") or {}
    return len(providers), sum(
        len(values)
        for values in source_scopes.values()
        if isinstance(values, list)
    )


def _deployment_matches(
    deployment,
    *,
    approval_hash,
    review_due_at,
    allowlist,
    provider_count,
    source_scope_count,
) -> bool:
    return bool(
        deployment.approval_manifest_hash == approval_hash
        and deployment.approval_review_due_at == review_due_at
        and deployment.allowlist_key_version == allowlist["key_version"]
        and deployment.actor_ref_hashes == allowlist["actor_hashes"]
        and deployment.context_ref_hashes == allowlist["context_hashes"]
        and deployment.approved_provider_count == provider_count
        and deployment.approved_source_scope_count == source_scope_count
    )


def _validate_readiness_report(
    report,
    *,
    approval_hash,
    expected_stage,
) -> None:
    if (
        not isinstance(report, Mapping)
        or report.get("ready") is not True
        or report.get("blockers")
        or report.get("stage") != expected_stage
        or report.get("approval_manifest_hash") != approval_hash
    ):
        raise PilotDeploymentError(
            "Pilot deployment readiness evidence is invalid."
        )


def _validate_approval_manifest(
    organization,
    approval_manifest: Mapping,
    *,
    now,
) -> None:
    if validate_pilot_approval_manifest(
        approval_manifest,
        organization_domain=organization.domain,
        now=now,
    ):
        raise PilotDeploymentError("Pilot approval manifest is invalid.")


@transaction.atomic
def stage_pilot_deployment(
    *,
    organization,
    approval_manifest: Mapping,
    readiness_report: Mapping,
    operator,
    idempotency_key: str,
    now=None,
) -> tuple[MemoryPilotDeployment, bool]:
    now = now or timezone.now()
    if not _IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
        raise PilotDeploymentError("Pilot staging idempotency key is invalid.")
    _validate_approval_manifest(
        organization,
        approval_manifest,
        now=now,
    )
    _validate_operator(
        organization,
        operator,
        approval_manifest=approval_manifest,
        at=now,
    )
    review_due_at = _parse_timestamp(approval_manifest.get("review_due_at"))
    if review_due_at is None or review_due_at <= now:
        raise PilotDeploymentError("Pilot approval is expired.")
    approval_hash = pilot_approval_manifest_hash(approval_manifest)
    _validate_readiness_report(
        readiness_report,
        approval_hash=approval_hash,
        expected_stage="preflight_read_only_pilot",
    )
    allowlist = approval_allowlist_hashes(organization, approval_manifest)
    provider_count, source_scope_count = _approved_counts(approval_manifest)

    existing_idempotent = MemoryPilotDeployment.objects.select_for_update().filter(
        organization=organization,
        stage_idempotency_key=idempotency_key,
    ).first()
    if existing_idempotent is not None:
        if not _deployment_matches(
            existing_idempotent,
            approval_hash=approval_hash,
            review_due_at=review_due_at,
            allowlist=allowlist,
            provider_count=provider_count,
            source_scope_count=source_scope_count,
        ):
            raise PilotDeploymentError(
                "Pilot staging idempotency key conflicts with existing state."
            )
        return existing_idempotent, False

    matching = MemoryPilotDeployment.objects.select_for_update().filter(
        organization=organization,
        approval_manifest_hash=approval_hash,
        allowlist_key_version=allowlist["key_version"],
    ).first()
    if matching is not None:
        if matching.state == MemoryPilotDeploymentState.SUSPENDED:
            raise PilotDeploymentError(
                "A suspended pilot deployment cannot be restaged."
            )
        if not _deployment_matches(
            matching,
            approval_hash=approval_hash,
            review_due_at=review_due_at,
            allowlist=allowlist,
            provider_count=provider_count,
            source_scope_count=source_scope_count,
        ):
            raise PilotDeploymentError(
                "Pilot approval conflicts with an existing deployment."
            )
        return matching, False

    if MemoryPilotDeployment.objects.select_for_update().filter(
        organization=organization,
        state=MemoryPilotDeploymentState.STAGED,
    ).exists():
        raise PilotDeploymentError(
            "Another pilot deployment is already staged."
        )
    deployment = MemoryPilotDeployment(
        organization=organization,
        state=MemoryPilotDeploymentState.STAGED,
        approval_manifest_hash=approval_hash,
        approval_review_due_at=review_due_at,
        allowlist_key_version=allowlist["key_version"],
        actor_ref_hashes=allowlist["actor_hashes"],
        context_ref_hashes=allowlist["context_hashes"],
        approved_provider_count=provider_count,
        approved_source_scope_count=source_scope_count,
        stage_idempotency_key=idempotency_key,
        staged_by=operator,
        staged_at=now,
    )
    try:
        deployment.full_clean()
        deployment.save()
    except ValidationError as exc:
        raise PilotDeploymentError(
            "Pilot deployment violates the staged runtime policy."
        ) from exc
    return deployment, True


@transaction.atomic
def activate_pilot_deployment(
    *,
    organization,
    approval_manifest: Mapping,
    readiness_report: Mapping,
    operator,
    idempotency_key: str,
    now=None,
) -> tuple[MemoryPilotDeployment, bool]:
    now = now or timezone.now()
    if not _IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
        raise PilotDeploymentError("Pilot activation idempotency key is invalid.")
    _validate_approval_manifest(
        organization,
        approval_manifest,
        now=now,
    )
    _validate_operator(
        organization,
        operator,
        approval_manifest=approval_manifest,
        at=now,
    )
    approval_hash = pilot_approval_manifest_hash(approval_manifest)
    _validate_readiness_report(
        readiness_report,
        approval_hash=approval_hash,
        expected_stage="live_read_only_pilot",
    )
    if not bool(getattr(settings, "ORG_MEMORY_QUERY_API_ENABLED", False)):
        raise PilotDeploymentError("Pilot query API is not enabled.")
    allowlist = approval_allowlist_hashes(organization, approval_manifest)
    provider_count, source_scope_count = _approved_counts(approval_manifest)
    review_due_at = _parse_timestamp(approval_manifest.get("review_due_at"))
    deployment = MemoryPilotDeployment.objects.select_for_update().filter(
        organization=organization,
        approval_manifest_hash=approval_hash,
        allowlist_key_version=allowlist["key_version"],
    ).first()
    if deployment is None:
        raise PilotDeploymentError("Matching staged pilot deployment is missing.")
    if deployment.state == MemoryPilotDeploymentState.ACTIVE:
        if deployment.activation_idempotency_key == idempotency_key:
            return deployment, False
        raise PilotDeploymentError("Pilot deployment is already active.")
    if deployment.state != MemoryPilotDeploymentState.STAGED:
        raise PilotDeploymentError("Pilot deployment cannot be activated.")
    if (
        not _deployment_matches(
            deployment,
            approval_hash=approval_hash,
            review_due_at=review_due_at,
            allowlist=allowlist,
            provider_count=provider_count,
            source_scope_count=source_scope_count,
        )
        or deployment.approval_review_due_at <= now
    ):
        raise PilotDeploymentError("Staged pilot deployment is stale.")
    if not deployment.staged_by_id or deployment.staged_by_id == operator.pk:
        raise PilotDeploymentError(
            "Pilot activation requires an independent operator."
        )

    current_active = MemoryPilotDeployment.objects.select_for_update().filter(
        organization=organization,
        state=MemoryPilotDeploymentState.ACTIVE,
    ).exclude(pk=deployment.pk).first()
    if current_active is not None:
        current_active.state = MemoryPilotDeploymentState.SUSPENDED
        current_active.suspended_by = operator
        current_active.suspended_at = now
        current_active.suspension_reason = MemoryPilotSuspensionReason.SUPERSEDED
        current_active.full_clean()
        current_active.save(
            update_fields=(
                "state",
                "suspended_by",
                "suspended_at",
                "suspension_reason",
                "updated_at",
            )
        )

    deployment.state = MemoryPilotDeploymentState.ACTIVE
    deployment.activation_idempotency_key = idempotency_key
    deployment.activated_by = operator
    deployment.activated_at = now
    try:
        deployment.full_clean()
        deployment.save(
            update_fields=(
                "state",
                "activation_idempotency_key",
                "activated_by",
                "activated_at",
                "updated_at",
            )
        )
    except ValidationError as exc:
        raise PilotDeploymentError(
            "Pilot deployment violates the active runtime policy."
        ) from exc
    return deployment, True


@transaction.atomic
def suspend_pilot_deployments(
    *,
    organization,
    operator,
    reason: str,
    now=None,
) -> int:
    now = now or timezone.now()
    _validate_operator(organization, operator, at=now)
    if reason not in MemoryPilotSuspensionReason.values:
        raise PilotDeploymentError("Pilot suspension reason is invalid.")
    rows = list(
        MemoryPilotDeployment.objects.select_for_update().filter(
            organization=organization,
            state__in=(
                MemoryPilotDeploymentState.STAGED,
                MemoryPilotDeploymentState.ACTIVE,
            ),
        )
    )
    for deployment in rows:
        deployment.state = MemoryPilotDeploymentState.SUSPENDED
        deployment.suspended_by = operator
        deployment.suspended_at = now
        deployment.suspension_reason = reason
        deployment.full_clean()
        deployment.save(
            update_fields=(
                "state",
                "suspended_by",
                "suspended_at",
                "suspension_reason",
                "updated_at",
            )
        )
    return len(rows)


def _digest_allowed(expected, candidates) -> bool:
    return any(
        hmac.compare_digest(str(expected), str(candidate))
        for candidate in candidates
    )


def actor_has_active_pilot_access(
    actor,
    *,
    now=None,
    allowed_surfaces=("admin_roo",),
) -> bool:
    now = now or timezone.now()
    if actor is None or actor.surface not in set(allowed_surfaces):
        return False
    try:
        version, secret = pilot_allowlist_key()
    except PilotDeploymentError:
        return False
    deployment = MemoryPilotDeployment.objects.filter(
        organization=actor.organization,
        state=MemoryPilotDeploymentState.ACTIVE,
        approval_review_due_at__gt=now,
        allowlist_key_version=version,
    ).first()
    if deployment is None:
        return False
    actor_digest = hash_pilot_reference(
        actor.organization,
        reference_type="actor",
        reference=f"slack:{actor.slack_user_id}",
        secret=secret,
    )
    if not _digest_allowed(actor_digest, deployment.actor_ref_hashes):
        return False
    context_refs = [f"channel:{actor.slack_channel_id}"]
    if str(actor.slack_channel_id or "").startswith("D"):
        context_refs.append(f"dm:{actor.slack_user_id}")
    elif str(actor.slack_channel_id or "").startswith("C"):
        # This capability is deliberately actor-scoped: the actor digest above
        # must match before a public-channel context can ever be considered.
        context_refs.append(PUBLIC_PILOT_ADMIN_CONTEXT)
    return any(
        _digest_allowed(
            hash_pilot_reference(
                actor.organization,
                reference_type="context",
                reference=reference,
                secret=secret,
            ),
            deployment.context_ref_hashes,
        )
        for reference in context_refs
    )


def pilot_deployment_readiness(
    *,
    organization,
    approval_manifest: Mapping,
    live: bool,
    allow_staged_activation: bool = False,
    allow_runtime_staging: bool = False,
    now=None,
) -> dict:
    now = now or timezone.now()
    try:
        allowlist = approval_allowlist_hashes(organization, approval_manifest)
    except PilotDeploymentError:
        return {
            "status": "block",
            "code": "runtime_allowlist_key_invalid",
            "metrics": {
                "active": 0,
                "staged": 0,
                "matching": 0,
            },
        }
    approval_hash = pilot_approval_manifest_hash(approval_manifest)
    provider_count, source_scope_count = _approved_counts(approval_manifest)
    review_due_at = _parse_timestamp(approval_manifest.get("review_due_at"))
    open_rows = list(
        MemoryPilotDeployment.objects.filter(
            organization=organization,
            state__in=(
                MemoryPilotDeploymentState.STAGED,
                MemoryPilotDeploymentState.ACTIVE,
            ),
        )
    )
    matching = [
        row
        for row in open_rows
        if _deployment_matches(
            row,
            approval_hash=approval_hash,
            review_due_at=review_due_at,
            allowlist=allowlist,
            provider_count=provider_count,
            source_scope_count=source_scope_count,
        )
        and row.approval_review_due_at > now
    ]
    required_states = (
        {
            MemoryPilotDeploymentState.STAGED,
            MemoryPilotDeploymentState.ACTIVE,
        }
        if allow_staged_activation
        else {MemoryPilotDeploymentState.ACTIVE}
    )
    required_matches = [
        row for row in matching if row.state in required_states
    ]
    metrics = {
        "active": sum(
            row.state == MemoryPilotDeploymentState.ACTIVE for row in open_rows
        ),
        "staged": sum(
            row.state == MemoryPilotDeploymentState.STAGED for row in open_rows
        ),
        "matching": len(matching),
    }
    if live:
        return {
            "status": "pass" if required_matches else "block",
            "code": (
                (
                    "runtime_pilot_binding_active"
                    if any(
                        row.state == MemoryPilotDeploymentState.ACTIVE
                        for row in required_matches
                    )
                    else "runtime_pilot_binding_staged"
                )
                if required_matches
                else "runtime_pilot_binding_missing"
            ),
            "metrics": metrics,
        }
    if allow_runtime_staging and not matching:
        return {
            "status": "warn",
            "code": "runtime_pilot_staging_pending",
            "metrics": metrics,
        }
    if matching:
        return {
            "status": "pass",
            "code": "runtime_pilot_binding_staged",
            "metrics": metrics,
        }
    if open_rows:
        return {
            "status": "block",
            "code": "runtime_pilot_binding_mismatch",
            "metrics": metrics,
        }
    return {
        "status": "warn",
        "code": "runtime_pilot_not_staged",
        "metrics": metrics,
    }


def pilot_deployment_report(organization, *, now=None) -> dict:
    now = now or timezone.now()
    rows = list(organization.memory_pilot_deployments.all())
    active = next(
        (
            row
            for row in rows
            if row.state == MemoryPilotDeploymentState.ACTIVE
        ),
        None,
    )
    try:
        key_version, _secret = pilot_allowlist_key()
        key_configured = True
    except PilotDeploymentError:
        key_version = ""
        key_configured = False
    query_api_enabled = bool(
        getattr(settings, "ORG_MEMORY_QUERY_API_ENABLED", False)
    )
    effective = bool(
        active
        and query_api_enabled
        and key_configured
        and active.allowlist_key_version == key_version
        and active.approval_review_due_at > now
    )
    return {
        "schema_version": "org-memory-pilot-deployment-v1",
        "organization_domain": organization.domain,
        "generated_at": now.isoformat(),
        "effective": effective,
        "query_api_enabled": query_api_enabled,
        "active": sum(
            row.state == MemoryPilotDeploymentState.ACTIVE for row in rows
        ),
        "staged": sum(
            row.state == MemoryPilotDeploymentState.STAGED for row in rows
        ),
        "suspended": sum(
            row.state == MemoryPilotDeploymentState.SUSPENDED for row in rows
        ),
        "allowlist_key_configured": key_configured,
        "allowlist_key_version_match": bool(
            active
            and key_configured
            and active.allowlist_key_version == key_version
        ),
        "approval_current": bool(
            active and active.approval_review_due_at > now
        ),
        "approved_actor_count": len(active.actor_ref_hashes) if active else 0,
        "approved_context_count": len(active.context_ref_hashes) if active else 0,
        "approved_provider_count": (
            active.approved_provider_count if active else 0
        ),
        "approved_source_scope_count": (
            active.approved_source_scope_count if active else 0
        ),
        "approval_manifest_hash": (
            active.approval_manifest_hash if active else ""
        ),
    }


def pilot_deployment_result(deployment, *, changed: bool, action: str) -> dict:
    return {
        "schema_version": "org-memory-pilot-deployment-change-v1",
        "organization_domain": deployment.organization.domain,
        "action": action,
        "changed": bool(changed),
        "state": deployment.state,
        "approval_manifest_hash": deployment.approval_manifest_hash,
        "allowlist_key_version": deployment.allowlist_key_version,
        "approved_actor_count": len(deployment.actor_ref_hashes),
        "approved_context_count": len(deployment.context_ref_hashes),
        "approved_provider_count": deployment.approved_provider_count,
        "approved_source_scope_count": deployment.approved_source_scope_count,
        "approval_review_due_at": deployment.approval_review_due_at.isoformat(),
    }


def pilot_release_gate_report(
    *,
    organization_domain: str | None = None,
    require_active: bool = False,
    now=None,
) -> dict:
    """Return a content-free deploy gate for the global private query flag."""

    now = now or timezone.now()
    query_api_enabled = bool(
        getattr(settings, "ORG_MEMORY_QUERY_API_ENABLED", False)
    )
    report = {
        "schema_version": "org-memory-pilot-release-gate-v1",
        "generated_at": now.isoformat(),
        "ready": True,
        "query_api_enabled": query_api_enabled,
        "required_state": "active" if require_active else "staged_or_active",
        "organization_domain": "",
        "blockers": [],
        "metrics": {
            "active": 0,
            "staged": 0,
            "current_key_matched": 0,
            "enabled_optional_features": 0,
        },
    }
    if not query_api_enabled:
        report["code"] = "private_query_api_disabled"
        return report

    domain = str(
        organization_domain
        if organization_domain is not None
        else getattr(settings, "ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN", "")
    ).strip()
    report["organization_domain"] = domain
    if not domain:
        report["blockers"].append("pilot_organization_domain_missing")

    key_version = ""
    try:
        key_version, _secret = pilot_allowlist_key()
    except PilotDeploymentError:
        report["blockers"].append("pilot_allowlist_key_invalid")

    optional_flags = (
        "ORG_MEMORY_PUBLICATION_ENABLED",
        "ORG_MEMORY_ACTIONS_ENABLED",
        "ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED",
        "ORG_MEMORY_SELECTOR_EXPORT_ENABLED",
    )
    enabled_optional_count = sum(
        bool(getattr(settings, setting_name, False))
        for setting_name in optional_flags
    )
    report["metrics"]["enabled_optional_features"] = enabled_optional_count
    if enabled_optional_count:
        report["blockers"].append("read_only_optional_features_enabled")

    organization = None
    if domain:
        from organizations.models import Organization

        organization = Organization.objects.filter(
            domain__iexact=domain,
        ).first()
        if organization is None:
            report["blockers"].append("pilot_organization_missing")

    if organization is not None:
        rows = list(
            MemoryPilotDeployment.objects.filter(
                organization=organization,
                state__in=(
                    MemoryPilotDeploymentState.STAGED,
                    MemoryPilotDeploymentState.ACTIVE,
                ),
            )
        )
        report["metrics"]["active"] = sum(
            row.state == MemoryPilotDeploymentState.ACTIVE for row in rows
        )
        report["metrics"]["staged"] = sum(
            row.state == MemoryPilotDeploymentState.STAGED for row in rows
        )
        acceptable_states = (
            {MemoryPilotDeploymentState.ACTIVE}
            if require_active
            else {
                MemoryPilotDeploymentState.STAGED,
                MemoryPilotDeploymentState.ACTIVE,
            }
        )
        matching = [
            row
            for row in rows
            if row.state in acceptable_states
            and row.approval_review_due_at > now
            and key_version
            and row.allowlist_key_version == key_version
        ]
        report["metrics"]["current_key_matched"] = len(matching)
        if not matching:
            report["blockers"].append(
                "active_pilot_binding_missing"
                if require_active
                else "staged_or_active_pilot_binding_missing"
            )

    report["blockers"] = sorted(set(report["blockers"]))
    report["ready"] = not report["blockers"]
    report["code"] = (
        "pilot_release_gate_ready"
        if report["ready"]
        else "pilot_release_gate_blocked"
    )
    return report


def pilot_access_matrix_report(
    *,
    organization,
    approval_manifest: Mapping,
    now=None,
) -> dict:
    """Prove the active pilot allowlist without emitting its raw references."""

    now = now or timezone.now()
    report = {
        "schema_version": "org-memory-pilot-access-matrix-v1",
        "generated_at": now.isoformat(),
        "organization_domain": organization.domain,
        "ready": False,
        "blockers": [],
        "metrics": {
            "approved_actor_count": 0,
            "approved_private_channel_count": 0,
            "approved_dm_count": 0,
            "expected_allow_cases": 0,
            "allowed_cases": 0,
            "expected_deny_cases": 0,
            "denied_cases": 0,
        },
    }

    validation_errors = validate_pilot_approval_manifest(
        approval_manifest,
        organization_domain=organization.domain,
        now=now,
    )
    if validation_errors:
        report["blockers"].append("approval_manifest_invalid")
        report["code"] = "pilot_access_matrix_blocked"
        return report

    release_report = pilot_release_gate_report(
        organization_domain=organization.domain,
        require_active=True,
        now=now,
    )
    report["blockers"].extend(release_report["blockers"])
    if not bool(getattr(settings, "ORG_MEMORY_QUERY_API_ENABLED", False)):
        report["blockers"].append("query_api_not_enabled")

    try:
        allowlist = approval_allowlist_hashes(
            organization,
            approval_manifest,
        )
    except PilotDeploymentError:
        report["blockers"].append("pilot_allowlist_key_invalid")
        report["blockers"] = sorted(set(report["blockers"]))
        report["code"] = "pilot_access_matrix_blocked"
        return report

    approval_hash = pilot_approval_manifest_hash(approval_manifest)
    review_due_at = _parse_timestamp(approval_manifest.get("review_due_at"))
    provider_count, source_scope_count = _approved_counts(approval_manifest)
    active_rows = list(
        MemoryPilotDeployment.objects.filter(
            organization=organization,
            state=MemoryPilotDeploymentState.ACTIVE,
        )
    )
    exact_rows = [
        deployment
        for deployment in active_rows
        if deployment.approval_review_due_at > now
        and _deployment_matches(
            deployment,
            approval_hash=approval_hash,
            review_due_at=review_due_at,
            allowlist=allowlist,
            provider_count=provider_count,
            source_scope_count=source_scope_count,
        )
    ]
    if len(exact_rows) != 1:
        report["blockers"].append("active_pilot_binding_mismatch")
        report["blockers"] = sorted(set(report["blockers"]))
        report["code"] = "pilot_access_matrix_blocked"
        return report

    actor_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["pilot_admin_refs"]
    ]
    private_channel_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["allowed_slack_contexts"]
        if reference.startswith("channel:")
    ]
    dm_actor_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["allowed_slack_contexts"]
        if reference.startswith("dm:")
    ]
    public_channels_for_pilot_admins = (
        PUBLIC_PILOT_ADMIN_CONTEXT
        in approval_manifest["allowed_slack_contexts"]
    )
    report["metrics"].update(
        {
            "approved_actor_count": len(actor_ids),
            "approved_private_channel_count": len(private_channel_ids),
            "approved_dm_count": len(dm_actor_ids),
            "approved_public_channel_admin_scope": int(
                public_channels_for_pilot_admins
            ),
        }
    )

    def access(actor_id, channel_id, *, surface="admin_roo"):
        actor = SimpleNamespace(
            organization=organization,
            surface=surface,
            slack_user_id=actor_id,
            slack_channel_id=channel_id,
        )
        return actor_has_active_pilot_access(actor, now=now)

    allowed_results = [
        access(actor_id, channel_id)
        for actor_id in actor_ids
        for channel_id in private_channel_ids
    ]
    allowed_results.extend(
        access(actor_id, "DPILOTACCESSCHECK")
        for actor_id in dm_actor_ids
    )
    if public_channels_for_pilot_admins:
        allowed_results.extend(
            access(actor_id, "CPILOTACCESSCHECK")
            for actor_id in actor_ids
        )

    synthetic_actor_id = "UPILOTACCESSDENY"
    while synthetic_actor_id in actor_ids:
        synthetic_actor_id += "X"
    synthetic_private_channel_id = "GPILOTACCESSDENY"
    while synthetic_private_channel_id in private_channel_ids:
        synthetic_private_channel_id += "X"
    synthetic_public_channel_id = "CPILOTACCESSDENY"

    denied_results = [
        access(synthetic_actor_id, channel_id)
        for channel_id in private_channel_ids
    ]
    denied_results.extend(
        access(actor_id, synthetic_private_channel_id)
        for actor_id in actor_ids
    )
    if public_channels_for_pilot_admins:
        denied_results.append(
            access(synthetic_actor_id, synthetic_public_channel_id)
        )
    else:
        denied_results.extend(
            access(actor_id, synthetic_public_channel_id)
            for actor_id in actor_ids
        )
    denied_results.extend(
        access(synthetic_actor_id, "DPILOTACCESSDENY")
        for _actor_id in dm_actor_ids
    )
    approved_context = (
        private_channel_ids[0]
        if private_channel_ids
        else (
            "DPILOTACCESSCHECK"
            if dm_actor_ids
            else "CPILOTACCESSCHECK"
        )
    )
    approved_actor = (
        actor_ids[0]
        if private_channel_ids or public_channels_for_pilot_admins
        else dm_actor_ids[0]
    )
    denied_results.append(
        access(
            approved_actor,
            approved_context,
            surface="public_roo",
        )
    )

    report["metrics"]["expected_allow_cases"] = len(allowed_results)
    report["metrics"]["allowed_cases"] = sum(allowed_results)
    report["metrics"]["expected_deny_cases"] = len(denied_results)
    report["metrics"]["denied_cases"] = sum(
        not result for result in denied_results
    )
    if not all(allowed_results):
        report["blockers"].append("approved_access_matrix_failed")
    if any(denied_results):
        report["blockers"].append("denied_access_matrix_failed")

    report["blockers"] = sorted(set(report["blockers"]))
    report["ready"] = not report["blockers"]
    report["code"] = (
        "pilot_access_matrix_ready"
        if report["ready"]
        else "pilot_access_matrix_blocked"
    )
    return report
