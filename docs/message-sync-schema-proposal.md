# Message synchronization schema and test authorization

This is the specific additive database change proposed for the authorized reliability implementation. The user explicitly approved this exact migration and disposable local test scope on 14 September 2026. The subsequent instruction to roll up related MLAI Chat PRs, merge to main and deploy also authorizes the coordinated production release; production validation still uses the release and rollback checks below.

## Backend migration: integrations.0047_message_sync_reliability

Depends on integrations.0046_communitybridgedeletionrequest. Add these models; retain existing public/private message and delivery ownership boundaries.

| Table/model | Fields and constraints |
| --- | --- |
| `bridge_sync_inbox` / BridgeSyncInbox | Big integer primary key; app ID, workspace ID and source event ID (unique together); encrypted payload using existing EncryptedTextField; status; available-at; attempt count; UUID lease token; lease expiry; bounded last-error code; received/completed/updated timestamps. Index status/available-at. No plaintext private message JSON. |
| `bridge_sync_state` / BridgeSyncState | Big integer primary key; nullable public-channel FK and nullable private-conversation FK with exactly-one check and separate uniqueness constraints; workspace/source channel identifiers; authority generation; archive/head cursors and verified range metadata; latest source activity; last successful scan/delivery times; next due; status/error code; scheduling last-served time. Private authority remains inherited from its conversation/grant. |
| `bridge_sync_job` / BridgeSyncJob | Big integer primary key; sync-state FK; kind (head/archive/thread/authority); source object key (empty for channel work); unique state/kind/object; cursor/range checkpoint JSON containing no message bodies; due time; priority lane; last-served time; lease UUID/expiry; attempt/backoff state; bounded error code; creation/completion times. Index kind/due-time and lease expiry. Known thread jobs remain durable after scans finish. |
| `bridge_api_budget` / BridgeApiBudget | Unique app/workspace/method tuple; next admitted request time; provider cooldown deadline; updated-at. Atomic admission across workers; contains no provider token. |
| `bridge_worker_heartbeat` / BridgeWorkerHeartbeat | Unique worker ID/lane; last heartbeat and last successful progress timestamps; counts and error code. Content-free operational health. |
| Existing CommunityBridgeDelivery | Add canonical_envelope JSON (public bridge only, default empty), lease UUID/expiry, and source revision string; no changes to existing message identity or payloads. |

All changes are new tables or nullable/defaulted fields. No existing messages are deleted, rewritten, or imported by this migration. Populate jobs incrementally with ordinary resumable worker operations after feature enablement. Database checks and indexes enforce scope and uniqueness. Reverse migration would drop new state, so operational rollback disables the capability while retaining tables rather than reversing the schema after use.

## Relay migration: 0030_message_sync

In mlai-chat, add community-scoped transactional change counters, a durable change/outbox table, and sync dispatch checkpoints. Event insert/update/delete and access changes create records in their originating transaction. A locked counter establishes commit-safe ordering; clients continue using the existing query/WebSocket surfaces with an opt-in capability. No destructive message rewrite. Initial clients receive a current authorized baseline before consuming new changes; existing history is not copied into the change log by the migration.

The relay schema and its indexes will be committed in the relay repository, alongside authorization and concurrency tests. Public/private access is checked at every replay, independent of stored cursor values.

## Requested local validation scope

Create the two additive migrations above and run them only against newly created disposable local PostgreSQL/SQLite test databases with synthetic fixtures. The existing backend migration baseline is listed with exact file hashes in [message-sync-baseline-migrations.json](message-sync-baseline-migrations.json), plus the installed Django/third-party framework migrations required by the existing test runner. The relay baseline is the existing 0001–0029 migration set at chat commit 40ed3dec.

Use isolated database names and local-only test credentials; do not read existing application databases or load production .env files. Run relevant transaction/lease/replay/permission/failure tests and the required test suites. Drop only the named disposable test databases after verification.

The local-test approval alone did not authorize production application. The later explicit deployment instruction supplies that authorization. Production rollout follows the separately reviewable release manifest and rollback checks in the implementation plan; populated additive migrations are retained during application rollback.
