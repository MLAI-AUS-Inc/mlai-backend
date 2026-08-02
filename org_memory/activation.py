from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone

from .models import (
    MemoryClaimKind,
    MemoryClassification,
    MemoryEpistemicType,
    MemoryEvidenceRole,
    MemoryExtractionStatus,
    MemorySourceLifecycle,
)


STRONG_GROUNDING_POLICY_VERSION = "strong-grounding-v1"
MIN_CLAIM_CONFIDENCE = Decimal("0.900")
MIN_EVIDENCE_CONFIDENCE = Decimal("0.900")
MIN_SOURCE_AUTHORITY = Decimal("0.800")
MIN_EXACT_QUOTE_CHARS = 20
_NON_BLOCKING_EXTRACTION_FLAGS = frozenset(
    {"partial_candidate_rejection", "quoted_prompt_injection"}
)

_REVIEWED_RULE_FIELDS = frozenset(
    {
        "default",
        "decisions_require_explicit_cue",
        "tasks_and_project_updates_may_auto_activate",
    }
)
_SAFE_EPISTEMIC_TYPES = {
    MemoryClaimKind.DECISION: frozenset({MemoryEpistemicType.DECISION}),
    MemoryClaimKind.TASK: frozenset(
        {MemoryEpistemicType.OBSERVATION, MemoryEpistemicType.TESTIMONY}
    ),
    MemoryClaimKind.PROJECT_STATUS: frozenset({MemoryEpistemicType.OBSERVATION}),
}
_DECISION_CUE_RE = re.compile(
    r"\b(?:agreed|approved|authori[sz]ed|decided|resolved|adopted|elected|"
    r"voted|ratified|confirmed|selected|appointed|endorsed|established|will)\b",
    re.IGNORECASE,
)
_NEGATED_OR_UNSETTLED_DECISION_RE = re.compile(
    r"\b(?:did\s+not\s+agree|not\s+agreed|not\s+approved|not\s+authori[sz]ed|"
    r"not\s+decided|no\s+decision|decision\s+(?:was\s+)?deferred|"
    r"pending\s+approval|subject\s+to\s+approval|tentative|draft|"
    r"proposed|suggested|considered|might|may|could)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AutoActivationDecision:
    eligible: bool
    reason_codes: tuple[str, ...]
    policy_id: int | None = None
    policy_key: str = ""
    policy_version: str = STRONG_GROUNDING_POLICY_VERSION

    def audit_metadata(self) -> dict:
        return {
            "activation_policy_version": self.policy_version,
            "source_policy_id": self.policy_id,
            "source_policy_key": self.policy_key,
            "reason_codes": list(self.reason_codes),
        }


def source_policy_for_version(source_version):
    """Resolve the most specific configured policy for an immutable source version."""

    source = source_version.source
    scope = source.source_scope
    if scope and scope.policy_id:
        return scope.policy
    configuration = source.configuration
    if configuration and configuration.default_policy_id:
        return configuration.default_policy
    return (
        source.organization.memory_source_policies.filter(
            provider=source.provider,
            is_active=True,
        )
        .filter(Q(scope_type="") | Q(scope_type=source.source_type))
        .order_by("-scope_type", "policy_key")
        .first()
    )


def _rules_allow_claim(rules: Mapping, claim) -> bool:
    if set(rules) != _REVIEWED_RULE_FIELDS or rules.get("default") != "review":
        return False
    if claim.kind == MemoryClaimKind.DECISION:
        return rules.get("decisions_require_explicit_cue") is True
    if claim.kind in {MemoryClaimKind.TASK, MemoryClaimKind.PROJECT_STATUS}:
        return rules.get("tasks_and_project_updates_may_auto_activate") is True
    return False


def _kind_is_allowed(policy, kind: str) -> bool:
    if not isinstance(policy.allowed_memory_kinds, list) or any(
        not isinstance(value, str) for value in policy.allowed_memory_kinds
    ):
        return False
    configured = set(policy.allowed_memory_kinds)
    return kind in configured or (
        kind == MemoryClaimKind.PROJECT_STATUS and "project_update" in configured
    )


def _matches_current_extraction_target(claim) -> bool:
    run = claim.extraction_run
    expected = (
        str(settings.ORG_MEMORY_EXTRACTION_MODEL),
        str(settings.ORG_MEMORY_EXTRACTOR_VERSION),
        str(settings.ORG_MEMORY_EXTRACTION_SCHEMA_VERSION),
        str(settings.ORG_MEMORY_EXTRACTION_PROMPT_VERSION),
    )
    claim_target = (
        str(claim.extractor_model),
        str(claim.extractor_version),
        str(claim.extractor_schema_version),
        str(claim.extractor_prompt_version),
    )
    run_target = (
        str(run.model),
        str(run.extractor_version),
        str(run.schema_version),
        str(run.prompt_version),
    )
    return claim_target == expected and run_target == expected


def _decision_has_explicit_cue(claim, evidence) -> bool:
    text = "\n".join([claim.statement, *(item.quote for item in evidence)])
    return bool(_DECISION_CUE_RE.search(claim.statement)) and not bool(
        _NEGATED_OR_UNSETTLED_DECISION_RE.search(text)
    )


def evaluate_claim_auto_activation(claim, *, now=None) -> AutoActivationDecision:
    """Fail closed unless a claim satisfies every reviewed strong-grounding invariant."""

    now = now or timezone.now()
    reasons: list[str] = []
    run = claim.extraction_run
    source_version = run.source_version
    source = source_version.source
    policy = source_policy_for_version(source_version)
    policy_id = getattr(policy, "pk", None)
    policy_key = str(getattr(policy, "policy_key", "") or "")

    if policy is None:
        reasons.append("source_policy_missing")
    else:
        if (
            policy.organization_id != claim.organization_id
            or policy.provider != source.provider
        ):
            reasons.append("source_policy_scope_mismatch")
        if (
            not policy.is_active
            or not policy.reviewed_by_id
            or not policy.reviewed_at
            or policy.reviewed_at > now
        ):
            reasons.append("source_policy_not_reviewed")
        rules = policy.auto_activation_rules
        if not isinstance(rules, Mapping) or not _rules_allow_claim(rules, claim):
            reasons.append("source_policy_does_not_allow_auto_activation")
        if not _kind_is_allowed(policy, claim.kind):
            reasons.append("claim_kind_not_allowed_by_source_policy")
        if Decimal(str(policy.authority_score)) < MIN_SOURCE_AUTHORITY:
            reasons.append("source_policy_authority_too_low")
        if policy.classification != MemoryClassification.COMMITTEE:
            reasons.append("source_policy_classification_not_committee")

    if claim.kind not in _SAFE_EPISTEMIC_TYPES:
        reasons.append("claim_kind_requires_review")
    elif claim.epistemic_type not in _SAFE_EPISTEMIC_TYPES[claim.kind]:
        reasons.append("epistemic_type_requires_review")

    if (
        run.organization_id != claim.organization_id
        or source.organization_id != claim.organization_id
    ):
        reasons.append("claim_source_organization_mismatch")

    if claim.classification != MemoryClassification.COMMITTEE:
        reasons.append("claim_classification_not_committee")
    if source_version.classification != MemoryClassification.COMMITTEE:
        reasons.append("source_classification_not_committee")
    if str((claim.metadata or {}).get("candidate_classification") or "") != (
        MemoryClassification.COMMITTEE
    ):
        reasons.append("candidate_classification_not_committee")

    if Decimal(claim.confidence) < MIN_CLAIM_CONFIDENCE:
        reasons.append("claim_confidence_too_low")
    if Decimal(claim.source_authority) < MIN_SOURCE_AUTHORITY:
        reasons.append("claim_source_authority_too_low")
    if policy is not None and abs(
        Decimal(claim.source_authority) - Decimal(str(policy.authority_score))
    ) > Decimal("0.001"):
        reasons.append("claim_source_authority_policy_mismatch")

    if run.status != MemoryExtractionStatus.EXTRACTED:
        reasons.append("extraction_not_successful")
    safety_flags = run.safety_flags
    if not isinstance(safety_flags, list) or any(
        not isinstance(flag, str) or flag not in _NON_BLOCKING_EXTRACTION_FLAGS
        for flag in safety_flags
    ):
        reasons.append("extraction_has_safety_flags")
    if not _matches_current_extraction_target(claim):
        reasons.append("extraction_target_not_current")

    if (
        source.lifecycle_state != MemorySourceLifecycle.ACTIVE
        or source.access_revoked_at is not None
        or source.tombstoned_at is not None
    ):
        reasons.append("source_not_active")
    if (
        not source_version.is_current
        or source.current_version_id != source_version.pk
        or source_version.tombstoned_at is not None
    ):
        reasons.append("source_version_not_current")
    if (
        policy is not None
        and policy.historical_cutoff is not None
        and (
            source_version.occurred_at is None
            or source_version.occurred_at < policy.historical_cutoff
        )
    ):
        reasons.append("source_predates_policy_cutoff")
    try:
        acl = source_version.acl_snapshot
    except ObjectDoesNotExist:
        acl = None
    if acl is None or not acl.is_accessible or acl.revoked_at is not None:
        reasons.append("source_acl_not_accessible")

    if "evidence" in getattr(claim, "_prefetched_objects_cache", {}):
        evidence = list(claim.evidence.all())
    else:
        evidence = list(
            claim.evidence.select_related("source", "source_version", "chunk").all()
        )
    if not evidence:
        reasons.append("exact_evidence_missing")
    for item in evidence:
        quote = str(item.quote or "")
        if (
            item.source_id != source.pk
            or item.source_version_id != source_version.pk
            or item.chunk.source_version_id != source_version.pk
        ):
            reasons.append("evidence_source_mismatch")
        if item.evidence_role != MemoryEvidenceRole.SUPPORTS:
            reasons.append("non_supporting_evidence_present")
        if Decimal(item.evidence_confidence) < MIN_EVIDENCE_CONFIDENCE:
            reasons.append("evidence_confidence_too_low")
        if len(quote.strip()) < MIN_EXACT_QUOTE_CHARS:
            reasons.append("evidence_quote_too_short")
        if (
            quote not in item.chunk.text
            or item.quote_hash
            != hashlib.sha256(quote.encode("utf-8")).hexdigest()
            or item.quote_start is None
            or item.quote_end is None
            or item.chunk.text[item.quote_start : item.quote_end] != quote
        ):
            reasons.append("evidence_quote_not_exact")
        if not item.chunk.active_for_retrieval:
            reasons.append("evidence_chunk_not_active")

    if claim.valid_until is not None and claim.valid_until <= now:
        reasons.append("claim_validity_expired")
    if (
        claim.kind in {MemoryClaimKind.TASK, MemoryClaimKind.PROJECT_STATUS}
        and claim.stale_after is not None
        and claim.stale_after <= now
    ):
        reasons.append("volatile_claim_is_stale")
    if (
        claim.kind == MemoryClaimKind.DECISION
        and not _decision_has_explicit_cue(claim, evidence)
    ):
        reasons.append("explicit_decision_cue_missing")

    reason_codes = tuple(sorted(set(reasons)))
    return AutoActivationDecision(
        eligible=not reason_codes,
        reason_codes=reason_codes or ("strong_grounding_satisfied",),
        policy_id=policy_id,
        policy_key=policy_key,
    )
