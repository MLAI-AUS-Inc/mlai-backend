from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping, Sequence

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .authorization import (
    OrganizationAuthorizationError,
    resolve_actor_authorization,
)
from .models import (
    MemoryChunk,
    MemoryClaim,
    MemoryFeedbackType,
    MemoryQueryLog,
    MemoryQueryMode,
    MemorySourceLifecycle,
    OrganizationIdentity,
    OrganizationIdentityProvider,
)
from .retrieval import allowed_memory_classifications, eligible_evidence_queryset


FEATURE_SCHEMA_VERSION = "org-memory-selector-features-v1"
DATASET_SCHEMA_VERSION = "org-memory-selector-dataset-v1"
DEFAULT_TOP_K = 10

_LANES = ("structured", "claim_text", "chunk_text", "vector")
_STATUSES = ("active", "stale", "candidate", "source_excerpt")
_QUERY_MODES = tuple(str(value) for value in MemoryQueryMode.values)
FEATURE_NAMES = (
    "baseline_score",
    "rrf_reciprocal",
    "lane_count",
    *(f"{lane}_rank_reciprocal" for lane in _LANES),
    "lexical_relevance",
    "entity_match",
    "current_state",
    "structured_match",
    "source_authority",
    "claim_confidence",
    *(f"status_{status}" for status in _STATUSES),
    "query_has_as_of",
    "query_has_time_start",
    "query_has_time_end",
    *(f"query_mode_{mode}" for mode in _QUERY_MODES),
)
_FEATURE_NAME_SET = frozenset(FEATURE_NAMES)
_POSITIVE_FEEDBACK = frozenset(
    (MemoryFeedbackType.RELEVANT, MemoryFeedbackType.CORRECT)
)
_NEGATIVE_FEEDBACK = frozenset(
    (
        MemoryFeedbackType.IRRELEVANT,
        MemoryFeedbackType.INCORRECT,
        MemoryFeedbackType.STALE,
        MemoryFeedbackType.HARMFUL,
    )
)
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SelectorShadowError(RuntimeError):
    pass


class SelectorShadowDisabled(SelectorShadowError):
    pass


class SelectorArtifactError(SelectorShadowError):
    pass


@dataclass(frozen=True)
class SelectorDataset:
    manifest: dict
    records: tuple[dict, ...]
    dataset_hash: str

    def as_dict(self) -> dict:
        return {
            "schema_version": DATASET_SCHEMA_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "manifest": self.manifest,
            "records": list(self.records),
            "dataset_hash": self.dataset_hash,
        }


@dataclass(frozen=True)
class _CandidateFact:
    organization_id: int
    classification: str
    currently_eligible: bool


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _export_secret() -> bytes:
    secret = str(getattr(settings, "ORG_MEMORY_SELECTOR_EXPORT_SECRET", "") or "")
    encoded = secret.encode("utf-8")
    if len(encoded) < 32:
        raise SelectorShadowError(
            "ORG_MEMORY_SELECTOR_EXPORT_SECRET must contain at least 32 bytes."
        )
    return encoded


def _pseudonym(secret: bytes, *, namespace: str, value: str) -> str:
    return hmac.new(
        secret,
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_number(value, *, default=0.0, minimum=-1000.0, maximum=1000.0) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number) or number < minimum or number > maximum:
        return float(default)
    return number


def _reciprocal_rank(value) -> float:
    rank = _safe_number(value, default=0, minimum=0, maximum=1_000_000)
    return 1.0 / rank if rank >= 1 else 0.0


def _candidate_features(row: Mapping, query_plan: Mapping) -> dict[str, float]:
    raw_features = row.get("features")
    raw_features = raw_features if isinstance(raw_features, Mapping) else {}
    lane_ranks = row.get("lane_ranks")
    lane_ranks = lane_ranks if isinstance(lane_ranks, Mapping) else {}
    query_plan = query_plan if isinstance(query_plan, Mapping) else {}
    status = str(raw_features.get("status") or "")
    mode = str(query_plan.get("mode") or "")
    lane_values = {
        lane: _reciprocal_rank(lane_ranks.get(lane))
        for lane in _LANES
    }
    features = {
        "baseline_score": _safe_number(row.get("score")),
        "rrf_reciprocal": sum(lane_values.values()),
        "lane_count": float(sum(value > 0 for value in lane_values.values())),
        **{
            f"{lane}_rank_reciprocal": reciprocal
            for lane, reciprocal in lane_values.items()
        },
        "lexical_relevance": _safe_number(
            raw_features.get("lexical_relevance"),
            minimum=0,
            maximum=1,
        ),
        "entity_match": float(bool(raw_features.get("entity_match"))),
        "current_state": float(bool(raw_features.get("current_state"))),
        "structured_match": float(bool(raw_features.get("structured_match"))),
        "source_authority": _safe_number(
            raw_features.get("source_authority"),
            minimum=0,
            maximum=1,
        ),
        "claim_confidence": _safe_number(
            raw_features.get("claim_confidence"),
            minimum=0,
            maximum=1,
        ),
        "query_has_as_of": float(bool(query_plan.get("as_of"))),
        "query_has_time_start": float(bool(query_plan.get("time_start"))),
        "query_has_time_end": float(bool(query_plan.get("time_end"))),
    }
    features.update(
        {f"status_{value}": float(status == value) for value in _STATUSES}
    )
    features.update(
        {f"query_mode_{value}": float(mode == value) for value in _QUERY_MODES}
    )
    return {name: float(features.get(name, 0.0)) for name in FEATURE_NAMES}


def _parse_candidate_id(value) -> tuple[str, str] | None:
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    kind, object_id = value.split(":", 1)
    if kind not in {"claim", "chunk"}:
        return None
    try:
        normalized = str(uuid.UUID(object_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return kind, normalized


def _chunks(values: Sequence[str], size=500) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _candidate_facts(candidate_ids: Iterable[str]) -> dict[str, _CandidateFact]:
    parsed = {
        candidate_id: _parse_candidate_id(candidate_id)
        for candidate_id in set(candidate_ids)
    }
    valid_values = [value for value in parsed.values() if value is not None]
    claim_ids = sorted(
        object_id for kind, object_id in valid_values if kind == "claim"
    )
    chunk_ids = sorted(
        object_id for kind, object_id in valid_values if kind == "chunk"
    )
    facts: dict[str, _CandidateFact] = {}
    eligible_claim_ids: set[str] = set()
    for batch in _chunks(claim_ids):
        eligible_claim_ids.update(
            str(value)
            for value in eligible_evidence_queryset()
            .filter(claim_id__in=batch)
            .values_list("claim_id", flat=True)
            .distinct()
        )
    for batch in _chunks(claim_ids):
        for claim_id, organization_id, classification in MemoryClaim.objects.filter(
            pk__in=batch
        ).values_list("pk", "organization_id", "classification"):
            normalized = str(claim_id)
            facts[f"claim:{normalized}"] = _CandidateFact(
                organization_id=organization_id,
                classification=classification,
                currently_eligible=normalized in eligible_claim_ids,
            )
    for batch in _chunks(chunk_ids):
        rows = MemoryChunk.objects.filter(
            pk__in=batch,
        ).values_list(
            "pk",
            "source_version__source__organization_id",
            "classification",
            "active_for_retrieval",
            "source_version__source__lifecycle_state",
            "source_version__source__access_revoked_at",
            "source_version__tombstoned_at",
            "source_version__acl_snapshot__is_accessible",
            "source_version__acl_snapshot__revoked_at",
        )
        for (
            chunk_id,
            organization_id,
            classification,
            active_for_retrieval,
            lifecycle_state,
            access_revoked_at,
            tombstoned_at,
            acl_accessible,
            acl_revoked_at,
        ) in rows:
            normalized = str(chunk_id)
            facts[f"chunk:{normalized}"] = _CandidateFact(
                organization_id=organization_id,
                classification=classification,
                currently_eligible=bool(
                    active_for_retrieval
                    and lifecycle_state == MemorySourceLifecycle.ACTIVE
                    and access_revoked_at is None
                    and tombstoned_at is None
                    and acl_accessible
                    and acl_revoked_at is None
                ),
            )
    return facts


def _authorization_for_query(query_log, cache: dict):
    cache_key = (query_log.requester_user_id, query_log.requester_slack_id)
    if cache_key in cache:
        return cache[cache_key]
    if query_log.requester_user_id is None:
        cache[cache_key] = None
        return None
    identities = OrganizationIdentity.objects.filter(
        organization=query_log.organization,
        user_id=query_log.requester_user_id,
        is_active=True,
        verified_at__isnull=False,
    ).select_related("user")
    if query_log.requester_slack_id:
        identities = identities.filter(
            provider=OrganizationIdentityProvider.SLACK,
            external_user_id=query_log.requester_slack_id,
        )
    identity = identities.order_by("provider", "pk").first()
    if identity is None:
        cache[cache_key] = None
        return None
    actor = SimpleNamespace(
        identity=identity,
        user=identity.user,
        organization=query_log.organization,
    )
    try:
        authorization = resolve_actor_authorization(actor)
    except OrganizationAuthorizationError:
        authorization = None
    cache[cache_key] = authorization
    return authorization


def _feedback_labels(query_log) -> dict[str, int]:
    grouped: dict[str, set[int]] = {}
    for feedback in query_log.feedback.all():
        if feedback.claim_id is None:
            continue
        if feedback.feedback_type in _POSITIVE_FEEDBACK:
            label = 1
        elif feedback.feedback_type in _NEGATIVE_FEEDBACK:
            label = 0
        else:
            continue
        grouped.setdefault(f"claim:{feedback.claim_id}", set()).add(label)
    return {
        candidate_id: next(iter(labels))
        for candidate_id, labels in grouped.items()
        if len(labels) == 1
    }


def build_selector_dataset(*, organization, limit=None) -> SelectorDataset:
    """Build a content-free, currently authorised selector dataset."""

    secret = _export_secret()
    maximum = int(
        limit
        if limit is not None
        else getattr(settings, "ORG_MEMORY_SELECTOR_SHADOW_LIMIT", 10_000)
    )
    maximum = max(1, min(maximum, 100_000))
    query_logs = list(
        MemoryQueryLog.objects.filter(
            organization=organization,
        )
        .exclude(candidate_trace=[])
        .select_related("organization", "requester_user")
        .prefetch_related("feedback")
        .order_by("-created_at", "-pk")[:maximum]
    )
    candidate_ids = []
    for query_log in query_logs:
        if not isinstance(query_log.candidate_trace, list):
            continue
        for row in query_log.candidate_trace:
            if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str):
                candidate_ids.append(row["candidate_id"])
    facts = _candidate_facts(candidate_ids)
    authorization_cache = {}
    excluded_counts: dict[str, int] = {}
    records = []
    pairwise_trace_count = 0
    labeled_trace_count = 0

    def exclude(reason):
        excluded_counts[reason] = excluded_counts.get(reason, 0) + 1

    for query_log in query_logs:
        trace = query_log.candidate_trace
        if not isinstance(trace, list) or not trace:
            exclude("empty_or_invalid_trace")
            continue
        authorization = _authorization_for_query(query_log, authorization_cache)
        if authorization is None:
            exclude("requester_not_currently_authorized")
            continue
        allowed = set(allowed_memory_classifications(authorization))
        if not allowed or not authorization.has_capability("view_general_memory"):
            exclude("requester_not_currently_authorized")
            continue
        normalized_rows = []
        trace_invalid = False
        for baseline_rank, row in enumerate(trace, 1):
            if not isinstance(row, Mapping):
                trace_invalid = True
                break
            parsed = _parse_candidate_id(row.get("candidate_id"))
            if parsed is None:
                trace_invalid = True
                break
            candidate_id = f"{parsed[0]}:{parsed[1]}"
            fact = facts.get(candidate_id)
            if (
                fact is None
                or fact.organization_id != organization.pk
                or not fact.currently_eligible
                or fact.classification not in allowed
            ):
                trace_invalid = True
                break
            normalized_rows.append((baseline_rank, candidate_id, row))
        if trace_invalid:
            exclude("candidate_not_currently_authorized")
            continue

        labels = _feedback_labels(query_log)
        candidates = []
        positive_refs = []
        negative_refs = []
        for baseline_rank, candidate_id, row in normalized_rows:
            candidate_ref = _pseudonym(
                secret,
                namespace="candidate",
                value=f"{organization.pk}:{candidate_id}",
            )
            label = labels.get(candidate_id)
            candidate = {
                "candidate_ref": candidate_ref,
                "baseline_rank": baseline_rank,
                "features": _candidate_features(row, query_log.query_plan),
                "label": label,
            }
            candidates.append(candidate)
            if label == 1:
                positive_refs.append(candidate_ref)
            elif label == 0:
                negative_refs.append(candidate_ref)
        pairs = [
            {"preferred_ref": positive, "rejected_ref": negative}
            for positive in positive_refs
            for negative in negative_refs
        ]
        if positive_refs or negative_refs:
            labeled_trace_count += 1
        if pairs:
            pairwise_trace_count += 1
        raw_baseline_version = str(query_log.selector_version or "")
        baseline_version = (
            raw_baseline_version
            if _VERSION_RE.fullmatch(raw_baseline_version)
            else "unknown"
        )
        mode = (
            str(query_log.query_plan.get("mode") or "")
            if isinstance(query_log.query_plan, Mapping)
            else ""
        )
        records.append(
            {
                "query_ref": _pseudonym(
                    secret,
                    namespace="query",
                    value=f"{organization.pk}:{query_log.pk}",
                ),
                "baseline_selector_version": baseline_version,
                "query_context": {
                    "mode": mode if mode in _QUERY_MODES else "",
                    "has_as_of": bool(query_log.as_of),
                },
                "candidates": candidates,
                "pairwise_labels": pairs,
            }
        )

    manifest = {
        "organization_ref": _pseudonym(
            secret,
            namespace="organization",
            value=str(organization.pk),
        ),
        "source_trace_count": len(query_logs),
        "eligible_trace_count": len(records),
        "labeled_trace_count": labeled_trace_count,
        "pairwise_trace_count": pairwise_trace_count,
        "excluded_trace_count": sum(excluded_counts.values()),
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "feature_names": list(FEATURE_NAMES),
        "feedback_policy": {
            "positive": sorted(str(value) for value in _POSITIVE_FEEDBACK),
            "negative": sorted(str(value) for value in _NEGATIVE_FEEDBACK),
            "conflicts": "omit_candidate_label",
            "implicit_negatives": False,
        },
    }
    hash_payload = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "manifest": manifest,
        "records": records,
    }
    return SelectorDataset(
        manifest=manifest,
        records=tuple(records),
        dataset_hash=_sha256_json(hash_payload),
    )


def write_selector_dataset(
    dataset: SelectorDataset,
    output_path,
    *,
    overwrite=False,
) -> Path:
    if not getattr(settings, "ORG_MEMORY_SELECTOR_EXPORT_ENABLED", False):
        raise SelectorShadowDisabled("Selector dataset export is disabled.")
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise SelectorShadowError(f"Output already exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                dataset.as_dict(),
                handle,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination
