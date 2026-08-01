from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from django.conf import settings
from django.core.checks import Tags, run_checks
from django.db import connection
from django.utils import timezone

from .authorization import (
    OrganizationAuthorizationError,
    resolve_actor_authorization,
)
from .evals import (
    evaluate_consolidation_seed_suite,
    evaluate_retrieval_seed_suite,
    evaluate_seed_suite,
)
from .governance import (
    DEFAULT_POLICY_PATH,
    SUPPORTED_PROVIDERS,
    load_policy_manifest,
    parse_enabled_providers,
    validate_policy_manifest,
)
from .models import (
    MemoryChunk,
    MemoryConnectionHealthStatus,
    MemoryConnectionState,
    MemoryDailyReconciliationStatus,
    MemoryFeedback,
    MemoryFeedbackType,
    MemoryOutboxStatus,
    MemoryPreviewStatus,
    MemoryProviderEnablement,
    MemoryQueryLog,
    MemoryScopeStatus,
    MemorySourceLifecycle,
    MemoryWorkStatus,
    OrganizationIdentity,
    OrganizationIdentityProvider,
    ServicePrincipal,
)
from .scheduling import provider_freshness_slo_seconds


PILOT_APPROVAL_SCHEMA_VERSION = 1
READINESS_SCHEMA_VERSION = "org-memory-pilot-readiness-v1"
PUBLIC_PILOT_ADMIN_CONTEXT = "public_channels:pilot_admins"
_PILOT_MANIFEST_FIELDS = frozenset(
    (
        "schema_version",
        "organization_domain",
        "approval_status",
        "approved_at",
        "review_due_at",
        "approvers",
        "pilot_admin_refs",
        "allowed_slack_contexts",
        "approved_providers",
        "approved_source_scopes",
        "controls",
    )
)
_APPROVER_ROLES = frozenset(("data", "security", "review", "operations"))
_REQUIRED_CONTROLS = frozenset(
    (
        "data_processing_terms_approved",
        "retention_and_deletion_approved",
        "backup_restore_tested",
        "incident_response_runbook_approved",
        "freshness_latency_cost_slos_approved",
        "public_roo_isolation_verified",
    )
)
_ADMIN_REF_RE = re.compile(r"^slack:[UW][A-Z0-9]{1,63}$")
_CONTEXT_RE = re.compile(
    r"^(?:dm:[UW][A-Z0-9]{1,63}|channel:G[A-Z0-9]{1,63}|"
    r"public_channels:pilot_admins)$"
)
_EXPLICIT_SELECTOR_LABELS = (
    MemoryFeedbackType.RELEVANT,
    MemoryFeedbackType.CORRECT,
    MemoryFeedbackType.IRRELEVANT,
    MemoryFeedbackType.INCORRECT,
    MemoryFeedbackType.STALE,
    MemoryFeedbackType.HARMFUL,
)


class PilotApprovalError(RuntimeError):
    pass


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pilot_approval_manifest_hash(manifest: Mapping) -> str:
    return hashlib.sha256(
        _canonical_json(manifest).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def load_pilot_approval_manifest(path) -> dict:
    manifest_path = Path(path).expanduser()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PilotApprovalError("Pilot approval manifest does not exist.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotApprovalError("Pilot approval manifest could not be read.") from exc
    if not isinstance(payload, dict):
        raise PilotApprovalError("Pilot approval manifest must be a JSON object.")
    return payload


def validate_pilot_approval_manifest(
    manifest: Mapping,
    *,
    organization_domain: str,
    now=None,
) -> list[str]:
    """Return content-free validation codes for the human rollout approval."""

    now = now or timezone.now()
    errors = []
    if not isinstance(manifest, Mapping):
        return ["approval_manifest_not_object"]
    if set(manifest) != _PILOT_MANIFEST_FIELDS:
        errors.append("approval_manifest_fields_invalid")
    if manifest.get("schema_version") != PILOT_APPROVAL_SCHEMA_VERSION:
        errors.append("approval_schema_unsupported")
    if (
        str(manifest.get("organization_domain") or "").strip().casefold()
        != str(organization_domain or "").strip().casefold()
    ):
        errors.append("approval_organization_mismatch")
    if manifest.get("approval_status") != "approved":
        errors.append("approval_status_not_approved")
    approved_at = _parse_timestamp(manifest.get("approved_at"))
    if approved_at is None or approved_at > now:
        errors.append("approval_timestamp_invalid")
    review_due_at = _parse_timestamp(manifest.get("review_due_at"))
    if review_due_at is None or review_due_at <= now:
        errors.append("approval_review_expired")
    elif review_due_at > now + timedelta(days=366):
        errors.append("approval_review_window_too_long")

    approvers = manifest.get("approvers")
    if not isinstance(approvers, Mapping) or set(approvers) != _APPROVER_ROLES:
        errors.append("approval_approvers_invalid")
    elif any(
        not isinstance(approvers.get(role), str)
        or not approvers[role].strip()
        for role in _APPROVER_ROLES
    ):
        errors.append("approval_approvers_incomplete")

    admin_refs = manifest.get("pilot_admin_refs")
    if (
        not isinstance(admin_refs, list)
        or not 1 <= len(admin_refs) <= 3
        or len(set(admin_refs)) != len(admin_refs)
        or any(not isinstance(value, str) or not _ADMIN_REF_RE.fullmatch(value) for value in admin_refs)
    ):
        errors.append("approval_pilot_admins_invalid")
        admin_refs = []
    contexts = manifest.get("allowed_slack_contexts")
    if (
        not isinstance(contexts, list)
        or not contexts
        or len(set(contexts)) != len(contexts)
        or any(not isinstance(value, str) or not _CONTEXT_RE.fullmatch(value) for value in contexts)
    ):
        errors.append("approval_slack_contexts_invalid")
        contexts = []
    approved_admin_ids = {
        value.split(":", 1)[1] for value in admin_refs
    }
    dm_ids = {
        value.split(":", 1)[1]
        for value in contexts
        if isinstance(value, str) and value.startswith("dm:")
    }
    if not dm_ids.issubset(approved_admin_ids):
        errors.append("approval_dm_actor_mismatch")

    providers = manifest.get("approved_providers")
    if (
        not isinstance(providers, list)
        or not providers
        or len(set(providers)) != len(providers)
        or any(value not in SUPPORTED_PROVIDERS for value in providers)
    ):
        errors.append("approval_providers_invalid")
        providers = []

    source_scopes = manifest.get("approved_source_scopes")
    if (
        not isinstance(source_scopes, Mapping)
        or set(source_scopes) != set(providers)
        or any(
            not isinstance(values, list)
            or not values
            or len(set(values)) != len(values)
            or any(
                not isinstance(value, str)
                or ":" not in value
                or not value.split(":", 1)[0].replace("_", "").isalnum()
                or len(value) > 1024
                for value in values
            )
            for values in source_scopes.values()
        )
    ):
        errors.append("approval_source_scopes_invalid")

    controls = manifest.get("controls")
    if not isinstance(controls, Mapping) or set(controls) != _REQUIRED_CONTROLS:
        errors.append("approval_controls_invalid")
    elif any(controls.get(name) is not True for name in _REQUIRED_CONTROLS):
        errors.append("approval_controls_incomplete")
    return sorted(set(errors))


def _check(key, status, code, *, metrics=None, codes=None):
    payload = {
        "key": key,
        "status": status,
        "code": code,
    }
    if metrics:
        payload["metrics"] = metrics
    if codes:
        payload["codes"] = sorted(set(codes))
    return payload


def _currently_authorized_admin_ids(organization) -> set[str]:
    authorized = set()
    identities = (
        OrganizationIdentity.objects.filter(
            organization=organization,
            provider=OrganizationIdentityProvider.SLACK,
            is_active=True,
            verified_at__isnull=False,
            user__isnull=False,
            user__is_active=True,
        )
        .select_related("user", "organization")
        .order_by("pk")
    )
    for identity in identities:
        actor = SimpleNamespace(
            organization=organization,
            user=identity.user,
            identity=identity,
        )
        try:
            authorization = resolve_actor_authorization(actor)
        except OrganizationAuthorizationError:
            continue
        if authorization.has_capability("view_general_memory"):
            authorized.add(identity.external_user_id)
    return authorized


def _active_admin_principal_metrics(organization, *, now) -> dict:
    usable_principals = 0
    usable_credentials = 0
    over_scoped_principals = 0
    for principal in ServicePrincipal.objects.filter(
        organization=organization,
        is_active=True,
    ).prefetch_related("credentials"):
        scopes = {str(value) for value in (principal.scopes or [])}
        surfaces = {str(value) for value in (principal.allowed_surfaces or [])}
        if "admin_roo" not in surfaces or "org_memory.read" not in scopes:
            continue
        if (
            "public_roo" in surfaces
            or "org_memory.actions" in scopes
            or "org_memory.publish" in scopes
        ):
            over_scoped_principals += 1
            continue
        usable_principals += 1
        usable_credentials += sum(
            credential.revoked_at is None
            and (
                credential.expires_at is None
                or credential.expires_at > now
            )
            for credential in principal.credentials.all()
        )
    return {
        "usable_principals": usable_principals,
        "usable_credentials": usable_credentials,
        "over_scoped_principals": over_scoped_principals,
    }


def build_pilot_readiness_report(
    *,
    organization,
    approval_manifest: Mapping,
    governance_manifest_path=DEFAULT_POLICY_PATH,
    environment="production",
    live=False,
    allow_staged_activation=False,
    allow_runtime_staging=False,
    now=None,
) -> dict:
    """Build a read-only, content-free rollout report for one organisation."""

    now = now or timezone.now()
    checks = []
    approval_errors = validate_pilot_approval_manifest(
        approval_manifest,
        organization_domain=organization.domain,
        now=now,
    )
    checks.append(
        _check(
            "human_approval",
            "block" if approval_errors else "pass",
            "pilot_approval_invalid" if approval_errors else "pilot_approval_current",
            codes=approval_errors,
        )
    )
    approved_providers = (
        set(approval_manifest.get("approved_providers") or [])
        if not approval_errors
        else set()
    )
    if approval_errors:
        runtime_deployment = {
            "status": "block",
            "code": "runtime_pilot_approval_invalid",
            "metrics": {"active": 0, "staged": 0, "matching": 0},
        }
    else:
        from .pilot_deployment import pilot_deployment_readiness

        runtime_deployment = pilot_deployment_readiness(
            organization=organization,
            approval_manifest=approval_manifest,
            live=live,
            allow_staged_activation=allow_staged_activation,
            allow_runtime_staging=allow_runtime_staging,
            now=now,
        )
    checks.append(
        _check(
            "runtime_pilot_deployment",
            runtime_deployment["status"],
            runtime_deployment["code"],
            metrics=runtime_deployment["metrics"],
        )
    )

    enabled_rows = list(
        MemoryProviderEnablement.objects.filter(
            organization=organization,
            is_enabled=True,
        )
    )
    org_enabled_providers = {row.provider for row in enabled_rows}
    deployment_providers = parse_enabled_providers(
        getattr(settings, "ORG_MEMORY_ENABLED_PROVIDERS", "")
    )
    enablement_valid = bool(org_enabled_providers) and all(
        row.approved_by_id and row.approved_at for row in enabled_rows
    )
    provider_alignment = bool(
        enablement_valid
        and org_enabled_providers.issubset(deployment_providers)
        and org_enabled_providers.issubset(approved_providers)
    )
    checks.append(
        _check(
            "provider_enablement",
            "pass" if provider_alignment else "block",
            (
                "provider_enablement_aligned"
                if provider_alignment
                else "provider_enablement_not_aligned"
            ),
            metrics={
                "organization_enabled": len(org_enabled_providers),
                "deployment_enabled": len(deployment_providers),
                "approval_enabled": len(approved_providers),
            },
        )
    )

    try:
        governance_manifest = load_policy_manifest(governance_manifest_path)
        governance_errors = validate_policy_manifest(
            governance_manifest,
            enabled_providers=org_enabled_providers,
            production=True,
        )
        governed_providers = governance_manifest.get("providers") or {}
        raw_approved_source_scopes = approval_manifest.get(
            "approved_source_scopes"
        ) or {}
        approved_source_scopes = (
            raw_approved_source_scopes
            if isinstance(raw_approved_source_scopes, Mapping)
            else {}
        )
        for provider, scope_refs in approved_source_scopes.items():
            governed_scope_refs = set(
                (
                    (governed_providers.get(provider) or {})
                    .get("source_scope", {})
                    .get("selectors", [])
                )
            )
            if not set(scope_refs).issubset(governed_scope_refs):
                governance_errors.append(
                    "approved_source_scope_not_governed"
                )
    except Exception:
        governance_errors = ["governance_manifest_unreadable"]
    checks.append(
        _check(
            "provider_governance",
            "block" if governance_errors else "pass",
            (
                "provider_governance_invalid"
                if governance_errors
                else "provider_governance_approved"
            ),
            metrics={"error_count": len(governance_errors)},
        )
    )

    active_configurations = list(
        organization.memory_connection_configurations.filter(
            lifecycle_state=MemoryConnectionState.ACTIVE,
            deleted_at__isnull=True,
        )
        .select_related("default_policy", "approved_preview")
        .prefetch_related("source_scopes")
    )
    invalid_configurations = 0
    selected_scope_count = 0
    selected_scope_refs: dict[str, set[str]] = {}
    for configuration in active_configurations:
        selected_scopes = [
            scope
            for scope in configuration.source_scopes.all()
            if scope.selected and scope.status == MemoryScopeStatus.SELECTED
        ]
        selected_scope_count += len(selected_scopes)
        selected_scope_refs.setdefault(configuration.provider, set()).update(
            f"{scope.scope_type}:{scope.external_id}"
            for scope in selected_scopes
        )
        preview = configuration.approved_preview
        policy = configuration.default_policy
        configuration_valid = bool(
            configuration.provider in org_enabled_providers
            and configuration.approved_by_id
            and configuration.approved_at
            and not configuration.last_error
            and selected_scopes
            and all(scope.policy_id for scope in selected_scopes)
            and policy is not None
            and policy.is_active
            and policy.reviewed_by_id
            and policy.reviewed_at
            and all(
                scope.policy_id == policy.pk
                and scope.policy.is_active
                and scope.policy.reviewed_by_id
                and scope.policy.reviewed_at
                for scope in selected_scopes
            )
            and preview is not None
            and preview.is_current
            and preview.status == MemoryPreviewStatus.READY
            and preview.dry_run_completed_at
            and configuration.last_dry_run_at
        )
        if not configuration_valid:
            invalid_configurations += 1
    connections_ready = bool(active_configurations) and not invalid_configurations
    checks.append(
        _check(
            "approved_connections",
            "pass" if connections_ready else "block",
            (
                "connections_approved"
                if connections_ready
                else "connections_not_approved"
            ),
            metrics={
                "active_connections": len(active_configurations),
                "selected_scopes": selected_scope_count,
                "invalid_connections": invalid_configurations,
            },
        )
    )
    raw_approved_scope_refs = (
        approval_manifest.get("approved_source_scopes") or {}
    )
    if not isinstance(raw_approved_scope_refs, Mapping):
        raw_approved_scope_refs = {}
    approved_scope_refs = {
        provider: set(values)
        for provider, values in raw_approved_scope_refs.items()
        if isinstance(values, list)
    }
    source_scopes_exact = bool(
        approved_scope_refs
        and selected_scope_refs == approved_scope_refs
    )
    checks.append(
        _check(
            "exact_source_scopes",
            "pass" if source_scopes_exact else "block",
            (
                "selected_source_scopes_exact"
                if source_scopes_exact
                else "selected_source_scopes_not_exact"
            ),
            metrics={
                "approved_scopes": sum(
                    len(values) for values in approved_scope_refs.values()
                ),
                "selected_scopes": sum(
                    len(values) for values in selected_scope_refs.values()
                ),
            },
        )
    )

    approved_admin_ids = {
        str(value).split(":", 1)[1]
        for value in approval_manifest.get("pilot_admin_refs") or []
        if isinstance(value, str) and value.startswith("slack:")
    }
    authorized_admin_ids = _currently_authorized_admin_ids(organization)
    actors_ready = bool(
        approved_admin_ids
        and approved_admin_ids == authorized_admin_ids
        and 1 <= len(authorized_admin_ids) <= 3
    )
    checks.append(
        _check(
            "pilot_actors",
            "pass" if actors_ready else "block",
            "pilot_actors_exact" if actors_ready else "pilot_actors_not_exact",
            metrics={
                "approved_actors": len(approved_admin_ids),
                "authorized_actors": len(authorized_admin_ids),
            },
        )
    )

    principal_metrics = _active_admin_principal_metrics(organization, now=now)
    principal_ready = bool(
        principal_metrics["usable_principals"]
        and principal_metrics["usable_credentials"]
        and not principal_metrics["over_scoped_principals"]
    )
    checks.append(
        _check(
            "admin_roo_service_identity",
            "pass" if principal_ready else "block",
            (
                "admin_roo_identity_ready"
                if principal_ready
                else "admin_roo_identity_not_ready"
            ),
            metrics=principal_metrics,
        )
    )

    optional_flags = {
        "publication": bool(settings.ORG_MEMORY_PUBLICATION_ENABLED),
        "actions": bool(settings.ORG_MEMORY_ACTIONS_ENABLED),
        "linear_execution": bool(
            settings.ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED
        ),
        "selector_export": bool(settings.ORG_MEMORY_SELECTOR_EXPORT_ENABLED),
        "selector_shadow": bool(settings.ORG_MEMORY_SELECTOR_SHADOW_ENABLED),
    }
    optional_features_off = not any(optional_flags.values())
    checks.append(
        _check(
            "optional_feature_kill_switches",
            "pass" if optional_features_off else "block",
            (
                "optional_features_disabled"
                if optional_features_off
                else "optional_features_enabled"
            ),
            metrics={
                "enabled_count": sum(optional_flags.values()),
            },
        )
    )

    query_enabled = bool(settings.ORG_MEMORY_QUERY_API_ENABLED)
    if live and not query_enabled:
        query_status = "block"
        query_code = "query_api_not_enabled"
    elif not live and not query_enabled:
        query_status = "warn"
        query_code = "query_api_activation_pending"
    else:
        query_status = "pass"
        query_code = "query_api_enabled"
    checks.append(
        _check(
            "query_api",
            query_status,
            query_code,
        )
    )

    assertion_window_ready = bool(
        1 <= int(settings.ORG_MEMORY_ACTOR_ASSERTION_MAX_AGE_SECONDS) <= 60
        and 0 <= int(settings.ORG_MEMORY_ACTOR_ASSERTION_CLOCK_SKEW_SECONDS) <= 5
    )
    checks.append(
        _check(
            "actor_assertion_window",
            "pass" if assertion_window_ready else "block",
            (
                "actor_assertion_window_bounded"
                if assertion_window_ready
                else "actor_assertion_window_unsafe"
            ),
        )
    )

    production = str(environment).lower() == "production"
    vector_installed = False
    if connection.vendor == "postgresql":
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                )
                vector_installed = bool(cursor.fetchone()[0])
        except Exception:
            vector_installed = False
    database_ready = (
        connection.vendor == "postgresql" and vector_installed
        if production
        else True
    )
    checks.append(
        _check(
            "database",
            "pass" if database_ready else "block",
            (
                "database_ready"
                if database_ready
                else "production_search_infrastructure_not_ready"
            ),
            metrics={
                "postgresql": connection.vendor == "postgresql",
                "vector_installed": vector_installed,
            },
        )
    )

    security_check_errors = [
        result
        for result in run_checks(tags=[Tags.security])
        if getattr(result, "level", 0) >= 40
    ]
    checks.append(
        _check(
            "django_security_checks",
            "block" if security_check_errors else "pass",
            (
                "django_security_checks_fail"
                if security_check_errors
                else "django_security_checks_pass"
            ),
            metrics={"error_count": len(security_check_errors)},
        )
    )

    queue_metrics = {
        "dead_work": organization.memory_work_items.filter(
            status=MemoryWorkStatus.DEAD,
            dead_letter__resolved_at__isnull=True,
        ).count(),
        "unresolved_dead_letters": organization.memory_dead_letters.filter(
            resolved_at__isnull=True,
        ).count(),
        "failed_outbox": organization.memory_outbox_events.filter(
            status=MemoryOutboxStatus.FAILED,
        ).count(),
        "expired_leases": organization.memory_work_items.filter(
            status=MemoryWorkStatus.PROCESSING,
            leases__released_at__isnull=True,
            leases__expires_at__lte=now,
        ).distinct().count(),
    }
    queues_ready = not any(queue_metrics.values())
    checks.append(
        _check(
            "runtime_queues",
            "pass" if queues_ready else "block",
            "runtime_queues_clean" if queues_ready else "runtime_queues_degraded",
            metrics=queue_metrics,
        )
    )

    recent_reports = list(
        organization.memory_daily_reconciliation_reports.prefetch_related(
            "connection_snapshots"
        )
        .order_by("-report_date", "-started_at")
        [:8]
    )
    latest_report = recent_reports[0] if recent_reports else None
    report_age_seconds = None
    healthy_snapshot_count = 0
    report_ready = False
    healthy_report = None
    for candidate_report in recent_reports:
        report_time = candidate_report.completed_at or candidate_report.updated_at
        candidate_age_seconds = max(int((now - report_time).total_seconds()), 0)
        snapshots = list(candidate_report.connection_snapshots.all())
        candidate_healthy_count = sum(
            snapshot.health_status == MemoryConnectionHealthStatus.HEALTHY
            for snapshot in snapshots
        )
        candidate_ready = bool(
            candidate_report.status == MemoryDailyReconciliationStatus.COMPLETED
            and candidate_age_seconds <= int(timedelta(hours=36).total_seconds())
            and len(snapshots) == len(active_configurations)
            and candidate_healthy_count == len(active_configurations)
            and not candidate_report.alerts
        )
        if candidate_ready:
            healthy_report = candidate_report
            report_age_seconds = candidate_age_seconds
            healthy_snapshot_count = candidate_healthy_count
            report_ready = True
            break
        if candidate_report == latest_report:
            report_age_seconds = candidate_age_seconds
            healthy_snapshot_count = candidate_healthy_count
    checks.append(
        _check(
            "daily_reconciliation",
            "pass" if report_ready else "block",
            (
                "daily_reconciliation_healthy"
                if report_ready
                else "daily_reconciliation_not_healthy"
            ),
            metrics={
                "report_present": latest_report is not None,
                "report_age_seconds": report_age_seconds,
                "healthy_connections": healthy_snapshot_count,
                "latest_report_status": (
                    latest_report.status if latest_report is not None else "missing"
                ),
                "healthy_report_id": (
                    str(healthy_report.pk) if healthy_report is not None else ""
                ),
            },
        )
    )

    stale_configurations = 0
    for configuration in active_configurations:
        freshness_slo = provider_freshness_slo_seconds(
            configuration.provider,
            configuration=configuration,
        )
        if (
            configuration.last_successful_sync_at is None
            or (now - configuration.last_successful_sync_at).total_seconds()
            > freshness_slo
        ):
            stale_configurations += 1
    freshness_ready = bool(active_configurations) and not stale_configurations
    checks.append(
        _check(
            "source_freshness",
            "pass" if freshness_ready else "block",
            "sources_fresh" if freshness_ready else "sources_stale",
            metrics={"stale_connections": stale_configurations},
        )
    )

    active_configuration_ids = [
        configuration.pk for configuration in active_configurations
    ]
    active_source_count = organization.memory_sources.filter(
        configuration_id__in=active_configuration_ids,
        lifecycle_state=MemorySourceLifecycle.ACTIVE,
        access_revoked_at__isnull=True,
        tombstoned_at__isnull=True,
    ).count()
    retrievable_chunk_count = MemoryChunk.objects.filter(
        source_version__source__organization=organization,
        source_version__source__configuration_id__in=active_configuration_ids,
        source_version__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source_version__source__access_revoked_at__isnull=True,
        source_version__tombstoned_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
        active_for_retrieval=True,
    ).exclude(classification="no_agent").count()
    evidence_ready = active_source_count > 0 and retrievable_chunk_count > 0
    checks.append(
        _check(
            "retrievable_evidence",
            "pass" if evidence_ready else "block",
            (
                "retrievable_evidence_present"
                if evidence_ready
                else "retrievable_evidence_missing"
            ),
            metrics={
                "active_sources": active_source_count,
                "retrievable_chunks": retrievable_chunk_count,
            },
        )
    )

    eval_results = (
        evaluate_seed_suite(),
        evaluate_consolidation_seed_suite(),
        evaluate_retrieval_seed_suite(),
    )
    evals_ready = all(result["ok"] for result in eval_results)
    checks.append(
        _check(
            "offline_seed_evaluations",
            "pass" if evals_ready else "block",
            "seed_evaluations_pass" if evals_ready else "seed_evaluations_fail",
            metrics={
                "cases": sum(result["cases"] for result in eval_results),
                "failures": sum(len(result["errors"]) for result in eval_results),
            },
        )
    )

    query_trace_count = MemoryQueryLog.objects.filter(
        organization=organization,
    ).count()
    labeled_trace_count = (
        MemoryFeedback.objects.filter(
            organization=organization,
            feedback_type__in=_EXPLICIT_SELECTOR_LABELS,
            claim__isnull=False,
        )
        .values("query_log_id")
        .distinct()
        .count()
    )
    selector_minimum = int(settings.ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES)
    selector_ready = labeled_trace_count >= selector_minimum
    checks.append(
        _check(
            "pilot_evidence_collection",
            "pass" if selector_ready else "warn",
            (
                "selector_label_gate_met"
                if selector_ready
                else "selector_label_gate_not_met"
            ),
            metrics={
                "query_traces": query_trace_count,
                "labeled_traces": labeled_trace_count,
                "selector_minimum": selector_minimum,
            },
        )
    )

    blockers = [
        check["code"] for check in checks if check["status"] == "block"
    ]
    warnings = [
        check["code"] for check in checks if check["status"] == "warn"
    ]
    approval_hash = pilot_approval_manifest_hash(approval_manifest)
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "organization_domain": organization.domain,
        "stage": "live_read_only_pilot" if live else "preflight_read_only_pilot",
        "environment": str(environment).lower(),
        "generated_at": now.isoformat(),
        "approval_manifest_hash": approval_hash,
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }
