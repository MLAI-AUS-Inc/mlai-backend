from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
from typing import Mapping

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import (
    CapabilityGrantEffect,
    MemoryActionType,
    MemoryConnectionHealthStatus,
    MemoryDailyReconciliationStatus,
    MemoryDeletionStatus,
    MemoryOutboxEventType,
    MemoryOutboxStatus,
    MemoryPilotAuditRisk,
    MemoryPilotQueryAudit,
    MemoryQueryLog,
    MemoryQueryStatus,
    MemorySyncRunStatus,
    MemoryWorkStatus,
    MemoryWorkTaskType,
    OrganizationCapabilityGrant,
    OrganizationMembership,
    OrganizationRoleAssignment,
)
from .pilot_readiness import validate_pilot_approval_manifest


PILOT_AUDIT_BATCH_SCHEMA_VERSION = 1
PILOT_EXIT_POLICY_SCHEMA_VERSION = 1
PILOT_EVIDENCE_REPORT_SCHEMA_VERSION = "org-memory-pilot-evidence-v1"

_RUBRIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_BATCH_FIELDS = frozenset(
    (
        "schema_version",
        "organization_domain",
        "reviewer_email",
        "rubric_version",
        "audits",
    )
)
_AUDIT_FIELDS = frozenset(
    (
        "query_id",
        "idempotency_key",
        "reviewed_at",
        "risk",
        "answer_correct",
        "faithfulness_correct",
        "abstention_correct",
        "current_state_correct",
        "temporal_correct",
        "correct_citation_count",
        "permission_leak",
        "public_admin_leak",
    )
)
_EXIT_POLICY_FIELDS = frozenset(
    (
        "schema_version",
        "organization_domain",
        "approval_status",
        "approved_at",
        "review_due_at",
        "pilot_approval_sha256",
        "approvers",
        "window",
        "rubric_version",
        "minimum_samples",
        "thresholds",
        "controls",
    )
)
_EXIT_APPROVER_ROLES = frozenset(("review", "security", "operations"))
_WINDOW_FIELDS = frozenset(("start_at", "end_at"))
_MINIMUM_SAMPLE_FIELDS = frozenset(
    (
        "pilot_days",
        "audited_queries",
        "answered_queries",
        "abstained_queries",
        "high_risk_citations",
        "current_state_queries",
        "temporal_queries",
    )
)
_MINIMUM_SAMPLE_FLOORS = {
    "pilot_days": 7,
    "audited_queries": 20,
    "answered_queries": 10,
    "abstained_queries": 5,
    "high_risk_citations": 10,
    "current_state_queries": 5,
    "temporal_queries": 5,
}
_THRESHOLD_FIELDS = frozenset(
    (
        "high_risk_citation_precision",
        "current_state_accuracy",
        "temporal_accuracy",
        "abstention_accuracy",
        "answer_faithfulness",
        "max_query_failure_rate",
        "max_p95_latency_ms",
        "max_p95_total_tokens",
        "max_daily_total_tokens",
    )
)
_THRESHOLD_FLOORS = {
    "high_risk_citation_precision": 0.95,
    "current_state_accuracy": 0.85,
    "temporal_accuracy": 0.80,
    "abstention_accuracy": 0.85,
    "answer_faithfulness": 0.90,
}
_EXIT_CONTROLS = frozenset(
    (
        "manual_audit_double_checked",
        "incident_log_reconciled",
        "cost_measurement_approved",
    )
)


class PilotEvidenceError(RuntimeError):
    pass


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


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


def _load_json_object(path, *, label):
    manifest_path = Path(path).expanduser()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PilotEvidenceError(f"{label} does not exist.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotEvidenceError(f"{label} could not be read.") from exc
    if not isinstance(value, dict):
        raise PilotEvidenceError(f"{label} must be a JSON object.")
    return value


def load_pilot_audit_batch(path) -> dict:
    return _load_json_object(path, label="Pilot audit batch")


def load_pilot_exit_policy(path) -> dict:
    return _load_json_object(path, label="Pilot exit policy")


def _is_nullable_boolean(value) -> bool:
    return value is None or isinstance(value, bool)


def validate_pilot_audit_batch(
    batch: Mapping,
    *,
    organization_domain: str,
    now=None,
) -> list[str]:
    """Validate structure without querying or exposing any pilot content."""

    now = now or timezone.now()
    errors = []
    if not isinstance(batch, Mapping):
        return ["audit_batch_not_object"]
    if set(batch) != _BATCH_FIELDS:
        errors.append("audit_batch_fields_invalid")
    if batch.get("schema_version") != PILOT_AUDIT_BATCH_SCHEMA_VERSION:
        errors.append("audit_batch_schema_unsupported")
    if (
        str(batch.get("organization_domain") or "").strip().casefold()
        != str(organization_domain or "").strip().casefold()
    ):
        errors.append("audit_batch_organization_mismatch")
    reviewer_email = batch.get("reviewer_email")
    if (
        not isinstance(reviewer_email, str)
        or "@" not in reviewer_email
        or len(reviewer_email) > 254
    ):
        errors.append("audit_batch_reviewer_invalid")
    rubric_version = batch.get("rubric_version")
    if (
        not isinstance(rubric_version, str)
        or not _RUBRIC_RE.fullmatch(rubric_version)
    ):
        errors.append("audit_batch_rubric_invalid")

    audits = batch.get("audits")
    if not isinstance(audits, list) or not 1 <= len(audits) <= 500:
        errors.append("audit_batch_size_invalid")
        return sorted(set(errors))
    idempotency_keys = []
    query_ids = []
    for item in audits:
        if not isinstance(item, Mapping) or set(item) != _AUDIT_FIELDS:
            errors.append("audit_item_fields_invalid")
            continue
        query_id = item.get("query_id")
        try:
            uuid.UUID(str(query_id))
        except (TypeError, ValueError, AttributeError):
            errors.append("audit_item_query_id_invalid")
        query_ids.append(str(query_id))
        idempotency_key = item.get("idempotency_key")
        if (
            not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_RE.fullmatch(idempotency_key)
        ):
            errors.append("audit_item_idempotency_key_invalid")
        idempotency_keys.append(idempotency_key)
        reviewed_at = _parse_timestamp(item.get("reviewed_at"))
        if reviewed_at is None or reviewed_at > now:
            errors.append("audit_item_reviewed_at_invalid")
        if item.get("risk") not in MemoryPilotAuditRisk.values:
            errors.append("audit_item_risk_invalid")
        for field in (
            "answer_correct",
            "faithfulness_correct",
            "current_state_correct",
            "temporal_correct",
        ):
            if not _is_nullable_boolean(item.get(field)):
                errors.append("audit_item_nullable_boolean_invalid")
        for field in (
            "abstention_correct",
            "permission_leak",
            "public_admin_leak",
        ):
            if not isinstance(item.get(field), bool):
                errors.append("audit_item_boolean_invalid")
        correct_citations = item.get("correct_citation_count")
        if (
            isinstance(correct_citations, bool)
            or not isinstance(correct_citations, int)
            or correct_citations < 0
        ):
            errors.append("audit_item_citation_count_invalid")
    if len(idempotency_keys) != len(set(idempotency_keys)):
        errors.append("audit_batch_duplicate_idempotency_key")
    if len(query_ids) != len(set(query_ids)):
        errors.append("audit_batch_duplicate_query")
    return sorted(set(errors))


def validate_pilot_exit_policy(
    policy: Mapping,
    *,
    organization_domain: str,
    now=None,
) -> list[str]:
    now = now or timezone.now()
    errors = []
    if not isinstance(policy, Mapping):
        return ["exit_policy_not_object"]
    if set(policy) != _EXIT_POLICY_FIELDS:
        errors.append("exit_policy_fields_invalid")
    if policy.get("schema_version") != PILOT_EXIT_POLICY_SCHEMA_VERSION:
        errors.append("exit_policy_schema_unsupported")
    if (
        str(policy.get("organization_domain") or "").strip().casefold()
        != str(organization_domain or "").strip().casefold()
    ):
        errors.append("exit_policy_organization_mismatch")
    if policy.get("approval_status") != "approved":
        errors.append("exit_policy_not_approved")
    approved_at = _parse_timestamp(policy.get("approved_at"))
    review_due_at = _parse_timestamp(policy.get("review_due_at"))
    if approved_at is None or approved_at > now:
        errors.append("exit_policy_approved_at_invalid")
    if review_due_at is None or review_due_at <= now:
        errors.append("exit_policy_review_expired")
    if (
        not isinstance(policy.get("pilot_approval_sha256"), str)
        or not _SHA256_RE.fullmatch(policy.get("pilot_approval_sha256") or "")
    ):
        errors.append("exit_policy_approval_hash_invalid")

    approvers = policy.get("approvers")
    if not isinstance(approvers, Mapping) or set(approvers) != _EXIT_APPROVER_ROLES:
        errors.append("exit_policy_approvers_invalid")
    else:
        names = [
            value.strip()
            for value in approvers.values()
            if isinstance(value, str) and value.strip()
        ]
        if len(names) != len(_EXIT_APPROVER_ROLES) or len(set(names)) != len(names):
            errors.append("exit_policy_approvers_not_distinct")

    window = policy.get("window")
    if not isinstance(window, Mapping) or set(window) != _WINDOW_FIELDS:
        errors.append("exit_policy_window_invalid")
    else:
        start_at = _parse_timestamp(window.get("start_at"))
        end_at = _parse_timestamp(window.get("end_at"))
        if start_at is None or end_at is None or end_at <= start_at:
            errors.append("exit_policy_window_invalid")
        elif approved_at is not None and approved_at > start_at:
            errors.append("exit_policy_approved_after_window_start")
        elif review_due_at is not None and review_due_at < end_at:
            errors.append("exit_policy_review_before_window_end")

    rubric_version = policy.get("rubric_version")
    if (
        not isinstance(rubric_version, str)
        or not _RUBRIC_RE.fullmatch(rubric_version)
    ):
        errors.append("exit_policy_rubric_invalid")

    minimums = policy.get("minimum_samples")
    if not isinstance(minimums, Mapping) or set(minimums) != _MINIMUM_SAMPLE_FIELDS:
        errors.append("exit_policy_minimum_samples_invalid")
    else:
        for field, floor in _MINIMUM_SAMPLE_FLOORS.items():
            value = minimums.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                errors.append("exit_policy_minimum_samples_below_floor")

    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, Mapping) or set(thresholds) != _THRESHOLD_FIELDS:
        errors.append("exit_policy_thresholds_invalid")
    else:
        for field, floor in _THRESHOLD_FLOORS.items():
            value = thresholds.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not floor <= float(value) <= 1
            ):
                errors.append("exit_policy_quality_threshold_unsafe")
        failure_rate = thresholds.get("max_query_failure_rate")
        if (
            isinstance(failure_rate, bool)
            or not isinstance(failure_rate, (int, float))
            or not 0 <= float(failure_rate) <= 0.05
        ):
            errors.append("exit_policy_failure_threshold_unsafe")
        for field in (
            "max_p95_latency_ms",
            "max_p95_total_tokens",
            "max_daily_total_tokens",
        ):
            value = thresholds.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                errors.append("exit_policy_operational_threshold_invalid")

    controls = policy.get("controls")
    if not isinstance(controls, Mapping) or set(controls) != _EXIT_CONTROLS:
        errors.append("exit_policy_controls_invalid")
    elif any(controls.get(field) is not True for field in _EXIT_CONTROLS):
        errors.append("exit_policy_controls_incomplete")
    return sorted(set(errors))


def _active_at(queryset, at):
    return queryset.filter(valid_from__lte=at).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gt=at)
    )


def _reviewer_has_capability(membership, *, at) -> bool:
    if not membership.is_effective_at(at):
        return False
    role_ids = _active_at(
        OrganizationRoleAssignment.objects.filter(
            membership=membership,
            role__organization=membership.organization,
            role__is_active=True,
        ),
        at,
    ).values_list("role_id", flat=True)
    subject = Q(membership=membership) | Q(
        role_id__in=role_ids,
        role__organization=membership.organization,
    )
    rows = _active_at(
        OrganizationCapabilityGrant.objects.filter(
            subject,
            capability__key="review_claims",
            capability__is_active=True,
        ),
        at,
    ).values_list("effect", flat=True)
    effects = set(rows)
    return (
        CapabilityGrantEffect.ALLOW in effects
        and CapabilityGrantEffect.DENY not in effects
    )


@transaction.atomic
def import_pilot_audit_batch(
    *,
    organization,
    batch: Mapping,
    now=None,
) -> dict:
    """Atomically persist a strict, content-free batch of independent audits."""

    now = now or timezone.now()
    errors = validate_pilot_audit_batch(
        batch,
        organization_domain=organization.domain,
        now=now,
    )
    if errors:
        raise PilotEvidenceError(
            "Pilot audit batch is invalid: " + ", ".join(errors)
        )
    User = get_user_model()
    reviewer = User.objects.filter(
        email__iexact=batch["reviewer_email"],
        is_active=True,
    ).first()
    if reviewer is None:
        raise PilotEvidenceError("Pilot audit reviewer is unavailable.")
    membership = OrganizationMembership.objects.filter(
        organization=organization,
        user=reviewer,
    ).first()
    if membership is None:
        raise PilotEvidenceError("Pilot audit reviewer is unavailable.")

    audits = list(batch["audits"])
    query_ids = [item["query_id"] for item in audits]
    queries = {
        str(query.pk): query
        for query in MemoryQueryLog.objects.select_related("requester_user").filter(
            organization=organization,
            pk__in=query_ids,
        )
    }
    if len(queries) != len(query_ids):
        raise PilotEvidenceError("One or more pilot audit queries are unavailable.")

    batch_hash = _sha256(batch)
    created_count = 0
    existing_count = 0
    for item in audits:
        query_log = queries[str(item["query_id"])]
        reviewed_at = _parse_timestamp(item["reviewed_at"])
        if not _reviewer_has_capability(membership, at=reviewed_at):
            raise PilotEvidenceError(
                "Pilot audit reviewer lacks an active review capability."
            )
        existing = MemoryPilotQueryAudit.objects.filter(
            organization=organization,
            idempotency_key=item["idempotency_key"],
        ).first()
        if existing is not None:
            if existing.batch_hash != batch_hash:
                raise PilotEvidenceError(
                    "Pilot audit idempotency key conflicts with existing evidence."
                )
            existing_count += 1
            continue
        if MemoryPilotQueryAudit.objects.filter(
            query_log=query_log,
            rubric_version=batch["rubric_version"],
        ).exists():
            raise PilotEvidenceError(
                "Pilot query already has an audit for this rubric."
            )

        audit = MemoryPilotQueryAudit(
            organization=organization,
            query_log=query_log,
            reviewer=reviewer,
            rubric_version=batch["rubric_version"],
            risk=item["risk"],
            answer_correct=item["answer_correct"],
            faithfulness_correct=item["faithfulness_correct"],
            abstention_correct=item["abstention_correct"],
            current_state_correct=item["current_state_correct"],
            temporal_correct=item["temporal_correct"],
            citation_count=len(query_log.citation_data or ()),
            correct_citation_count=item["correct_citation_count"],
            permission_leak=item["permission_leak"],
            public_admin_leak=item["public_admin_leak"],
            idempotency_key=item["idempotency_key"],
            batch_hash=batch_hash,
            reviewed_at=reviewed_at,
        )
        try:
            audit.full_clean()
            audit.save()
        except (ValidationError, IntegrityError) as exc:
            raise PilotEvidenceError(
                "Pilot audit violates the approved audit rubric."
            ) from exc
        created_count += 1
    return {
        "schema_version": "org-memory-pilot-audit-import-v1",
        "organization_domain": organization.domain,
        "batch_hash": batch_hash,
        "received": len(audits),
        "created": created_count,
        "existing": existing_count,
    }


def _ratio(numerator, denominator):
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)


def _p95(values):
    values = sorted(int(value) for value in values)
    if not values:
        return None
    return values[max(math.ceil(len(values) * 0.95) - 1, 0)]


def _check(name, status, code, *, metrics=None, codes=None):
    value = {
        "name": name,
        "status": status,
        "code": code,
        "metrics": metrics or {},
    }
    if codes:
        value["validation_codes"] = sorted(set(codes))
    return value


def _empty_report(
    *,
    organization,
    approval_manifest,
    exit_policy,
    now,
    policy_errors,
) -> dict:
    check = _check(
        "exit_policy",
        "block",
        "pilot_exit_policy_invalid",
        metrics={"error_count": len(policy_errors)},
        codes=policy_errors,
    )
    return {
        "schema_version": PILOT_EVIDENCE_REPORT_SCHEMA_VERSION,
        "organization_domain": organization.domain,
        "generated_at": now.isoformat(),
        "approval_manifest_hash": _sha256(approval_manifest),
        "exit_policy_hash": _sha256(exit_policy),
        "ready_to_exit": False,
        "blockers": [check["code"]],
        "warnings": [],
        "checks": [check],
    }


def _query_is_in_approved_context(query, approval_manifest) -> bool:
    approved_actors = set(approval_manifest.get("pilot_admin_refs") or ())
    contexts = set(approval_manifest.get("allowed_slack_contexts") or ())
    actor_ref = f"slack:{query.requester_slack_id}"
    if actor_ref not in approved_actors:
        return False
    channel_context = f"channel:{query.channel_id}"
    if channel_context in contexts:
        return True
    return bool(
        str(query.channel_id or "").startswith("D")
        and f"dm:{query.requester_slack_id}" in contexts
    )


def build_pilot_evidence_report(
    *,
    organization,
    approval_manifest: Mapping,
    exit_policy: Mapping,
    now=None,
) -> dict:
    """Aggregate one completed pilot window without emitting content or identifiers."""

    now = now or timezone.now()
    policy_errors = validate_pilot_exit_policy(
        exit_policy,
        organization_domain=organization.domain,
        now=now,
    )
    if policy_errors:
        return _empty_report(
            organization=organization,
            approval_manifest=approval_manifest,
            exit_policy=exit_policy,
            now=now,
            policy_errors=policy_errors,
        )

    checks = [
        _check("exit_policy", "pass", "pilot_exit_policy_approved")
    ]
    approval_errors = validate_pilot_approval_manifest(
        approval_manifest,
        organization_domain=organization.domain,
        now=now,
    )
    approval_hash = _sha256(approval_manifest)
    approval_bound = (
        not approval_errors
        and exit_policy["pilot_approval_sha256"] == approval_hash
    )
    checks.append(
        _check(
            "pilot_approval_binding",
            "pass" if approval_bound else "block",
            (
                "pilot_approval_bound"
                if approval_bound
                else "pilot_approval_binding_invalid"
            ),
            metrics={"approval_error_count": len(approval_errors)},
            codes=approval_errors,
        )
    )

    start_at = _parse_timestamp(exit_policy["window"]["start_at"])
    end_at = _parse_timestamp(exit_policy["window"]["end_at"])
    approval_start = _parse_timestamp(approval_manifest.get("approved_at"))
    approval_end = _parse_timestamp(approval_manifest.get("review_due_at"))
    window_complete = bool(
        end_at <= now
        and approval_start
        and approval_end
        and start_at >= approval_start
        and end_at <= approval_end
    )
    duration_seconds = max(int((end_at - start_at).total_seconds()), 0)
    expected_pilot_days = max(math.ceil(duration_seconds / 86400), 1)
    checks.append(
        _check(
            "pilot_window",
            "pass" if window_complete else "block",
            "pilot_window_complete" if window_complete else "pilot_window_invalid",
            metrics={"duration_seconds": duration_seconds},
        )
    )

    query_rows = list(
        MemoryQueryLog.objects.filter(
            organization=organization,
            created_at__gte=start_at,
            created_at__lte=end_at,
        ).order_by("created_at")
    )
    out_of_scope_queries = sum(
        not _query_is_in_approved_context(query, approval_manifest)
        for query in query_rows
    )
    scoped_traffic = bool(query_rows) and not out_of_scope_queries
    checks.append(
        _check(
            "pilot_traffic_scope",
            "pass" if scoped_traffic else "block",
            (
                "pilot_traffic_exactly_scoped"
                if scoped_traffic
                else "pilot_traffic_scope_violation"
            ),
            metrics={
                "query_count": len(query_rows),
                "out_of_scope_queries": out_of_scope_queries,
            },
        )
    )

    audits = list(
        MemoryPilotQueryAudit.objects.filter(
            organization=organization,
            rubric_version=exit_policy["rubric_version"],
            query_log__created_at__gte=start_at,
            query_log__created_at__lte=end_at,
        ).select_related("query_log")
    )
    answered_audits = [
        audit
        for audit in audits
        if audit.query_log.status == MemoryQueryStatus.ANSWERED
    ]
    abstained_audits = [
        audit
        for audit in audits
        if audit.query_log.status == MemoryQueryStatus.ABSTAINED
    ]
    current_state_audits = [
        audit for audit in audits if audit.current_state_correct is not None
    ]
    temporal_audits = [
        audit for audit in audits if audit.temporal_correct is not None
    ]
    high_risk_citation_count = sum(
        audit.citation_count
        for audit in audits
        if audit.risk == MemoryPilotAuditRisk.HIGH
    )
    minimums = exit_policy["minimum_samples"]
    coverage_metrics = {
        "pilot_days": duration_seconds // 86400,
        "audited_queries": len(audits),
        "answered_queries": len(answered_audits),
        "abstained_queries": len(abstained_audits),
        "high_risk_citations": high_risk_citation_count,
        "current_state_queries": len(current_state_audits),
        "temporal_queries": len(temporal_audits),
    }
    eligible_audit_query_ids = {
        query.pk
        for query in query_rows
        if query.status
        in (
            MemoryQueryStatus.ANSWERED,
            MemoryQueryStatus.ABSTAINED,
        )
    }
    audited_query_ids = {audit.query_log_id for audit in audits}
    coverage_metrics["unaudited_answer_decisions"] = len(
        eligible_audit_query_ids - audited_query_ids
    )
    coverage_ready = all(
        coverage_metrics[field] >= minimums[field]
        for field in _MINIMUM_SAMPLE_FIELDS
    ) and not coverage_metrics["unaudited_answer_decisions"]
    checks.append(
        _check(
            "audit_coverage",
            "pass" if coverage_ready else "block",
            (
                "pilot_audit_sample_complete"
                if coverage_ready
                else "pilot_audit_sample_incomplete"
            ),
            metrics=coverage_metrics,
        )
    )

    high_risk_correct_citations = sum(
        audit.correct_citation_count
        for audit in audits
        if audit.risk == MemoryPilotAuditRisk.HIGH
    )
    quality_metrics = {
        "high_risk_citation_precision": _ratio(
            high_risk_correct_citations,
            high_risk_citation_count,
        ),
        "current_state_accuracy": _ratio(
            sum(audit.current_state_correct is True for audit in current_state_audits),
            len(current_state_audits),
        ),
        "temporal_accuracy": _ratio(
            sum(audit.temporal_correct is True for audit in temporal_audits),
            len(temporal_audits),
        ),
        "abstention_accuracy": _ratio(
            sum(audit.abstention_correct for audit in audits),
            len(audits),
        ),
        "answer_faithfulness": _ratio(
            sum(audit.faithfulness_correct is True for audit in answered_audits),
            len(answered_audits),
        ),
        "answer_accuracy": _ratio(
            sum(audit.answer_correct is True for audit in answered_audits),
            len(answered_audits),
        ),
    }
    thresholds = exit_policy["thresholds"]
    quality_ready = coverage_ready and all(
        quality_metrics[field] is not None
        and quality_metrics[field] >= float(thresholds[field])
        for field in _THRESHOLD_FLOORS
    )
    checks.append(
        _check(
            "quality_gates",
            "pass" if quality_ready else "block",
            (
                "pilot_quality_gates_met"
                if quality_ready
                else "pilot_quality_gates_not_met"
            ),
            metrics=quality_metrics,
        )
    )

    permission_leaks = sum(audit.permission_leak for audit in audits)
    public_admin_leaks = sum(audit.public_admin_leak for audit in audits)
    isolation_ready = not permission_leaks and not public_admin_leaks
    checks.append(
        _check(
            "isolation_gates",
            "pass" if isolation_ready else "block",
            (
                "pilot_isolation_preserved"
                if isolation_ready
                else "pilot_isolation_failure"
            ),
            metrics={
                "permission_leaks": permission_leaks,
                "public_admin_leaks": public_admin_leaks,
            },
        )
    )

    status_counts = {
        status: sum(query.status == status for query in query_rows)
        for status in MemoryQueryStatus.values
    }
    answer_attempts = (
        status_counts[MemoryQueryStatus.ANSWERED]
        + status_counts[MemoryQueryStatus.ABSTAINED]
        + status_counts[MemoryQueryStatus.FAILED]
    )
    failure_rate = _ratio(
        status_counts[MemoryQueryStatus.FAILED],
        answer_attempts,
    )
    reliability_ready = bool(
        answer_attempts
        and failure_rate is not None
        and failure_rate <= float(thresholds["max_query_failure_rate"])
    )
    checks.append(
        _check(
            "query_reliability",
            "pass" if reliability_ready else "block",
            (
                "pilot_query_reliability_met"
                if reliability_ready
                else "pilot_query_reliability_not_met"
            ),
            metrics={
                "answer_attempts": answer_attempts,
                "failed_queries": status_counts[MemoryQueryStatus.FAILED],
                "failure_rate": failure_rate,
            },
        )
    )

    p95_latency_ms = _p95(
        query.latency_ms
        for query in query_rows
        if query.status
        in (
            MemoryQueryStatus.ANSWERED,
            MemoryQueryStatus.ABSTAINED,
            MemoryQueryStatus.FAILED,
        )
    )
    latency_ready = bool(
        p95_latency_ms is not None
        and p95_latency_ms <= thresholds["max_p95_latency_ms"]
    )
    checks.append(
        _check(
            "latency_slo",
            "pass" if latency_ready else "block",
            "pilot_latency_slo_met" if latency_ready else "pilot_latency_slo_not_met",
            metrics={"p95_latency_ms": p95_latency_ms},
        )
    )

    token_totals = [
        query.input_tokens + query.output_tokens
        for query in query_rows
        if query.status == MemoryQueryStatus.ANSWERED
    ]
    missing_usage = sum(total <= 0 for total in token_totals)
    daily_tokens = {}
    for query in query_rows:
        date_key = query.created_at.date().isoformat()
        daily_tokens[date_key] = daily_tokens.get(date_key, 0) + (
            query.input_tokens + query.output_tokens
        )
    p95_total_tokens = _p95(token_totals)
    max_daily_total_tokens = max(daily_tokens.values(), default=None)
    ledgers = organization.memory_daily_cost_ledgers.filter(
        budget_date__gte=start_at.date(),
        budget_date__lte=end_at.date(),
    )
    over_cost_ceiling_days = sum(
        ledger.consumed_aud + ledger.reserved_aud > ledger.ceiling_aud
        for ledger in ledgers
    )
    token_cost_ready = bool(
        token_totals
        and not missing_usage
        and p95_total_tokens <= thresholds["max_p95_total_tokens"]
        and max_daily_total_tokens <= thresholds["max_daily_total_tokens"]
        and ledgers.count() >= expected_pilot_days
        and not over_cost_ceiling_days
    )
    checks.append(
        _check(
            "token_cost_slos",
            "pass" if token_cost_ready else "block",
            (
                "pilot_token_cost_slos_met"
                if token_cost_ready
                else "pilot_token_cost_slos_not_met"
            ),
            metrics={
                "p95_total_tokens": p95_total_tokens,
                "max_daily_total_tokens": max_daily_total_tokens,
                "missing_usage_queries": missing_usage,
                "cost_ledger_days": ledgers.count(),
                "over_cost_ceiling_days": over_cost_ceiling_days,
            },
        )
    )

    snapshots = organization.memory_connection_health_snapshots.filter(
        report__report_date__gte=start_at.date(),
        report__report_date__lte=end_at.date(),
    )
    unhealthy_snapshots = snapshots.exclude(
        health_status=MemoryConnectionHealthStatus.HEALTHY,
    ).count()
    stale_snapshots = snapshots.filter(
        Q(source_lag_seconds__isnull=True)
        | Q(source_lag_seconds__gt=F("freshness_slo_seconds"))
    ).count()
    freshness_ready = snapshots.exists() and not unhealthy_snapshots and not stale_snapshots
    checks.append(
        _check(
            "source_freshness",
            "pass" if freshness_ready else "block",
            (
                "pilot_sources_within_slo"
                if freshness_ready
                else "pilot_source_freshness_not_met"
            ),
            metrics={
                "snapshots": snapshots.count(),
                "unhealthy_snapshots": unhealthy_snapshots,
                "stale_snapshots": stale_snapshots,
            },
        )
    )

    failed_backfills = organization.memory_sync_runs.filter(
        action_type=MemoryActionType.BACKFILL,
        status=MemorySyncRunStatus.FAILED,
        created_at__gte=start_at,
        created_at__lte=end_at,
    ).count()
    failed_revocation_work = organization.memory_work_items.filter(
        task_type__in=(
            MemoryWorkTaskType.REFRESH_PERMISSIONS,
            MemoryWorkTaskType.DELETE,
        ),
        status__in=(MemoryWorkStatus.FAILED, MemoryWorkStatus.DEAD),
        created_at__gte=start_at,
        created_at__lte=end_at,
    ).count()
    failed_deletions = organization.memory_deletion_requests.filter(
        status=MemoryDeletionStatus.FAILED,
        requested_at__gte=start_at,
        requested_at__lte=end_at,
    ).count()
    failed_revocation_outbox = organization.memory_outbox_events.filter(
        event_type__in=(
            MemoryOutboxEventType.SOURCE_ACCESS_REVOKED,
            MemoryOutboxEventType.SOURCE_TOMBSTONED,
        ),
        status=MemoryOutboxStatus.FAILED,
        created_at__gte=start_at,
        created_at__lte=end_at,
    ).count()
    sync_safety_ready = not any(
        (
            failed_backfills,
            failed_revocation_work,
            failed_deletions,
            failed_revocation_outbox,
        )
    )
    checks.append(
        _check(
            "sync_and_revocation_safety",
            "pass" if sync_safety_ready else "block",
            (
                "pilot_sync_revocation_safety_met"
                if sync_safety_ready
                else "pilot_sync_revocation_failure"
            ),
            metrics={
                "failed_backfills": failed_backfills,
                "failed_revocation_work": failed_revocation_work,
                "failed_deletions": failed_deletions,
                "failed_revocation_outbox": failed_revocation_outbox,
            },
        )
    )

    reports = organization.memory_daily_reconciliation_reports.filter(
        report_date__gte=start_at.date(),
        report_date__lte=end_at.date(),
    )
    expected_report_days = expected_pilot_days
    healthy_reports = reports.filter(
        status=MemoryDailyReconciliationStatus.COMPLETED,
        alerts=[],
    ).count()
    reports_without_snapshots = reports.filter(
        connection_snapshots__isnull=True,
    ).count()
    daily_health_ready = (
        healthy_reports >= expected_report_days
        and reports.count() == healthy_reports
        and not reports_without_snapshots
    )
    checks.append(
        _check(
            "daily_health_coverage",
            "pass" if daily_health_ready else "block",
            (
                "pilot_daily_health_complete"
                if daily_health_ready
                else "pilot_daily_health_incomplete"
            ),
            metrics={
                "expected_report_days": expected_report_days,
                "healthy_report_days": healthy_reports,
                "report_days": reports.count(),
                "reports_without_snapshots": reports_without_snapshots,
            },
        )
    )

    blockers = [
        check["code"] for check in checks if check["status"] == "block"
    ]
    warnings = [
        check["code"] for check in checks if check["status"] == "warn"
    ]
    return {
        "schema_version": PILOT_EVIDENCE_REPORT_SCHEMA_VERSION,
        "organization_domain": organization.domain,
        "generated_at": now.isoformat(),
        "approval_manifest_hash": approval_hash,
        "exit_policy_hash": _sha256(exit_policy),
        "ready_to_exit": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }
