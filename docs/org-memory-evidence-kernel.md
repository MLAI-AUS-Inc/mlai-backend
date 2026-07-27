# Organisational-memory evidence kernel

PR5 adds the durable, fail-closed persistence layer beneath Admin Roo. It does
not enable source ingestion, retrieval, embeddings, model extraction, or a
background worker. Those capabilities must use this kernel rather than writing
evidence tables directly.

## Core invariants

- `MemorySource` is the stable identity of one provider object.
- Every content change creates a new immutable `MemorySourceVersion`.
- A source can have at most one current version.
- Each version owns one captured `MemoryAclSnapshot`.
- `MemoryChunk` text, locator, hash, classification, and source version are
  immutable after creation.
- Old-version chunks are retained for authorised audit/history but are not
  active for current retrieval.
- `no_agent`, inaccessible, revoked, and tombstoned evidence never has an
  active retrieval chunk.
- Source bodies and credentials are forbidden in outbox/work metadata. Outbox
  payloads contain only bounded operational identifiers and counts.
- Source-version capture and its outbox event commit in the same transaction.
- Queued work never grants permission: the worker calls
  `validate_work_item_for_execution()` immediately before handling it.

`capture_source_version()` is idempotent on stable source identity plus
provider `version_key`. Reusing a version key for a different hash or
classification fails closed. Capturing a new version retires the previous
version and deactivates its chunks before the new version becomes current.

## ACL and retrieval boundary

An ACL snapshot records provider revision, external user/group references,
link-sharing state, bounded metadata, an integrity fingerprint, and captured or
revoked time. `is_accessible` must be supplied explicitly by a connector.

The kernel's `active_for_retrieval` flag is necessary but not sufficient for an
answer. Later retrieval must still apply organisation, classification,
capability, current provider-principal mapping, source lifecycle, version, and
ACL filters in SQL before any candidate reaches an embedding model, reranker,
LLM, log, or error response.

There is intentionally no private retrieval endpoint in PR5.

## Deletion and access loss

`revoke_source_access()` and configuration-policy invalidation synchronously:

1. mark the source access-revoked;
2. mark its current ACL inaccessible;
3. deactivate all current chunks; and
4. create an idempotent reconciliation outbox event.

`tombstone_source()`, connection deletion, organisation startup-data deletion,
and Gmail disconnect synchronously:

1. deactivate every source chunk;
2. retire/tombstone every version;
3. cancel pending source work;
4. clear the source's current-version pointer;
5. write a `MemoryDeletionRequest`; and
6. create an idempotent tombstone outbox event.

Full company teardown explicitly removes protected memory configurations,
service principals, and dead letters in dependency order before deleting the
organisation. Reset-in-place keeps audit history but tombstones evidence.

## Review and runtime primitives

`MemoryReviewItem` is the canonical idempotent review queue for sensitivity,
source access, claim activation, contradictions, corrections, stale records,
entity merges, and later publication decisions.

PR5 also defines:

- `MemoryOutboxEvent` for transactional dispatch;
- `MemoryWorkItem` with task/status/idempotency/retry fields;
- `MemoryWorkerLease` with one active lease per work item and explicit expiry;
- `MemoryDeadLetter` preserving the terminal failure snapshot; and
- `MemoryDeletionRequest` as the organisational-memory deletion registry.

PR6 implements dispatch, claiming with `select_for_update(skip_locked=True)`,
heartbeat/recovery, retries, concurrency limits, rate-limit lanes, cursor-safe
sync runs, and bounded dead-letter/requeue operations. Provider execution still
requires a reviewed provider adapter; the metadata-only adapters fail closed.
See `docs/org-memory-runtime.md` for the operator contract.

## Operations

Authorised source managers can call:

```text
GET /api/v1/org-memory/health
```

The organisation-scoped response contains counts only—never source text. It
returns HTTP 503 when it detects an active chunk attached to a historical
version, inactive source, missing/revoked ACL, or `no_agent` classification.
Unresolved dead letters report `degraded` without making the endpoint public.

Django admin exposes read-only source versions, ACLs, chunks, outbox events,
work items, leases, dead letters, and deletion records. Operators may assign
and resolve review items. The only source mutations are explicit admin actions
for access revocation and tombstoning.
