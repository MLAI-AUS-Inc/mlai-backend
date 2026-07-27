from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection, transaction
from django.db.models import F
from pgvector.django import CosineDistance

from .models import PublicKnowledgeItem, PublicKnowledgeStatus


PUBLIC_VECTOR_DIMENSIONS = 1536
RRF_K = 60
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


class PublicKnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class PublicKnowledgeHit:
    item: PublicKnowledgeItem
    score: float
    text_rank: Optional[int]
    vector_rank: Optional[int]


def active_public_knowledge(*, organization_id=None, organization_domain=None):
    rows = PublicKnowledgeItem.objects.filter(status=PublicKnowledgeStatus.ACTIVE)
    if organization_id is not None:
        rows = rows.filter(organization_id=organization_id)
    elif organization_domain:
        rows = rows.filter(organization__domain__iexact=str(organization_domain).strip())
    else:
        return PublicKnowledgeItem.objects.none()
    return rows


def refresh_public_search_vectors(*, item_ids=None) -> int:
    """Refresh the public-only tsvector; SQLite uses the portable text lane."""

    if connection.vendor != "postgresql":
        return 0
    rows = PublicKnowledgeItem.objects.all()
    if item_ids is not None:
        rows = rows.filter(pk__in=tuple(item_ids))
    return rows.update(
        search_vector=(
            SearchVector("title", weight="A", config="english")
            + SearchVector("body", weight="B", config="english")
        )
    )


def _postgres_text_lane(rows, query: str, *, limit: int) -> list:
    search_query = SearchQuery(query, config="english", search_type="websearch")
    ranked = list(
        rows.filter(search_vector=search_query)
        .annotate(_public_rank=SearchRank(F("search_vector"), search_query))
        .order_by("-_public_rank", "-published_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    if ranked:
        return ranked
    return list(
        rows.annotate(
            _public_rank=SearchRank(
                SearchVector("title", weight="A", config="english")
                + SearchVector("body", weight="B", config="english"),
                search_query,
            )
        )
        .filter(_public_rank__gt=0)
        .order_by("-_public_rank", "-published_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def _portable_text_lane(rows, query: str, *, limit: int) -> list:
    terms = tuple(
        dict.fromkeys(
            word.casefold()
            for word in _WORD_RE.findall(query)
            if len(word) > 1
        )
    )
    if not terms:
        return []
    candidate_limit = max(limit * 30, 150)
    scored = []
    for item_id, title, body, tags in rows.values_list(
        "pk",
        "title",
        "body",
        "tags",
    )[:candidate_limit]:
        title_text = str(title or "").casefold()
        body_text = str(body or "").casefold()
        tag_text = " ".join(str(value) for value in (tags or ())).casefold()
        matched = sum(
            1
            for term in terms
            if term in title_text or term in body_text or term in tag_text
        )
        if not matched:
            continue
        title_matches = sum(title_text.count(term) for term in terms)
        tag_matches = sum(tag_text.count(term) for term in terms)
        body_matches = sum(body_text.count(term) for term in terms)
        score = (
            matched / len(terms)
            + title_matches * 0.25
            + tag_matches * 0.15
            + body_matches / max(len(body_text), 1)
        )
        scored.append((score, str(item_id), item_id))
    scored.sort(key=lambda value: (-value[0], value[1]))
    return [value[2] for value in scored[:limit]]


def _normalized_vector(vector: Sequence[float]) -> list[float]:
    try:
        normalized = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise PublicKnowledgeError("Public query embedding must be numeric.") from exc
    if len(normalized) != PUBLIC_VECTOR_DIMENSIONS:
        raise PublicKnowledgeError(
            f"Public query embedding must contain {PUBLIC_VECTOR_DIMENSIONS} values."
        )
    if not all(math.isfinite(value) for value in normalized) or not any(normalized):
        raise PublicKnowledgeError("Public query embedding is invalid.")
    return normalized


@transaction.atomic
def store_public_knowledge_embedding(
    *,
    item,
    vector: Sequence[float],
    model: str,
    version: str,
) -> PublicKnowledgeItem:
    item = PublicKnowledgeItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PublicKnowledgeStatus.ACTIVE:
        raise PublicKnowledgeError("Only active public knowledge can be embedded.")
    model = str(model or "").strip()
    version = str(version or "").strip()
    if not model or not version:
        raise PublicKnowledgeError("Public embedding model and version are required.")
    normalized = _normalized_vector(vector)
    vector_hash = hashlib.sha256(
        json.dumps(
            normalized,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        item.embedding_model == model
        and item.embedding_version == version
        and item.embedding_hash
        and item.embedding_hash != vector_hash
    ):
        raise PublicKnowledgeError(
            "This public embedding model version already has different vector data."
        )
    item.embedding = normalized
    item.embedding_model = model
    item.embedding_version = version
    item.embedding_hash = vector_hash
    item.full_clean()
    item.save(
        update_fields=(
            "embedding",
            "embedding_model",
            "embedding_version",
            "embedding_hash",
            "updated_at",
        )
    )
    return item


def _vector_lane(rows, vector: Sequence[float], *, limit: int) -> list:
    normalized = _normalized_vector(vector)
    return list(
        rows.filter(embedding__isnull=False)
        .annotate(_public_distance=CosineDistance("embedding", normalized))
        .order_by("_public_distance", "-published_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def search_public_knowledge(
    *,
    query: str,
    organization_id=None,
    organization_domain=None,
    query_embedding: Optional[Sequence[float]] = None,
    limit: int = 5,
    candidate_limit: int = 50,
) -> tuple[PublicKnowledgeHit, ...]:
    query = str(query or "").strip()
    if not query or len(query) > 500:
        raise PublicKnowledgeError("query must contain between 1 and 500 characters.")
    limit = min(max(int(limit), 1), 20)
    candidate_limit = min(max(int(candidate_limit), limit), 200)
    rows = active_public_knowledge(
        organization_id=organization_id,
        organization_domain=organization_domain,
    )
    text_ids = (
        _postgres_text_lane(rows, query, limit=candidate_limit)
        if connection.vendor == "postgresql"
        else _portable_text_lane(rows, query, limit=candidate_limit)
    )
    vector_ids = (
        _vector_lane(rows, query_embedding, limit=candidate_limit)
        if query_embedding is not None
        else []
    )
    text_ranks = {value: index for index, value in enumerate(text_ids, start=1)}
    vector_ranks = {value: index for index, value in enumerate(vector_ids, start=1)}
    ids = set(text_ranks) | set(vector_ranks)
    if not ids:
        return ()
    by_id = {
        item.pk: item
        for item in rows.filter(pk__in=ids)
    }
    ranked = []
    for item_id in ids:
        item = by_id.get(item_id)
        if item is None:
            continue
        text_rank = text_ranks.get(item_id)
        vector_rank = vector_ranks.get(item_id)
        score = (
            (1 / (RRF_K + text_rank) if text_rank else 0)
            + (1 / (RRF_K + vector_rank) if vector_rank else 0)
        )
        ranked.append(
            PublicKnowledgeHit(
                item=item,
                score=score,
                text_rank=text_rank,
                vector_rank=vector_rank,
            )
        )
    ranked.sort(key=lambda hit: (-hit.score, -hit.item.published_at.timestamp(), str(hit.item.pk)))
    return tuple(ranked[:limit])


def answer_public_knowledge_query(
    *,
    query: str,
    organization_id=None,
    organization_domain=None,
    limit: int = 5,
) -> dict:
    hits = search_public_knowledge(
        query=query,
        organization_id=organization_id,
        organization_domain=organization_domain,
        limit=limit,
    )
    if not hits:
        return {
            "status": "abstained",
            "answer": "I don’t have published public knowledge that supports an answer to that question.",
            "citations": [],
        }
    answer = "\n\n".join(hit.item.body for hit in hits)
    return {
        "status": "answered",
        "answer": answer[:12000],
        "citations": [
            {
                "item_id": str(hit.item.pk),
                "public_key": hit.item.public_key,
                "revision": hit.item.revision,
                "title": hit.item.title,
                "published_at": hit.item.published_at,
            }
            for hit in hits
        ],
    }
