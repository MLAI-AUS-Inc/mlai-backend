# Organisational-memory durable runtime

PR6 provides the transactional dispatcher, leased worker, and daily scheduler
for Admin Roo's organisational memory. It does not grant Public Roo access and
does not enable a provider merely because its worker service is running.

## Safety and durability invariants

- An outbox event and its idempotent work item are converted transactionally.
- A work item has at most one active lease. Claiming uses
  `select_for_update(skip_locked=True)` when the database supports it.
- Workers heartbeat on a separate database connection. Expired leases return
  work to the queue, or dead-letter it after the final permitted attempt.
- Organisation and provider lanes serialize concurrency decisions. Provider
  lanes also persist `retry_after` throttles across worker processes.
- Every connection action is represented by one `MemorySyncRun`; a partial
  unique constraint permits only one pending/running run per connection.
- Provider page records, removals, source versions, ACLs, checkpoints, and the
  next cursor commit in one transaction. A failed page advances nothing.
- Paused connections are not claimable. Policy/scope invalidation and deletion
  cancel queued or in-flight configuration work and release its leases.
- Credentials and source bodies are forbidden in work/outbox payloads. Queue
  rows carry IDs, cursors remain on the protected connection record, and source
  text only enters the immutable evidence kernel.
- Source reconciliation also schedules one version-fingerprinted extraction
  job. Extraction creates reviewed candidates only; it cannot activate memory,
  call tools, change permissions, or publish to Public Roo. See
  `docs/org-memory-extraction.md`.
- Successful extraction schedules identifier-only consolidation work.
  Deterministic deduplication, reviewed supersession/contradiction, temporal
  state, and current-state projections are described in
  `docs/org-memory-consolidation.md`.
- Metadata-only provider adapters raise an explicit permanent failure. A real
  provider adapter, deployment allowlist, organisation approval, and governance
  approval are all required before content execution succeeds.

## Services

Production and local Compose files define:

```text
memory-worker
  python manage.py run_memory_worker

memory-scheduler
  python manage.py schedule_memory_work every bounded poll interval
```

The scheduler recovers expired leases, computes claim staleness, dispatches
outbox events, coordinates the idempotent daily reconciliation/report window,
safely generates review summaries and digests after a fully healthy report,
schedules provider-specific due connections, and converts pending action
requests into work. The worker claims and executes persisted work. Neither
performs long ingestion inside the web process or the existing jobs scheduler.
Daily source health, watch renewal, catch-up, and model-cost ceilings are
documented in `docs/org-memory-daily-reconciliation.md`.
Review operations and derived-artifact lineage are documented in
`docs/org-memory-review-summaries-digests.md`.
The optional, separately indexed public publication boundary is documented in
`docs/org-memory-publication.md`.
The disabled-by-default Admin Roo action proposal, approval, execution,
reversal, and ingestion gateway is documented in
`docs/org-memory-actions.md`.

## Operator commands

```bash
# One diagnostic worker poll
python manage.py run_memory_worker --once

# Normal continuous worker
python manage.py run_memory_worker

# One scheduler/dispatcher cycle
python manage.py schedule_memory_work

# Force today's daily report before its configured local hour
python manage.py schedule_memory_work --force-daily

# Force one approved active connection to become due
python manage.py schedule_memory_work --configuration <uuid> --force

# Counts only; never source text
python manage.py memory_queue_status
python manage.py memory_queue_status --fail-on-degraded

# Requeue one non-action/source dead letter after investigation
python manage.py requeue_memory_dead_letter <dead-letter-uuid>
```

Connection-action dead letters are deliberately not replayed in place. Repair
the connection/policy and submit a new idempotent action so its approval and
execution gates are evaluated again.

The authenticated `GET /api/v1/org-memory/health` endpoint adds pending,
processing, failed-outbox, expired-lease, active-run, review, and dead-letter
counts to evidence invariants. Django admin exposes sync runs and runtime lanes
as read-only operational records.

## Configuration

The safe defaults are documented in `.env.example`:

```text
ORG_MEMORY_WORKER_LEASE_SECONDS=120
ORG_MEMORY_WORKER_HEARTBEAT_SECONDS=30
ORG_MEMORY_WORKER_POLL_SECONDS=2
ORG_MEMORY_WORKER_MAX_ATTEMPTS=5
ORG_MEMORY_ORGANIZATION_CONCURRENCY=1
ORG_MEMORY_PROVIDER_CONCURRENCY=4
ORG_MEMORY_RETRY_BASE_SECONDS=30
ORG_MEMORY_RETRY_MAX_SECONDS=3600
ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400
ORG_MEMORY_FRESHNESS_SLO_SECONDS=86400
ORG_MEMORY_DAILY_RECONCILIATION_TIME_ZONE=Australia/Sydney
ORG_MEMORY_DAILY_RECONCILIATION_HOUR=5
ORG_MEMORY_SCHEDULER_POLL_SECONDS=60
```

`ORG_MEMORY_ENABLED_PROVIDERS` remains empty by default. Starting these runtime
services therefore does not activate private provider ingestion.
