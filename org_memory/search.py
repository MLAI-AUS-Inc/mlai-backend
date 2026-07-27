from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import F
from pgvector.django import CosineDistance

from .embeddings import (
    EmbeddingTarget,
    configured_embedding_target,
    generate_embedding,
    normalize_vector,
)
from .models import (
    MemoryChunk,
    MemoryChunkEmbedding,
    MemoryClassification,
    MemorySourceLifecycle,
)


RRF_K = 60
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True)
class MemorySearchHit:
    chunk: MemoryChunk
    score: float
    text_rank: Optional[int]
    vector_rank: Optional[int]


@dataclass(frozen=True)
class MemorySearchResult:
    hits: tuple[MemorySearchHit, ...]
    lanes: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    model: str
    version: str

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reasons)


def eligible_memory_chunks(
    *,
    organization,
    classifications: Optional[Iterable[str]] = None,
):
    allowed = tuple(
        classifications
        or (
            value
            for value in MemoryClassification.values
            if value != MemoryClassification.NO_AGENT
        )
    )
    if MemoryClassification.NO_AGENT in allowed:
        allowed = tuple(value for value in allowed if value != MemoryClassification.NO_AGENT)
    return MemoryChunk.objects.filter(
        source_version__source__organization=organization,
        active_for_retrieval=True,
        classification__in=allowed,
        source_version__is_current=True,
        source_version__tombstoned_at__isnull=True,
        source_version__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source_version__source__access_revoked_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
    )


def refresh_search_vectors(*, chunk_ids: Optional[Iterable] = None) -> int:
    """Refresh stored tsvectors; SQLite deliberately uses its text fallback."""

    if connection.vendor != "postgresql":
        return 0
    chunks = MemoryChunk.objects.all()
    if chunk_ids is not None:
        chunks = chunks.filter(pk__in=tuple(chunk_ids))
    return chunks.update(search_vector=SearchVector("text", config="english"))


def _postgres_text_lane(queryset, query: str, *, limit: int) -> list:
    search_query = SearchQuery(query, config="english", search_type="websearch")
    ranked = list(
        queryset.filter(search_vector=search_query)
        .annotate(_memory_text_score=SearchRank(F("search_vector"), search_query))
        .order_by("-_memory_text_score", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    if ranked:
        return ranked
    # Old rows remain searchable while a post-deploy tsvector rebuild is running.
    return list(
        queryset.annotate(
            _memory_text_score=SearchRank(
                SearchVector("text", config="english"),
                search_query,
            )
        )
        .filter(_memory_text_score__gt=0)
        .order_by("-_memory_text_score", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def _portable_text_lane(queryset, query: str, *, limit: int) -> list:
    terms = tuple(dict.fromkeys(word.lower() for word in _WORD_RE.findall(query) if len(word) > 1))
    if not terms:
        return []
    candidate_limit = max(limit * 20, 100)
    scored = []
    for chunk_id, text in queryset.values_list("pk", "text")[:candidate_limit]:
        normalized = (text or "").lower()
        matched = sum(1 for term in terms if term in normalized)
        if matched:
            density = sum(normalized.count(term) for term in terms) / max(len(normalized), 1)
            scored.append((matched / len(terms) + density, str(chunk_id), chunk_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:limit]]


def _text_lane(queryset, query: str, *, limit: int) -> list:
    if connection.vendor == "postgresql":
        return _postgres_text_lane(queryset, query, limit=limit)
    return _portable_text_lane(queryset, query, limit=limit)


def _vector_lane(
    queryset,
    vector: Sequence[float],
    *,
    organization_id,
    target: EmbeddingTarget,
    limit: int,
) -> list:
    normalized = normalize_vector(vector, dimensions=target.dimensions)
    return list(
        MemoryChunkEmbedding.objects.filter(
            organization_id=organization_id,
            chunk__in=queryset,
            is_current=True,
            model=target.model,
            version=target.version,
            dimensions=target.dimensions,
        )
        .annotate(_memory_distance=CosineDistance("vector", normalized))
        .order_by("_memory_distance", "chunk_id")
        .values_list("chunk_id", flat=True)[:limit]
    )


def search_memory_chunks(
    *,
    organization,
    query: str,
    query_embedding: Optional[Sequence[float]] = None,
    generate_vector: bool = True,
    classifications: Optional[Iterable[str]] = None,
    limit: int = 20,
    candidate_limit: int = 100,
    embedding_provider=None,
) -> MemorySearchResult:
    query = str(query or "").strip()
    if not query:
        raise ValueError("A non-empty memory search query is required.")
    limit = max(min(int(limit), 100), 1)
    candidate_limit = max(min(int(candidate_limit), 500), limit)
    target = configured_embedding_target()
    base = eligible_memory_chunks(
        organization=organization,
        classifications=classifications,
    )
    text_ids = _text_lane(base, query, limit=candidate_limit)
    lanes = ["text"]
    degraded = []
    vector_ids = []
    if query_embedding is None and generate_vector:
        try:
            query_embedding = generate_embedding(
                query,
                target=target,
                provider=embedding_provider,
            )
        except Exception as exc:
            degraded.append(f"vector_unavailable:{exc.__class__.__name__}")
    if query_embedding is not None:
        try:
            vector_ids = _vector_lane(
                base,
                query_embedding,
                organization_id=organization.pk,
                target=target,
                limit=candidate_limit,
            )
            lanes.append("vector")
        except Exception as exc:
            degraded.append(f"vector_search_failed:{exc.__class__.__name__}")

    scores = {}
    text_ranks = {}
    vector_ranks = {}
    for rank, chunk_id in enumerate(text_ids, start=1):
        text_ranks[chunk_id] = rank
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    for rank, chunk_id in enumerate(vector_ids, start=1):
        vector_ranks[chunk_id] = rank
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

    ordered_ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))[:limit]
    chunks = {
        chunk.pk: chunk
        for chunk in base.filter(pk__in=ordered_ids).select_related(
            "source_version__source",
            "source_version__acl_snapshot",
        )
    }
    hits = tuple(
        MemorySearchHit(
            chunk=chunks[chunk_id],
            score=scores[chunk_id],
            text_rank=text_ranks.get(chunk_id),
            vector_rank=vector_ranks.get(chunk_id),
        )
        for chunk_id in ordered_ids
        if chunk_id in chunks
    )
    return MemorySearchResult(
        hits=hits,
        lanes=tuple(lanes),
        degraded_reasons=tuple(degraded),
        model=target.model,
        version=target.version,
    )
