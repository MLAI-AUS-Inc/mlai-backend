# Admitted run event authority, protocol 1

This is the paired backend/CF contract for **new explicitly admitted executions**.
It does not activate a flag, change historical Jobs, create a migration, or
replace the existing observational `run_events` protocol. A reducer alone is
not the complete R3 ownership transition. Admission, binding, backend fact
services, compatible readers and the paired producer must land together.

## Envelope and admission

The callback envelope has exactly these semantic fields:

```json
{
  "event": "run_events_v2",
  "schema_version": 1,
  "run_id": "the-original-CF-worker-run-id",
  "backend_run_admission_v1": { "...": "exact saved wire echo below" },
  "backend_run_execution_v1": { "...": "exact retained execution ticket below" },
  "events": [{ "...": "one or more immutable events below" }]
}
```

`event_type` is optional and, when present, is exactly `run_events_v2`.
Transport-only `event_id` and `emitted_at` may be added by the durable callback
outbox. They do not change event identity or digest. No other envelope fields
are accepted. The batch contains 1–100 events, one immutable admission echo and one retained
execution ticket. CF groups journal records by both echoes.

The wire echo is the backend receipt's `identity` object plus
`event_protocol: 1`:

```json
{
  "schema_version": 1,
  "dispatch_key": "original-client-request-id",
  "organization_id": "42",
  "principal": {"kind": "user", "user_id": "7", "actor_id": "U_FIXTURE"},
  "workflow": "article_generation",
  "domain": "example.test",
  "github_repo": "",
  "request_digest": "64 lowercase hexadecimal SHA256 characters",
  "scope": {
    "website_connection_id": null,
    "connection_generation": null,
    "connection_target_id": null,
    "repository_id": null,
    "operation_id": null,
    "operation_attempt": null,
    "deletion_epoch": null,
    "expected_source_sha": null,
    "source_run_id": null
  },
  "event_protocol": 1
}
```

The scheduler principal is exactly `{"kind":"scheduler","service":"..."}`.
The original dispatch key is retained exactly, up to 255 characters. A bounded
local provisional Run ID is an internal alias, never a replacement wire key.
Every scope key is present, with JSON null for an absent admitted value.
Do not derive missing keys, normalize an incoming tenant, or infer permission
from this worker echo. `run_ownership.require_event_identity(run,payload)`
compares it to the private backend `run_owner_v1` receipt and requires its
`authority.event_protocol == 1`. Reservation-only receipts have
`authority.backend_facts == false` and `authority.event_protocol == null`;
they do not transfer mirror or snapshot ownership.

The receiver locks an **existing bound Run**. It never admits, creates, binds,
renames or deletes a Run. The callback router must validate the original
admission and event envelope before any binding or materialization. The
canonical Run PK, source, original charge lineage and history survive binding.

## Backend-authorized execution ticket

A new source or operation must not inherit authority from the initial admission.
The private `run_owner_execution_v1` receipt is
`{current_execution_id, tickets: {execution_id: ticket}}`. Its wire echo is:

```json
{
  "schema_version": 1,
  "execution_id": "stable-backend-control-identity",
  "generation": 0,
  "admission_digest": "SHA256 of the canonical complete wire admission echo",
  "request_digest": "SHA256 of the validated current request inputs",
  "scope": {"...": "all nine complete current scope fields, explicit nulls"}
}
```

`require_event_execution(run,payload)` verifies the exact retained ticket and
its link to immutable admission. A fresh backend rights/source/operation grant
issues a new ticket when scope changes, **even when CF generation is unchanged**.
Only the current execution ID may project lifecycle/step facts; a retained old
ticket may append history. An unknown/forged ticket is refused. A worker cannot
issue a ticket, revive an older current ID, or authorize its own generation.
The current ticket's generation is returned by
`authorized_event_generation(run)`. Ticket IDs are immutable safe identifiers.

## Immutable events

Every event has `seq`, `generation`, `state_version`, `type`, `at`, and `digest`.
Sequence is a positive integer smaller than 2^53; generation and state version
are nonnegative integers smaller than 2^53. Booleans and numeric strings are
invalid. `at` is an aware ISO8601 timestamp; it is evidence, never ordering or
authority. The event generation must equal its exact retained execution ticket. A worker
cannot authorize a next generation or changed source by inventing a number.
Older-ticket events are retained as history without affecting current projection;
an unknown ticket or mismatched generation refuses.

Allowed types and additional fields:

| Type | Required fields | Optional fields | Meaning |
|---|---|---|---|
| `run_state` | `status` | `step` | Canonical `contracts/run-statuses.json` worker execution status; no approval, money or delivery proof. |
| `step_started` | `step`, `attempt` | | Attempt is a positive integer; step is a safe identifier. |
| `step_done` | `step`, `attempt`, `status` | | Status is completed/skipped/failed/blocked/cancelled. |
| `failure` | `failure` | `step` | Membership-only causal object below; no raw error text or worker policy. |
| `artifact` | `step`, `kind` | `artifact_id`, `content_sha256` | Safe identifiers and optional content digest only; never path, URL or content. |
| `cost` | at least one of `calls`, `tokens`, `input_tokens`, `output_tokens`, `usd` | `step`, `model` | Per-step cumulative observation; does not debit, reserve, refund or replace the accounting ledger. |

Failure's exact typed fields are `{code, stage, registry_version?, registry_sha256?}`.
The code/stage are safe identifiers, optional registry version is a positive
integer, and optional registry SHA is 64 lowercase hexadecimal characters.
Those diagnostics are **never compared** to the backend's deployed version or
hash. The backend's existing `classify_failure(code,stage)` derives local class,
retry and safe copy by membership. Unknown valid codes map to its registered
`UNREGISTERED_FAILURE_CODE` generic fallback (`unsupported`/`stop_contract`).
Their original unknown identifier is not rendered. No incoming class, retry,
action, message or findings are accepted. Failure alone never sets Run status.

Safe identifiers match `[A-Za-z0-9_.:-]{1,120}`. Counts are nonnegative integers
smaller than 2^53. `usd` is a finite nonnegative decimal **string**, at most 40
characters. SHA256 fields are exactly 64 lowercase hexadecimal characters.
Fields not listed for a type are rejected; nested worker dictionaries are not
merged into Run results. In particular no request/body, approval, billing,
repository mutation, private receipt or arbitrary result field can be supplied.

Digest is the lowercase hexadecimal SHA256 of the UTF8 encoding of this object:

```python
material = {
    "schema_version": 1,
    "run_id": original_worker_run_id,
    "admission": original_admission_echo,
    "execution": original_execution_ticket,
    "event": event_without_digest,
}
canonical = json.dumps(material, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False).encode("utf-8")
digest = hashlib.sha256(canonical).hexdigest()
```

Do not normalize, add defaults to, or reclassify the immutable material before
checking its digest. Validation/projection may subsequently derive safe views.
The [fixture](run-event-authority-v1.fixture.json) includes a computed digest.

CF records the full admission echo, execution ticket and CF state version **when
the event is recorded**. Flushing groups those immutable records by both echoes and sends
the original event bytes. Retry never rebuilds them from a later RUN_REQUEST,
re-pin, resume or status. Sequence recovery scans retained journal/state or
fails closed; it does not infer a new sequence from wall-clock time.

## Receipt, projection and replay

Immutable event identity is `(canonical Run PK, execution_id, generation, seq)`; state version
and the complete event body are covered by its digest. Identical replay is an
acknowledged no-op. Reusing the identity with a different digest conflicts,
including a changed state version, source echo or worker run ID. Append and
projection commit in the same database transaction under the Run row lock.
There is no interval compaction or pruning of the sole replay ledger. A bounded
recent display is optional; it does not replace retained event identities and
digests. Unknown gaps remain gaps.

Projection orders only the current execution ticket's facts by `(state_version, seq)`. Late
events remain history and cannot rewind a newer Run or attempt. Within an
attempt a finished step cannot return to running; starting another attempt
requires a strictly larger attempt. A worker does not reopen a stopped run in
the same authorized execution ticket. Completed/local cancelled/local denied Runs
retain final precedence. A new backend-authorized execution ticket may recover a failed or blocked
execution, through current backend control before worker dispatch.

Run-state completion says worker execution finished. It does not prove a
reviewed draft, deployed page, merged commit, current permission, owner consent
or a captured charge. Those existing delivery and settlement fences remain.
Event ingestion performs no HTTP, Slack delivery, repository operation,
charging/refund or approval mutation. Historical append/replay does not touch
Run creation, recency, reconciliation or snapshot clocks. Any dedicated new
event-progress clock records only a new forward projection, never a replay.

Snapshots and legacy callbacks for event-owned Runs cannot regain lifecycle,
step, request or backend-private receipt authority. Old unmarked executions
retain their current snapshot/Job/observational-event semantics.

The initial implementation stores the replay ledger in existing backend-owned
JSON. Production activation additionally requires a measured retained-ledger
size/capacity policy that does not silently drop digests or unseen gaps.
