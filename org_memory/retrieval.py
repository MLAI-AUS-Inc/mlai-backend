from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from django.conf import settings
from django.db.models import Prefetch, Q
from django.utils import timezone

from .authorization import OrganizationAuthorizationContext
from .consolidation import eligible_claims_as_of
from .models import (
    MemoryChunk,
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryClassification,
    MemoryConnectionState,
    MemoryCurrentState,
    MemoryEntity,
    MemoryEvidence,
    MemoryEvidenceSufficiency,
    MemoryQueryMode,
    MemorySourceLifecycle,
)
from .search import MemorySearchResult, search_memory_chunks


RRF_K = 60
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_DECISION_RE = re.compile(r"\b(?:decision|decisions|decided)\b", re.I)
_RECENCY_RE = re.compile(r"\b(?:most recent|latest|recent)\b", re.I)
_COUNTED_MEMORY_RE = re.compile(
    r"\b(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:(?:most\s+recent|latest|recent)\s+)?"
    r"(?:decisions?|commitments?|tasks?|actions?|items?)\b",
    re.I,
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bmlai_sp_[A-Za-z0-9._-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "our",
    "the",
    "to",
    "was",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}


@dataclass(frozen=True)
class QueryPlan:
    mode: str
    entity_ids: tuple[str, ...]
    entity_names: tuple[str, ...]
    ranking_entity_ids: tuple[str, ...]
    ranking_entity_names: tuple[str, ...]
    kinds: tuple[str, ...]
    as_of: object
    time_start: object
    time_end: object
    include_archive: bool
    detail: str
    requested_count: Optional[int]
    recency_priority: bool
    required_kind: Optional[str]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.upper(),
            "entities": [
                {"id": entity_id, "name": name, "match_strength": "explicit"}
                for entity_id, name in zip(self.entity_ids, self.entity_names)
            ],
            "ranking_entities": [
                {"id": entity_id, "name": name, "match_strength": "inferred"}
                for entity_id, name in zip(
                    self.ranking_entity_ids,
                    self.ranking_entity_names,
                )
            ],
            "kinds": list(self.kinds),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "time_range": {
                "start": self.time_start.isoformat() if self.time_start else None,
                "end": self.time_end.isoformat() if self.time_end else None,
            },
            "include_archive": self.include_archive,
            "detail": self.detail,
            "requested_count": self.requested_count,
            "recency_priority": self.recency_priority,
            "required_kind": self.required_kind,
        }


@dataclass
class MemoryCandidate:
    key: str
    claim: Optional[MemoryClaim]
    chunk: Optional[MemoryChunk]
    score: float
    lane_ranks: dict
    features: dict


@dataclass(frozen=True)
class PackedMemory:
    memory_id: str
    candidate: MemoryCandidate
    payload: dict
    citations: tuple[dict, ...]
    estimated_tokens: int


@dataclass(frozen=True)
class MemorySelection:
    plan: QueryPlan
    candidates: tuple[MemoryCandidate, ...]
    selected: tuple[PackedMemory, ...]
    sufficiency: str
    confidence: float
    warnings: tuple[str, ...]
    candidate_trace: tuple[dict, ...]
    lanes: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    embedding_model: str
    embedding_version: str

    @property
    def selected_claim_ids(self) -> list[str]:
        return [str(item.candidate.claim.pk) for item in self.selected if item.candidate.claim]

    @property
    def selected_chunk_ids(self) -> list[str]:
        return [str(item.candidate.chunk.pk) for item in self.selected if item.candidate.chunk]

    @property
    def evidence_bundle(self) -> dict:
        return {
            "as_of": (self.plan.as_of or timezone.now()).isoformat(),
            "query_mode": self.plan.mode.upper(),
            "warnings": list(self.warnings),
            "memories": [item.payload for item in self.selected],
        }


def redact_query(value: str) -> str:
    redacted = str(value or "")[:4000]
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:2000]


def query_terms(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term.casefold()
            for term in _WORD_RE.findall(str(value or ""))
            if len(term) > 1 and term.casefold() not in _STOP_WORDS
        )
    )


def _entity_name_tokens(value: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _WORD_RE.findall(str(value or "")))


def _contains_entity_phrase(
    query_tokens: tuple[str, ...],
    entity_name: str,
) -> bool:
    entity_tokens = _entity_name_tokens(entity_name)
    if not entity_tokens:
        return False
    # A single character is never a safe implicit entity scope. Explicit external
    # identifiers are resolved outside this free-text name matcher.
    if len(entity_tokens) == 1 and len(entity_tokens[0]) < 2:
        return False
    width = len(entity_tokens)
    return any(
        query_tokens[index : index + width] == entity_tokens
        for index in range(len(query_tokens) - width + 1)
    )


def _entity_matches_query(entity: MemoryEntity, query_tokens: tuple[str, ...]) -> bool:
    names = dict.fromkeys((entity.normalized_name, entity.canonical_name))
    return any(_contains_entity_phrase(query_tokens, name) for name in names if name)


def _entity_alias_matches_query(entity: MemoryEntity, query_tokens: tuple[str, ...]) -> bool:
    strong_names = {
        _entity_name_tokens(name)
        for name in (entity.normalized_name, entity.canonical_name)
        if name
    }
    for alias in entity.aliases if isinstance(entity.aliases, list) else ():
        alias_tokens = _entity_name_tokens(alias)
        if alias_tokens in strong_names:
            continue
        if _contains_entity_phrase(query_tokens, alias):
            return True
    return False


def allowed_memory_classifications(
    authorization: OrganizationAuthorizationContext,
) -> tuple[str, ...]:
    return tuple(
        value
        for value in MemoryClassification.values
        if value != MemoryClassification.NO_AGENT
        and authorization.may_view_memory_class(value)
    )


def _natural_time_range(query: str):
    normalized = query.casefold()
    local_now = timezone.localtime(timezone.now())
    if "last week" in normalized:
        this_monday = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return this_monday - timedelta(days=7), this_monday
    if "this week" in normalized:
        start = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, local_now
    if "yesterday" in normalized:
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=1), today
    if "today" in normalized:
        return local_now.replace(hour=0, minute=0, second=0, microsecond=0), local_now
    return None, None


def _mode_for_query(query: str, *, as_of=None, time_start=None, time_end=None) -> str:
    normalized = query.casefold()
    if as_of is not None:
        return MemoryQueryMode.HISTORICAL_AS_OF
    if time_start or time_end or any(
        phrase in normalized
        for phrase in ("timeline", "history", "changed", "last week", "this week", "yesterday")
    ):
        return MemoryQueryMode.TIMELINE
    if any(phrase in normalized for phrase in ("source", "evidence", "citation", "where did")):
        return MemoryQueryMode.EVIDENCE_LOOKUP
    if any(phrase in normalized for phrase in ("open loop", "open task", "blocker", "outstanding", "todo")):
        return MemoryQueryMode.OPEN_LOOPS
    if any(phrase in normalized for phrase in ("metric", "revenue", "mrr", "arr", "balance", "amount")):
        return MemoryQueryMode.METRIC
    if any(
        phrase in normalized
        for phrase in (
            "relationship",
            "relationship with",
            "sponsor relationship",
            "partner relationship",
            "partnership with",
        )
    ):
        return MemoryQueryMode.RELATIONSHIP
    if any(phrase in normalized for phrase in ("who owns", "who is", "expert", "owner", "person")):
        return MemoryQueryMode.PERSON_OR_EXPERT
    if any(phrase in normalized for phrase in ("summary", "overview", "what do we know", "across the organisation")):
        return MemoryQueryMode.GLOBAL_SUMMARY
    if any(phrase in normalized for phrase in ("can we", "before we", "precondition")):
        return MemoryQueryMode.ACTION_PRECONDITION
    return MemoryQueryMode.CURRENT_STATE


def _kinds_for_mode(mode: str, *, query: str = "") -> tuple[str, ...]:
    if _DECISION_RE.search(query):
        return (
            MemoryClaimKind.DECISION,
            MemoryClaimKind.COMMITMENT,
            MemoryClaimKind.TASK,
            MemoryClaimKind.OPEN_LOOP,
        )
    if mode == MemoryQueryMode.OPEN_LOOPS:
        return (
            MemoryClaimKind.TASK,
            MemoryClaimKind.OPEN_LOOP,
            MemoryClaimKind.COMMITMENT,
        )
    if mode == MemoryQueryMode.METRIC:
        return (MemoryClaimKind.METRIC,)
    if mode == MemoryQueryMode.RELATIONSHIP:
        return (MemoryClaimKind.RELATIONSHIP,)
    if mode == MemoryQueryMode.PERSON_OR_EXPERT:
        return (
            MemoryClaimKind.PERSON_PROFILE,
            MemoryClaimKind.RELATIONSHIP,
            MemoryClaimKind.COMMITMENT,
            MemoryClaimKind.DECISION,
        )
    return ()


def _requested_count(query: str) -> Optional[int]:
    match = _COUNTED_MEMORY_RE.search(str(query or ""))
    if not match:
        return None
    raw = match.group("count").casefold()
    value = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    return min(max(value, 1), 10)


def plan_memory_query(
    *,
    organization,
    authorization: OrganizationAuthorizationContext,
    query: str,
    as_of=None,
    time_start=None,
    time_end=None,
    answer_mode: str = "auto",
) -> QueryPlan:
    query_tokens = _entity_name_tokens(query)
    allowed = allowed_memory_classifications(authorization)
    entity_rows = []
    ranking_entity_rows = []
    if query_tokens and allowed:
        for entity in MemoryEntity.objects.filter(
            organization=organization,
            merged_into__isnull=True,
            classification__in=allowed,
        ).only("pk", "canonical_name", "normalized_name", "aliases", "metadata"):
            if (entity.metadata or {}).get("retrieval_quarantined") is True:
                continue
            if _entity_matches_query(entity, query_tokens):
                entity_rows.append(entity)
            elif _entity_alias_matches_query(entity, query_tokens):
                ranking_entity_rows.append(entity)
    natural_start, natural_end = _natural_time_range(query)
    time_start = time_start or natural_start
    time_end = time_end or natural_end
    explicit_modes = {
        "current": MemoryQueryMode.CURRENT_STATE,
        "historical": MemoryQueryMode.HISTORICAL_AS_OF,
        "timeline": MemoryQueryMode.TIMELINE,
        "evidence": MemoryQueryMode.EVIDENCE_LOOKUP,
    }
    mode = explicit_modes.get(str(answer_mode or "auto").casefold())
    mode = mode or _mode_for_query(
        query,
        as_of=as_of,
        time_start=time_start,
        time_end=time_end,
    )
    return QueryPlan(
        mode=mode,
        entity_ids=tuple(str(entity.pk) for entity in entity_rows[:10]),
        entity_names=tuple(entity.canonical_name for entity in entity_rows[:10]),
        ranking_entity_ids=tuple(str(entity.pk) for entity in ranking_entity_rows[:10]),
        ranking_entity_names=tuple(entity.canonical_name for entity in ranking_entity_rows[:10]),
        kinds=_kinds_for_mode(mode, query=query),
        as_of=as_of,
        time_start=time_start,
        time_end=time_end,
        include_archive=mode in {MemoryQueryMode.HISTORICAL_AS_OF, MemoryQueryMode.TIMELINE},
        detail="standard",
        requested_count=_requested_count(query),
        recency_priority=bool(_RECENCY_RE.search(query)),
        required_kind=(
            MemoryClaimKind.DECISION if _DECISION_RE.search(query) else None
        ),
    )


def eligible_evidence_queryset():
    return MemoryEvidence.objects.filter(
        source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source__access_revoked_at__isnull=True,
        source_version__tombstoned_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
    ).select_related("source", "source__configuration", "source_version", "chunk")


def _claim_queryset(*, organization, plan: QueryPlan, classifications):
    if plan.mode == MemoryQueryMode.TIMELINE:
        claims = (
            MemoryClaim.objects.filter(
                organization=organization,
                status__in=(
                    MemoryClaimStatus.ACTIVE,
                    MemoryClaimStatus.STALE,
                    MemoryClaimStatus.SUPERSEDED,
                    MemoryClaimStatus.CONTRADICTED,
                ),
                classification__in=classifications,
                evidence__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
                evidence__source__access_revoked_at__isnull=True,
                evidence__source_version__acl_snapshot__is_accessible=True,
                evidence__source_version__acl_snapshot__revoked_at__isnull=True,
                evidence__source_version__tombstoned_at__isnull=True,
            )
            .exclude(classification=MemoryClassification.NO_AGENT)
            .distinct()
        )
    else:
        as_of = plan.as_of or timezone.now()
        claims = eligible_claims_as_of(
            organization=organization,
            as_of=as_of,
            historical=plan.include_archive,
        ).filter(classification__in=classifications)
    if plan.entity_ids:
        claims = claims.filter(
            Q(subject_entity_id__in=plan.entity_ids)
            | Q(object_entity_id__in=plan.entity_ids)
        )
    if plan.kinds:
        claims = claims.filter(kind__in=plan.kinds)
    if plan.time_start:
        claims = claims.filter(
            Q(observed_at__isnull=False, observed_at__gte=plan.time_start)
            | Q(valid_from__isnull=False, valid_from__gte=plan.time_start)
        )
    if plan.time_end:
        claims = claims.filter(
            Q(observed_at__isnull=True) | Q(observed_at__lt=plan.time_end)
        ).filter(Q(valid_from__isnull=True) | Q(valid_from__lt=plan.time_end))
    return claims.distinct()


def _text_relevance(text: str, terms: Iterable[str]) -> float:
    terms = tuple(terms)
    if not terms:
        return 0.0
    normalized = str(text or "").casefold()
    matched = sum(1 for term in terms if term in normalized)
    return matched / len(terms)


def _claim_text_lane(claims, terms, *, limit: int) -> list:
    scored = []
    for claim in claims[: max(limit * 10, 100)]:
        relevance = _text_relevance(
            " ".join(
                (
                    claim.statement,
                    claim.predicate,
                    claim.subject_entity.canonical_name if claim.subject_entity else "",
                    claim.object_entity.canonical_name if claim.object_entity else "",
                )
            ),
            terms,
        )
        if relevance:
            scored.append((relevance, str(claim.pk), claim.pk))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:limit]]


def _source_health_warning(source, *, now) -> bool:
    configuration = source.configuration
    if not configuration or configuration.lifecycle_state != MemoryConnectionState.ACTIVE:
        return False
    interval = max(int(getattr(settings, "ORG_MEMORY_SYNC_INTERVAL_SECONDS", 86400)), 60)
    last_sync = configuration.last_successful_sync_at
    return last_sync is None or last_sync <= now - timedelta(seconds=interval * 2)


def _candidate_citations(candidate: MemoryCandidate) -> tuple[dict, ...]:
    if candidate.claim:
        evidence = list(candidate.claim.authorized_evidence)
        sources = []
        seen = set()
        for item in evidence:
            key = (item.source_id, item.source_version_id)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "evidence_id": str(item.pk),
                    "provider": item.source.provider,
                    "label": item.source.title or item.source.source_type,
                    "source_url": item.source.canonical_url,
                    "occurred_at": (
                        item.source_version.occurred_at
                        or item.source_version.source_updated_at
                        or item.source_version.captured_at
                    ).isoformat(),
                    "source_id": str(item.source_id),
                    "source_version_id": str(item.source_version_id),
                    "locator": item.source_locator,
                }
            )
        return tuple(sources[:5])
    source = candidate.chunk.source_version.source
    version = candidate.chunk.source_version
    return (
        {
            "evidence_id": str(candidate.chunk.pk),
            "provider": source.provider,
            "label": source.title or source.source_type,
            "source_url": source.canonical_url,
            "occurred_at": (
                version.occurred_at or version.source_updated_at or version.captured_at
            ).isoformat(),
            "source_id": str(source.pk),
            "source_version_id": str(version.pk),
            "locator": candidate.chunk.source_locator,
        },
    )


def _relevant_chunk_excerpt(text: str, terms: Iterable[str], *, limit: int = 1600) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    terms = tuple(terms)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text[:limit]
    scored = []
    for index, line in enumerate(lines):
        normalized = line.casefold()
        term_matches = sum(term in normalized for term in terms)
        email_count = len(_EMAIL_RE.findall(line))
        content_words = len(_WORD_RE.findall(line))
        score = term_matches * 4 + min(content_words, 80) / 80 - email_count * 3
        scored.append((score, content_words, -index, index))
    _score, _words, _position, best_index = max(scored)
    selected_indices = {best_index}
    used = len(lines[best_index])
    distance = 1
    while used < limit and (best_index - distance >= 0 or best_index + distance < len(lines)):
        for candidate_index in (best_index - distance, best_index + distance):
            if candidate_index < 0 or candidate_index >= len(lines):
                continue
            line = lines[candidate_index]
            if used + len(line) + 1 > limit:
                continue
            selected_indices.add(candidate_index)
            used += len(line) + 1
        distance += 1
    return "\n".join(lines[index] for index in sorted(selected_indices))[:limit]


def _pack_candidate(candidate: MemoryCandidate, *, terms: Iterable[str] = ()) -> PackedMemory:
    citations = _candidate_citations(candidate)
    if candidate.claim:
        claim = candidate.claim
        quotes = [item.quote for item in list(claim.authorized_evidence)[:3]]
        payload = {
            "memory_id": candidate.key,
            "type": "claim",
            "claim_id": str(claim.pk),
            "kind": claim.kind,
            "status": claim.status,
            "statement": claim.statement,
            "predicate": claim.predicate,
            "observed_at": claim.observed_at.isoformat() if claim.observed_at else None,
            "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
            "valid_until": claim.valid_until.isoformat() if claim.valid_until else None,
            "confidence": float(claim.confidence),
            "source_authority": float(claim.source_authority),
            "exact_evidence": quotes,
            "citation_ids": [item["evidence_id"] for item in citations],
        }
    else:
        chunk = candidate.chunk
        payload = {
            "memory_id": candidate.key,
            "type": "source_excerpt",
            "chunk_id": str(chunk.pk),
            "text": _relevant_chunk_excerpt(chunk.text, terms),
            "occurred_at": (
                chunk.occurred_at or chunk.source_version.occurred_at
            ).isoformat()
            if (chunk.occurred_at or chunk.source_version.occurred_at)
            else None,
            "citation_ids": [item["evidence_id"] for item in citations],
        }
    estimated_tokens = max(len(str(payload)) // 4, 1)
    return PackedMemory(
        memory_id=candidate.key,
        candidate=candidate,
        payload=payload,
        citations=citations,
        estimated_tokens=estimated_tokens,
    )


def _sufficiency(candidates: list[MemoryCandidate], plan: QueryPlan) -> tuple[str, float]:
    if not candidates:
        return MemoryEvidenceSufficiency.INSUFFICIENT, 0.0
    top = candidates[0]
    relevance = float(top.features.get("lexical_relevance", 0))
    exact = bool(top.features.get("entity_match") or top.features.get("structured_match"))
    semantic = top.lane_ranks.get("vector") is not None
    grounded = bool(_candidate_citations(top))
    if plan.requested_count:
        grounded_matches = sum(
            bool(_candidate_citations(candidate))
            and (
                plan.required_kind is None
                or (
                    candidate.claim is not None
                    and candidate.claim.kind == plan.required_kind
                )
            )
            for candidate in candidates
        )
        if grounded_matches < plan.requested_count:
            if grounded_matches:
                return (
                    MemoryEvidenceSufficiency.PARTIAL,
                    min(0.5, grounded_matches / plan.requested_count),
                )
            return MemoryEvidenceSufficiency.INSUFFICIENT, 0.0
    if grounded and (relevance >= 0.34 or exact or semantic):
        confidence = min(
            0.98,
            0.55
            + relevance * 0.25
            + (0.1 if exact else 0)
            + (0.05 if semantic else 0)
            + min(len(candidates), 3) * 0.02,
        )
        if plan.mode == MemoryQueryMode.GLOBAL_SUMMARY and len(candidates) < 2:
            return MemoryEvidenceSufficiency.PARTIAL, min(confidence, 0.65)
        return MemoryEvidenceSufficiency.SUFFICIENT, confidence
    if grounded and (relevance > 0 or semantic):
        return MemoryEvidenceSufficiency.PARTIAL, 0.45
    return MemoryEvidenceSufficiency.INSUFFICIENT, 0.15


def select_memory(
    *,
    organization,
    authorization: OrganizationAuthorizationContext,
    query: str,
    as_of=None,
    time_start=None,
    time_end=None,
    answer_mode="auto",
    context_token_budget=6000,
    embedding_provider=None,
) -> MemorySelection:
    plan = plan_memory_query(
        organization=organization,
        authorization=authorization,
        query=query,
        as_of=as_of,
        time_start=time_start,
        time_end=time_end,
        answer_mode=answer_mode,
    )
    classifications = allowed_memory_classifications(authorization)
    if not classifications:
        return MemorySelection(
            plan=plan,
            candidates=(),
            selected=(),
            sufficiency=MemoryEvidenceSufficiency.INSUFFICIENT,
            confidence=0.0,
            warnings=("no_authorized_memory_classes",),
            candidate_trace=(),
            lanes=(),
            degraded_reasons=(),
            embedding_model=settings.ORG_MEMORY_EMBEDDING_MODEL,
            embedding_version=settings.ORG_MEMORY_EMBEDDING_VERSION,
        )
    candidate_limit = max(min(int(settings.ORG_MEMORY_QUERY_CANDIDATE_LIMIT), 500), 10)
    result_limit = max(min(int(settings.ORG_MEMORY_QUERY_RESULT_LIMIT), 100), 1)
    claims = _claim_queryset(
        organization=organization,
        plan=plan,
        classifications=classifications,
    ).select_related("subject_entity", "object_entity")
    terms = query_terms(query)
    claim_ids = list(claims.values_list("pk", flat=True))
    current_ids = list(
        MemoryCurrentState.objects.filter(
            organization=organization,
            claim_id__in=claim_ids,
        ).values_list("claim_id", flat=True)
    )
    structured_ids = []
    structured_claims = (
        claims
        if plan.mode == MemoryQueryMode.TIMELINE
        else claims.filter(pk__in=current_ids)
    )
    ranking_entity_ids = frozenset((*plan.entity_ids, *plan.ranking_entity_ids))
    for claim in structured_claims.order_by(
        "-source_authority", "-confidence", "-observed_at", "-recorded_at"
    )[: max(candidate_limit * 5, 100)]:
        entity_match = bool(
            ranking_entity_ids
            and (
                str(claim.subject_entity_id) in ranking_entity_ids
                or str(claim.object_entity_id) in ranking_entity_ids
            )
        )
        text_match = _text_relevance(
            " ".join((claim.statement, claim.predicate)),
            terms,
        )
        broad_structured_mode = bool(plan.kinds) or plan.mode == MemoryQueryMode.GLOBAL_SUMMARY
        if entity_match or text_match or broad_structured_mode:
            structured_ids.append(claim.pk)
        if len(structured_ids) >= candidate_limit:
            break
    if not structured_ids and (plan.entity_ids or plan.kinds):
        structured_ids = list(
            claims.order_by(
                "-source_authority", "-confidence", "-observed_at", "-recorded_at"
            ).values_list("pk", flat=True)[:candidate_limit]
        )
    claim_text_ids = _claim_text_lane(claims, terms, limit=candidate_limit)
    chunk_result: MemorySearchResult = search_memory_chunks(
        organization=organization,
        query=query,
        generate_vector=bool(settings.ORG_MEMORY_QUERY_VECTOR_ENABLED),
        classifications=classifications,
        limit=candidate_limit,
        candidate_limit=candidate_limit,
        embedding_provider=embedding_provider,
    )
    eligible_claim_id_set = set(claim_ids)
    chunk_ids = [hit.chunk.pk for hit in chunk_result.hits]
    chunk_claim_map = {}
    for chunk_id, mapped_claim_id in MemoryEvidence.objects.filter(
        chunk_id__in=chunk_ids,
        claim_id__in=eligible_claim_id_set,
        source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source__access_revoked_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
        source_version__tombstoned_at__isnull=True,
    ).values_list("chunk_id", "claim_id"):
        chunk_claim_map.setdefault(chunk_id, mapped_claim_id)

    scores = {}
    lane_ranks = {}

    def add_lane(key, lane, rank):
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
        lane_ranks.setdefault(key, {})[lane] = rank

    for rank, claim_id in enumerate(structured_ids, 1):
        add_lane(f"claim:{claim_id}", "structured", rank)
    for rank, claim_id in enumerate(claim_text_ids, 1):
        add_lane(f"claim:{claim_id}", "claim_text", rank)
    restrict_unmapped_chunks = bool(
        plan.entity_ids
        or plan.kinds
        or plan.as_of
        or plan.time_start
        or plan.time_end
    )
    for rank, hit in enumerate(chunk_result.hits, 1):
        mapped_claim_id = chunk_claim_map.get(hit.chunk.pk)
        if mapped_claim_id is None and restrict_unmapped_chunks:
            continue
        key = f"claim:{mapped_claim_id}" if mapped_claim_id else f"chunk:{hit.chunk.pk}"
        add_lane(key, "chunk_text", hit.text_rank or rank)
        if hit.vector_rank is not None:
            add_lane(key, "vector", hit.vector_rank)

    claim_keys = [key.split(":", 1)[1] for key in scores if key.startswith("claim:")]
    chunk_keys = [key.split(":", 1)[1] for key in scores if key.startswith("chunk:")]
    evidence_prefetch = Prefetch(
        "evidence",
        queryset=eligible_evidence_queryset().order_by("created_at", "pk"),
        to_attr="authorized_evidence",
    )
    claim_map = {
        str(claim.pk): claim
        for claim in MemoryClaim.objects.filter(
            pk__in=claim_keys,
            organization=organization,
            classification__in=classifications,
        )
        .select_related("subject_entity", "object_entity")
        .prefetch_related(evidence_prefetch)
    }
    allowed_chunk_ids = set(chunk_keys) & {str(hit.chunk.pk) for hit in chunk_result.hits}
    chunk_map = {
        str(chunk.pk): chunk
        for chunk in MemoryChunk.objects.filter(
            pk__in=allowed_chunk_ids,
        ).select_related("source_version__source__configuration", "source_version")
    }
    candidates = []
    current_id_set = {str(value) for value in current_ids}
    for key, base_score in scores.items():
        kind, object_id = key.split(":", 1)
        claim = claim_map.get(object_id) if kind == "claim" else None
        chunk = chunk_map.get(object_id) if kind == "chunk" else None
        if claim is None and chunk is None:
            continue
        text = claim.statement if claim else chunk.text
        lexical = _text_relevance(text, terms)
        entity_match = bool(
            claim
            and ranking_entity_ids
            and (
                str(claim.subject_entity_id) in ranking_entity_ids
                or str(claim.object_entity_id) in ranking_entity_ids
            )
        )
        current_state = bool(claim and str(claim.pk) in current_id_set)
        structured_match = "structured" in lane_ranks[key]
        authority = float(claim.source_authority) if claim else 0.5
        confidence = float(claim.confidence) if claim else 0.5
        status_adjustment = 0.0
        if claim and claim.status == MemoryClaimStatus.ACTIVE:
            status_adjustment += 0.01
        if claim and claim.status == MemoryClaimStatus.STALE:
            status_adjustment -= 0.015
        boilerplate = bool(
            chunk
            and chunk.ordinal == 0
            and len(_EMAIL_RE.findall(chunk.text[:4000])) >= 4
        )
        if boilerplate:
            status_adjustment -= 0.04
        adjusted = (
            base_score
            + lexical * 0.05
            + authority * 0.01
            + confidence * 0.005
            + (0.02 if entity_match else 0)
            + (0.015 if current_state else 0)
            + status_adjustment
        )
        candidates.append(
            MemoryCandidate(
                key=key,
                claim=claim,
                chunk=chunk,
                score=adjusted,
                lane_ranks=lane_ranks[key],
                features={
                    "lexical_relevance": round(lexical, 6),
                    "entity_match": entity_match,
                    "current_state": current_state,
                    "structured_match": structured_match,
                    "source_authority": round(authority, 4),
                    "claim_confidence": round(confidence, 4),
                    "status": claim.status if claim else "source_excerpt",
                    "boilerplate": boilerplate,
                },
            )
        )
    if plan.recency_priority:
        def recency_timestamp(item):
            if item.claim:
                value = (
                    item.claim.observed_at
                    or item.claim.valid_from
                    or item.claim.recorded_at
                )
            else:
                value = (
                    item.chunk.occurred_at
                    or item.chunk.source_version.occurred_at
                    or item.chunk.source_version.source_updated_at
                    or item.chunk.source_version.captured_at
                )
            return value.timestamp() if value else 0

        candidates.sort(
            key=lambda item: (-recency_timestamp(item), -item.score, item.key)
        )
    else:
        candidates.sort(key=lambda item: (-item.score, item.key))
    candidates = candidates[:result_limit]
    sufficiency, confidence = _sufficiency(candidates, plan)

    budget = max(min(int(context_token_budget), 12000), 1000)
    selected = []
    seen_text = set()
    used_tokens = 0
    for candidate in candidates:
        raw_text = candidate.claim.statement if candidate.claim else candidate.chunk.text
        normalized_text = " ".join(raw_text.casefold().split())
        if normalized_text in seen_text:
            continue
        packed = _pack_candidate(candidate, terms=terms)
        if selected and used_tokens + packed.estimated_tokens > budget:
            continue
        selected.append(packed)
        seen_text.add(normalized_text)
        used_tokens += packed.estimated_tokens

    warnings = []
    if sufficiency == MemoryEvidenceSufficiency.PARTIAL:
        warnings.append("limited_evidence")
    if chunk_result.degraded:
        warnings.append("semantic_retrieval_unavailable")
    now = timezone.now()
    source_health_warning = False
    for packed in selected:
        candidate = packed.candidate
        if candidate.claim:
            if candidate.claim.status == MemoryClaimStatus.STALE:
                warnings.append("stale_memory")
            if candidate.claim.consolidation_runs.filter(
                operation="contradicts",
                status="review_required",
            ).exists() or candidate.claim.matched_consolidation_runs.filter(
                operation="contradicts",
                status="review_required",
            ).exists():
                warnings.append("unresolved_conflict")
            for evidence in candidate.claim.authorized_evidence:
                source_health_warning |= _source_health_warning(evidence.source, now=now)
        else:
            source_health_warning |= _source_health_warning(
                candidate.chunk.source_version.source,
                now=now,
            )
    if source_health_warning:
        warnings.append("partial_source_freshness")
    warnings = tuple(dict.fromkeys(warnings))
    trace = tuple(
        {
            "candidate_id": candidate.key,
            "score": round(candidate.score, 8),
            "lane_ranks": candidate.lane_ranks,
            "features": candidate.features,
            "selected": any(item.memory_id == candidate.key for item in selected),
        }
        for candidate in candidates
    )
    return MemorySelection(
        plan=plan,
        candidates=tuple(candidates),
        selected=tuple(selected),
        sufficiency=sufficiency,
        confidence=confidence,
        warnings=warnings,
        candidate_trace=trace,
        lanes=chunk_result.lanes,
        degraded_reasons=chunk_result.degraded_reasons,
        embedding_model=chunk_result.model,
        embedding_version=chunk_result.version,
    )
