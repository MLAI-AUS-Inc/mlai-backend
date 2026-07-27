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
    MemorySelectorShadowResult,
    MemorySelectorShadowRun,
    MemorySelectorShadowRunStatus,
    MemorySourceLifecycle,
    OrganizationIdentity,
    OrganizationIdentityProvider,
)
from .retrieval import allowed_memory_classifications, eligible_evidence_queryset


FEATURE_SCHEMA_VERSION = "org-memory-selector-features-v1"
DATASET_SCHEMA_VERSION = "org-memory-selector-dataset-v1"
LEARNED_SELECTOR_INTERFACE_VERSION = "learned-memory-selector-v2"
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


class LearnedMemorySelectorV2:
    """Strict, local-only linear scorer used solely by shadow evaluation."""

    _ARTIFACT_KEYS = frozenset(
        ("interface_version", "version", "feature_schema_version", "bias", "weights")
    )

    def __init__(
        self,
        *,
        version: str,
        bias: float,
        weights: Mapping[str, float],
        artifact_hash: str,
    ):
        self.version = version
        self.bias = bias
        self.weights = dict(weights)
        self.artifact_hash = artifact_hash

    @classmethod
    def from_dict(cls, artifact: Mapping) -> "LearnedMemorySelectorV2":
        if not isinstance(artifact, Mapping):
            raise SelectorArtifactError("Selector artifact must be a JSON object.")
        if set(artifact) != cls._ARTIFACT_KEYS:
            raise SelectorArtifactError("Selector artifact fields do not match the schema.")
        if artifact.get("interface_version") != LEARNED_SELECTOR_INTERFACE_VERSION:
            raise SelectorArtifactError("Selector artifact interface version is unsupported.")
        if artifact.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
            raise SelectorArtifactError("Selector artifact feature schema is unsupported.")
        version = artifact.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise SelectorArtifactError("Selector artifact version is invalid.")
        bias = _safe_number(
            artifact.get("bias"),
            default=float("nan"),
            minimum=-100,
            maximum=100,
        )
        if not math.isfinite(bias):
            raise SelectorArtifactError("Selector artifact bias is invalid.")
        raw_weights = artifact.get("weights")
        if not isinstance(raw_weights, Mapping) or not raw_weights:
            raise SelectorArtifactError("Selector artifact weights are required.")
        if not set(raw_weights).issubset(_FEATURE_NAME_SET):
            raise SelectorArtifactError("Selector artifact contains unknown features.")
        weights = {}
        for name, value in raw_weights.items():
            number = _safe_number(
                value,
                default=float("nan"),
                minimum=-100,
                maximum=100,
            )
            if not math.isfinite(number):
                raise SelectorArtifactError("Selector artifact weight is invalid.")
            weights[str(name)] = number
        normalized = {
            "interface_version": LEARNED_SELECTOR_INTERFACE_VERSION,
            "version": version,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "bias": bias,
            "weights": weights,
        }
        return cls(
            version=version,
            bias=bias,
            weights=weights,
            artifact_hash=_sha256_json(normalized),
        )

    @classmethod
    def from_path(cls, path) -> "LearnedMemorySelectorV2":
        artifact_path = Path(path).expanduser().resolve()
        maximum = int(
            getattr(settings, "ORG_MEMORY_SELECTOR_ARTIFACT_MAX_BYTES", 262_144)
        )
        if artifact_path.stat().st_size > maximum:
            raise SelectorArtifactError("Selector artifact exceeds the size limit.")
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SelectorArtifactError("Selector artifact could not be read.") from exc
        return cls.from_dict(artifact)

    def score(self, features: Mapping[str, float]) -> float:
        return self.bias + sum(
            weight * _safe_number(features.get(name))
            for name, weight in self.weights.items()
        )

    def rank(self, candidates: Sequence[Mapping]) -> tuple[dict, ...]:
        ranked = [
            {
                "candidate_ref": str(candidate["candidate_ref"]),
                "score": self.score(candidate.get("features") or {}),
            }
            for candidate in candidates
        ]
        ranked.sort(key=lambda row: (-row["score"], row["candidate_ref"]))
        return tuple(ranked)


def _ndcg(order: Sequence[str], labels: Mapping[str, int]) -> float | None:
    if not labels:
        return None
    explicitly_labeled_order = [
        candidate_ref for candidate_ref in order if candidate_ref in labels
    ]
    gains = [
        float(labels[candidate_ref])
        for candidate_ref in explicitly_labeled_order
    ]
    ideal = sorted((float(value) for value in labels.values()), reverse=True)

    def dcg(values):
        return sum(value / math.log2(index + 2) for index, value in enumerate(values))

    ideal_score = dcg(ideal)
    return dcg(gains) / ideal_score if ideal_score else None


def _pairwise_accuracy(order: Sequence[str], pairs: Sequence[Mapping]) -> float | None:
    if not pairs:
        return None
    positions = {candidate_ref: index for index, candidate_ref in enumerate(order)}
    correct = sum(
        positions.get(pair["preferred_ref"], math.inf)
        < positions.get(pair["rejected_ref"], math.inf)
        for pair in pairs
    )
    return correct / len(pairs)


def _top_k_overlap(left: Sequence[str], right: Sequence[str], *, k=DEFAULT_TOP_K) -> float:
    size = min(k, len(left), len(right))
    if size == 0:
        return 1.0
    return len(set(left[:size]) & set(right[:size])) / size


def _mean(values: Iterable[float | None]) -> float | None:
    materialized = [value for value in values if value is not None]
    return sum(materialized) / len(materialized) if materialized else None


def _order_hash(order: Sequence[str]) -> str:
    return _sha256_json(list(order))


def run_selector_shadow(
    *,
    organization,
    selector: LearnedMemorySelectorV2,
    limit=None,
) -> MemorySelectorShadowRun:
    """Persist an offline comparison. This function never changes retrieval output."""

    if not getattr(settings, "ORG_MEMORY_SELECTOR_SHADOW_ENABLED", False):
        raise SelectorShadowDisabled("Selector shadow evaluation is disabled.")
    dataset = build_selector_dataset(organization=organization, limit=limit)
    minimum = max(
        1,
        int(getattr(settings, "ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES", 3000)),
    )
    baseline_versions = sorted(
        {
            str(record["baseline_selector_version"])
            for record in dataset.records
            if record["baseline_selector_version"]
        }
    )
    baseline_version = (
        baseline_versions[0] if len(baseline_versions) == 1 else "mixed"
    )
    run, _ = MemorySelectorShadowRun.objects.get_or_create(
        organization=organization,
        dataset_hash=dataset.dataset_hash,
        model_artifact_hash=selector.artifact_hash,
        learned_selector_version=selector.version,
        defaults={
            "status": MemorySelectorShadowRunStatus.RUNNING,
            "baseline_selector_version": baseline_version,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "minimum_required_traces": minimum,
            "eligible_trace_count": dataset.manifest["eligible_trace_count"],
            "labeled_trace_count": dataset.manifest["labeled_trace_count"],
        },
    )
    if (
        run.status == MemorySelectorShadowRunStatus.COMPLETED
        and run.results.count() == run.evaluated_trace_count
    ):
        return run
    if dataset.manifest["labeled_trace_count"] < minimum:
        run.status = MemorySelectorShadowRunStatus.BLOCKED
        run.minimum_required_traces = minimum
        run.eligible_trace_count = dataset.manifest["eligible_trace_count"]
        run.labeled_trace_count = dataset.manifest["labeled_trace_count"]
        run.evaluated_trace_count = 0
        run.metrics = {
            "promotion_eligible": False,
            "reason": "insufficient_labeled_traces",
        }
        run.error_code = "insufficient_labeled_traces"
        run.completed_at = timezone.now()
        run.save(
            update_fields=(
                "status",
                "minimum_required_traces",
                "eligible_trace_count",
                "labeled_trace_count",
                "evaluated_trace_count",
                "metrics",
                "error_code",
                "completed_at",
            )
        )
        return run

    query_log_by_ref = {
        _pseudonym(
            _export_secret(),
            namespace="query",
            value=f"{organization.pk}:{query_log_id}",
        ): query_log_id
        for query_log_id in MemoryQueryLog.objects.filter(
            organization=organization
        ).values_list("pk", flat=True)
    }
    result_models = []
    metric_rows = []
    try:
        for record in dataset.records:
            started = time.perf_counter()
            baseline_order = [
                candidate["candidate_ref"]
                for candidate in sorted(
                    record["candidates"],
                    key=lambda row: (row["baseline_rank"], row["candidate_ref"]),
                )
            ]
            shadow_order = [
                row["candidate_ref"] for row in selector.rank(record["candidates"])
            ]
            labels = {
                candidate["candidate_ref"]: candidate["label"]
                for candidate in record["candidates"]
                if candidate["label"] is not None
            }
            baseline_ndcg = _ndcg(baseline_order, labels)
            shadow_ndcg = _ndcg(shadow_order, labels)
            baseline_pairwise = _pairwise_accuracy(
                baseline_order, record["pairwise_labels"]
            )
            shadow_pairwise = _pairwise_accuracy(
                shadow_order, record["pairwise_labels"]
            )
            top_k_overlap = _top_k_overlap(baseline_order, shadow_order)
            result_models.append(
                MemorySelectorShadowResult(
                    run=run,
                    query_log_id=query_log_by_ref[record["query_ref"]],
                    query_ref=record["query_ref"],
                    candidate_count=len(record["candidates"]),
                    labeled_candidate_count=len(labels),
                    baseline_order_hash=_order_hash(baseline_order),
                    shadow_order_hash=_order_hash(shadow_order),
                    top_k_overlap=top_k_overlap,
                    baseline_ndcg=baseline_ndcg,
                    shadow_ndcg=shadow_ndcg,
                    baseline_pairwise_accuracy=baseline_pairwise,
                    shadow_pairwise_accuracy=shadow_pairwise,
                    disagreement=baseline_order != shadow_order,
                    latency_ms=max(
                        0,
                        int((time.perf_counter() - started) * 1000),
                    ),
                )
            )
            metric_rows.append(
                {
                    "top_k_overlap": top_k_overlap,
                    "baseline_ndcg": baseline_ndcg,
                    "shadow_ndcg": shadow_ndcg,
                    "baseline_pairwise_accuracy": baseline_pairwise,
                    "shadow_pairwise_accuracy": shadow_pairwise,
                    "disagreement": baseline_order != shadow_order,
                }
            )
        baseline_ndcg = _mean(row["baseline_ndcg"] for row in metric_rows)
        shadow_ndcg = _mean(row["shadow_ndcg"] for row in metric_rows)
        baseline_pairwise = _mean(
            row["baseline_pairwise_accuracy"] for row in metric_rows
        )
        shadow_pairwise = _mean(
            row["shadow_pairwise_accuracy"] for row in metric_rows
        )
        ndcg_gain = (
            shadow_ndcg - baseline_ndcg
            if shadow_ndcg is not None and baseline_ndcg is not None
            else None
        )
        threshold = float(
            getattr(settings, "ORG_MEMORY_SELECTOR_MIN_NDCG_GAIN", 0.02)
        )
        promotion_eligible = bool(
            dataset.manifest["labeled_trace_count"] >= minimum
            and ndcg_gain is not None
            and ndcg_gain >= threshold
            and (
                baseline_pairwise is None
                or shadow_pairwise is None
                or shadow_pairwise >= baseline_pairwise
            )
        )
        metrics = {
            "top_k_overlap": _mean(row["top_k_overlap"] for row in metric_rows),
            "disagreement_rate": _mean(
                float(row["disagreement"]) for row in metric_rows
            ),
            "baseline_ndcg": baseline_ndcg,
            "shadow_ndcg": shadow_ndcg,
            "ndcg_gain": ndcg_gain,
            "baseline_pairwise_accuracy": baseline_pairwise,
            "shadow_pairwise_accuracy": shadow_pairwise,
            "minimum_ndcg_gain": threshold,
            "promotion_eligible": promotion_eligible,
            "production_ranking_changed": False,
            "reinforcement_learning_enabled": False,
        }
        with transaction.atomic():
            if run.results.exists():
                raise SelectorShadowError(
                    "An incomplete shadow run already contains immutable results."
                )
            MemorySelectorShadowResult.objects.bulk_create(result_models)
            run.status = MemorySelectorShadowRunStatus.COMPLETED
            run.minimum_required_traces = minimum
            run.eligible_trace_count = dataset.manifest["eligible_trace_count"]
            run.labeled_trace_count = dataset.manifest["labeled_trace_count"]
            run.evaluated_trace_count = len(result_models)
            run.metrics = metrics
            run.error_code = ""
            run.completed_at = timezone.now()
            run.save(
                update_fields=(
                    "status",
                    "minimum_required_traces",
                    "eligible_trace_count",
                    "labeled_trace_count",
                    "evaluated_trace_count",
                    "metrics",
                    "error_code",
                    "completed_at",
                )
            )
        return run
    except Exception:
        run.status = MemorySelectorShadowRunStatus.FAILED
        run.metrics = {"promotion_eligible": False}
        run.error_code = "shadow_evaluation_failed"
        run.completed_at = timezone.now()
        run.save(
            update_fields=("status", "metrics", "error_code", "completed_at")
        )
        raise
