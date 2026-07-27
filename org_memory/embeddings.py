from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Sequence

from django.conf import settings
from django.db import transaction

from .kernel import EvidenceKernelError, create_work_item
from .models import (
    MemoryChunk,
    MemoryChunkEmbedding,
    MemoryClassification,
    MemorySourceLifecycle,
    MemoryWorkItem,
    MemoryWorkTaskType,
)


SCHEMA_EMBEDDING_DIMENSIONS = 1536


class EmbeddingError(RuntimeError):
    pass


class EmbeddingConfigurationError(EmbeddingError):
    pass


class EmbeddingInvariantError(EmbeddingError):
    pass


class EmbeddingProvider(Protocol):
    def embed(self, text: str, *, model: str, dimensions: int) -> Sequence[float]: ...


class OpenAIEmbeddingProvider:
    def embed(self, text: str, *, model: str, dimensions: int) -> Sequence[float]:
        from openai import OpenAI

        try:
            client = OpenAI()
        except Exception as exc:
            raise EmbeddingConfigurationError(
                "The OpenAI embedding client is not configured."
            ) from exc
        response = client.embeddings.create(
            input=text,
            model=model,
            dimensions=dimensions,
            encoding_format="float",
        )
        if not response.data:
            raise EmbeddingError("The embedding provider returned no vector.")
        return response.data[0].embedding


@dataclass(frozen=True)
class EmbeddingTarget:
    model: str
    version: str
    dimensions: int


def configured_embedding_target(
    *,
    model: Optional[str] = None,
    version: Optional[str] = None,
    dimensions: Optional[int] = None,
) -> EmbeddingTarget:
    target = EmbeddingTarget(
        model=str(model or settings.ORG_MEMORY_EMBEDDING_MODEL).strip(),
        version=str(version or settings.ORG_MEMORY_EMBEDDING_VERSION).strip(),
        dimensions=int(dimensions or settings.ORG_MEMORY_EMBEDDING_DIMENSIONS),
    )
    if not target.model or not target.version:
        raise EmbeddingConfigurationError("Embedding model and version are required.")
    if target.dimensions != SCHEMA_EMBEDDING_DIMENSIONS:
        raise EmbeddingConfigurationError(
            f"This schema requires {SCHEMA_EMBEDDING_DIMENSIONS}-dimension embeddings."
        )
    return target


def normalize_vector(vector: Iterable[float], *, dimensions: int) -> list[float]:
    try:
        normalized = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingInvariantError("Embedding values must be numeric.") from exc
    if len(normalized) != dimensions:
        raise EmbeddingInvariantError(
            f"Expected {dimensions} embedding values; received {len(normalized)}."
        )
    if not all(math.isfinite(value) for value in normalized):
        raise EmbeddingInvariantError("Embedding values must be finite.")
    if not any(value != 0 for value in normalized):
        raise EmbeddingInvariantError("An all-zero embedding cannot be cosine-ranked.")
    return normalized


def vector_digest(vector: Sequence[float]) -> str:
    payload = json.dumps(vector, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _eligible_chunk_queryset():
    return MemoryChunk.objects.filter(
        active_for_retrieval=True,
        classification__in=[
            value
            for value in MemoryClassification.values
            if value != MemoryClassification.NO_AGENT
        ],
        source_version__is_current=True,
        source_version__tombstoned_at__isnull=True,
        source_version__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source_version__source__access_revoked_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
    )


@transaction.atomic
def store_chunk_embedding(
    *,
    chunk: MemoryChunk,
    vector: Iterable[float],
    model: Optional[str] = None,
    version: Optional[str] = None,
    dimensions: Optional[int] = None,
) -> tuple[MemoryChunkEmbedding, bool]:
    target = configured_embedding_target(
        model=model,
        version=version,
        dimensions=dimensions,
    )
    locked_chunk = (
        _eligible_chunk_queryset()
        .select_for_update()
        .select_related("source_version__source")
        .filter(pk=chunk.pk)
        .first()
    )
    if locked_chunk is None:
        raise EmbeddingInvariantError(
            "Only current, accessible, retrieval-active chunks may be embedded."
        )
    normalized = normalize_vector(vector, dimensions=target.dimensions)
    digest = vector_digest(normalized)
    existing = MemoryChunkEmbedding.objects.filter(
        chunk=locked_chunk,
        model=target.model,
        version=target.version,
    ).first()
    if existing is not None:
        if existing.vector_hash != digest:
            raise EmbeddingInvariantError(
                "An embedding version is immutable and already has different vector data."
            )
        if not existing.is_current:
            MemoryChunkEmbedding.objects.filter(
                chunk=locked_chunk,
                is_current=True,
            ).exclude(pk=existing.pk).update(is_current=False)
            existing.is_current = True
            existing.save(update_fields=("is_current", "updated_at"))
        MemoryChunk.objects.filter(pk=locked_chunk.pk).update(
            embedding_model=target.model,
            embedding_version=target.version,
        )
        return existing, False

    MemoryChunkEmbedding.objects.filter(
        chunk=locked_chunk,
        is_current=True,
    ).update(is_current=False)
    embedding = MemoryChunkEmbedding(
        organization_id=locked_chunk.source_version.source.organization_id,
        chunk=locked_chunk,
        model=target.model,
        version=target.version,
        dimensions=target.dimensions,
        vector=normalized,
        vector_hash=digest,
        is_current=True,
    )
    embedding.full_clean()
    embedding.save()
    MemoryChunk.objects.filter(pk=locked_chunk.pk).update(
        embedding_model=target.model,
        embedding_version=target.version,
    )
    return embedding, True


def generate_embedding(
    text: str,
    *,
    target: Optional[EmbeddingTarget] = None,
    provider: Optional[EmbeddingProvider] = None,
) -> list[float]:
    target = target or configured_embedding_target()
    if not str(text or "").strip():
        raise EmbeddingInvariantError("Embedding input must not be empty.")
    max_chars = int(getattr(settings, "ORG_MEMORY_EMBEDDING_MAX_CHARS", 24000))
    if max_chars < 1 or len(text) > max_chars:
        raise EmbeddingInvariantError(
            "Embedding input exceeds the configured chunk-size contract."
        )
    raw = (provider or OpenAIEmbeddingProvider()).embed(
        text,
        model=target.model,
        dimensions=target.dimensions,
    )
    return normalize_vector(raw, dimensions=target.dimensions)


def process_embedding_work(
    work_item: MemoryWorkItem,
    *,
    provider: Optional[EmbeddingProvider] = None,
) -> dict:
    payload = work_item.payload or {}
    chunk_id = str(payload.get("chunk_id") or "")
    if not chunk_id:
        raise EmbeddingInvariantError("Embedding work is missing chunk_id.")
    chunk = _eligible_chunk_queryset().select_related("source_version__source").filter(
        pk=chunk_id
    ).first()
    if chunk is None:
        raise EmbeddingInvariantError("Embedding work references ineligible evidence.")
    if (
        chunk.source_version_id != work_item.source_version_id
        or chunk.source_version.source_id != work_item.source_id
        or chunk.source_version.source.organization_id != work_item.organization_id
    ):
        raise EmbeddingInvariantError("Embedding work ownership does not match its chunk.")
    target = configured_embedding_target(
        model=payload.get("model"),
        version=payload.get("version"),
        dimensions=payload.get("dimensions"),
    )
    vector = generate_embedding(chunk.text, target=target, provider=provider)
    embedding, created = store_chunk_embedding(
        chunk=chunk,
        vector=vector,
        model=target.model,
        version=target.version,
        dimensions=target.dimensions,
    )
    return {
        "chunk_id": str(chunk.pk),
        "embedding_id": str(embedding.pk),
        "model": target.model,
        "version": target.version,
        "created": created,
    }


def schedule_chunk_embeddings(
    *,
    source_version=None,
    organization=None,
    model: Optional[str] = None,
    version: Optional[str] = None,
    dimensions: Optional[int] = None,
    limit: int = 1000,
) -> dict:
    if source_version is None and organization is None:
        raise EvidenceKernelError("A source version or organization is required.")
    target = configured_embedding_target(
        model=model,
        version=version,
        dimensions=dimensions,
    )
    chunks = _eligible_chunk_queryset().select_related("source_version__source")
    if source_version is not None:
        chunks = chunks.filter(source_version=source_version)
    if organization is not None:
        chunks = chunks.filter(source_version__source__organization=organization)
    chunks = chunks.exclude(
        embeddings__model=target.model,
        embeddings__version=target.version,
    ).distinct().order_by("created_at", "pk")[: max(int(limit), 0)]
    scheduled = 0
    existing = 0
    for chunk in chunks:
        target_fingerprint = hashlib.sha256(
            f"{target.model}:{target.version}:{chunk.content_hash}".encode("utf-8")
        ).hexdigest()
        _work, created = create_work_item(
            organization=chunk.source_version.source.organization,
            provider=chunk.source_version.source.provider,
            task_type=MemoryWorkTaskType.EMBED,
            source=chunk.source_version.source,
            source_version=chunk.source_version,
            configuration=chunk.source_version.source.configuration,
            idempotency_key=f"embed:{chunk.pk}:{target_fingerprint}",
            payload={
                "chunk_id": str(chunk.pk),
                "model": target.model,
                "version": target.version,
                "dimensions": target.dimensions,
            },
        )
        if created:
            scheduled += 1
        else:
            existing += 1
    return {
        "scheduled": scheduled,
        "existing": existing,
        "model": target.model,
        "version": target.version,
        "dimensions": target.dimensions,
    }
