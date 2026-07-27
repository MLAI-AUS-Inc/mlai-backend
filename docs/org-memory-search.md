# Organisational-memory PostgreSQL search

PR7 adds the storage and operational boundary for hybrid organisational-memory
retrieval. It does not expose a retrieval endpoint to Public Roo or Admin Roo;
the permission-filtered answer API is added in a later vertical-slice PR.

## Storage contract

- `MemoryChunk.search_vector` stores an English PostgreSQL `tsvector` and has a
  GIN index named `orgmem_chunk_search_gin`.
- `MemoryChunkEmbedding` retains immutable `(chunk, model, version)` vectors.
  Exactly one vector is current for a chunk, and retrieval always filters by a
  single configured model, version, and 1,536-dimension schema.
- `orgmem_embed_vector_hnsw` is a cosine HNSW index. SQLite migrations retain
  model state but deliberately omit this PostgreSQL-only physical index.
- Historical vectors remain available for audit and rollback. A version cannot
  be overwritten with different vector data.
- Search applies organisation, source lifecycle, current-version, source ACL,
  classification, and retrieval-active filters in the database before results
  leave the retrieval service.

The current embedding target is configured by:

```text
ORG_MEMORY_EMBEDDING_MODEL=text-embedding-3-small
ORG_MEMORY_EMBEDDING_VERSION=openai-text-embedding-3-small-v1
ORG_MEMORY_EMBEDDING_DIMENSIONS=1536
ORG_MEMORY_EMBEDDING_MAX_CHARS=24000
```

Changing dimensions requires an explicit schema migration. Changing the model
or processing contract requires a new `ORG_MEMORY_EMBEDDING_VERSION`; reusing a
version for different vector data fails closed.

## Deployment guard

Controlled Postgres environments use the pinned
`pgvector/pgvector:0.8.2-pg15-bookworm` image. Before migrations, deployment
runs:

```bash
python manage.py check_org_memory_search --require-vector
```

This checks `pg_available_extensions` and stops the release before the vector
migration if the database cannot provide pgvector. Migration installs the
extension, creates the vector/text fields, and builds the indexes. Deployment
then verifies installation and refreshes all stored text vectors:

```bash
python manage.py check_org_memory_search --require-vector --require-installed
python manage.py rebuild_memory_search_vectors
```

The GitHub Actions PostgreSQL lane starts the same pinned image, migrates a real
PostgreSQL database, checks both indexes, and verifies GIN/HNSW query plans. The
normal unit-test lane remains SQLite-backed.

## Embedding and re-embedding

New source-version outbox events refresh their text vectors and enqueue one
idempotent `embed` work item per eligible chunk. The existing leased worker calls
the embedding provider, validates finite/non-zero dimensions, and atomically
promotes the resulting version. Source text never enters queue payloads.

To queue a new version for one organisation:

```bash
python manage.py reembed_org_memory \
  --organization example.com \
  --version openai-text-embedding-3-small-v2 \
  --limit 1000
```

To queue all organisations after reviewing cost and provider capacity:

```bash
python manage.py reembed_org_memory \
  --all \
  --version openai-text-embedding-3-small-v2 \
  --limit 10000
```

Repeated commands are idempotent for the same chunk/content/version. Superseded
or access-revoked evidence is ineligible and is never embedded.

## Degraded mode

`search_memory_chunks` fuses text and vector ranks with reciprocal-rank fusion.
If query embedding generation or the vector lane fails, it returns the exact
same permission-filtered text lane plus a machine-readable degraded reason.
SQLite uses a deterministic bounded text scorer so unit tests and local
development do not pretend to exercise PostgreSQL ranking.

An embedding failure therefore affects semantic recall, not memory availability.
The caller must preserve the degraded flag in future Admin Roo answer metadata.
