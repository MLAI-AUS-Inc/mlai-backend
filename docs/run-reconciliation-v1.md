# Admitted Run history reconciliation v1

This is the backend consumer contract for the existing admitted event v1
protocol. It does not activate new admission, change a deployment flag, repair
historical Jobs, or confer approval, billing, delivery or repository authority.
The paired CF producer and HTTP route require their own integration proof.

## Authenticated read transport

`GET /api/runs/{worker_run_id}/events?after_seq=0&limit=100`

Use the configured internal `X-API-KEY` service authentication. The endpoint
must not create an artifact directory, Run, journal, task, provider request or
status snapshot. A genuinely missing Run returns 404 without materialization.
Invalid bounds return 422; authentication failure returns 401/403. Redirects
are refused by the consumer. The default and maximum event count is 100.

The exact 200 response has these five fields:

```json
{
  "schema_version": 1,
  "run_id": "existing-worker-run",
  "batches": [],
  "next_cursor": 0,
  "has_more": false
}
```

Each `batches` entry is an unchanged `run_events_v2` envelope described in
`run-event-authority-v1.md`: exact original admission, retained execution
ticket and original digest-covered event bytes. A page may contain multiple
tickets. Batches and their events are in strictly increasing global journal
sequence order, with at most 100 events across the entire page. All run IDs
must equal the requested worker ID. There are no snapshot, approval or money
fields in this response.

`after_seq` and `next_cursor` are nonnegative typed integers below 2^53. They
refer to the durable **per-Run global journal**, across every execution ticket
and generation; counters never reset at resume or source re-pin. For a
nonempty page, `next_cursor` equals its final event sequence. An empty page
keeps the requested cursor and cannot claim `has_more=true`. Reserved gaps
remain unknown gaps in the backend immutable ledger: this cursor never
asserts that every integer in an interval was received.

The producer must expose only a stable journal prefix: it must not later
reconstruct or insert an older event behind an already returned cursor. If a
reserved position can still acquire an event, the returned prefix must stop
before it. Durable journal recovery or an explicit fail-closed refusal is
required; wall-clock reconstruction and silent gap compaction are forbidden.

## Backend runner and claims

`run_authoritative_reconciliation_sweep(now=None, limit=10)` is registered
unconditionally before the legacy reconciliation runner. It considers every
explicit backend event admission in `(RUN_STATUSES | PLATFORM_EXTENSIONS) -
TERMINAL`, with no workflow filter or active-only filter. A dispatch with a
backend `pending_resolution` receipt also remains eligible even when its
provisional transport state is blocked. New-admission runtime flags can be
off without abandoning an already admitted execution. Historical and
reservation-only rows never enter this path. If a page projects a terminal
state while announcing more history, the runner drains those known remaining
pages without reopening the terminal Run, then stops polling it.

Each Run stores a private `run_owner_reconciliation_v1` JSON receipt: cursor,
last attempt, next due time, bounded retry count and a random claim nonce.
Claims lock only the existing canonical Run. A 60-second lease permits retry
after process loss; response application checks the exact nonce, canonical PK,
worker ID and previous cursor. A replaced claim cannot apply an older reply.
The durable last-attempt ordering, then original creation time and PK, gives
unvisited Runs priority over previously attempted Runs. One tick selects at
most its explicit limit (maximum 100), and fetches at most one page per Run.
The sweep stops claiming new work after 45 seconds. Each streamed history
read also has a 15-second elapsed-time budget, checked between bounded socket
reads, in addition to its 3-second connection and 10-second read timeouts.
Unavailable or damaged admissions back off; they cannot monopolize a bounded
tick ahead of healthy executions.

All network I/O occurs outside database transactions. The consumer refuses
redirects, bounds response bytes at 8 MiB, validates every event shape/digest
and retained admission/ticket before applying a page, then ingests through
`receive_authoritative_run_events`. Receipt append, typed projections and
cursor advancement share one transaction. A later conflicting batch rolls
back the page and preserves the old cursor for an exact replay. Old-ticket
facts remain immutable history; only the current backend ticket can project
lifecycle/steps. Local completion, cancellation and denial retain precedence.

Pull/retry metadata never updates `created_at`, `updated_at`,
`last_event_emitted_at` or `reconciled_at`, original request/body/source,
tenant/principal, approval, billing lineage or any account/ledger balance.
An event's actual forward lifecycle projection retains its separate private
`last_projected_at` clock. Cost events remain observations, never debits.

The legacy reconciliation candidate and direct-effect helpers exclude
explicit event-owned Runs, including status snapshots, stalled recovery,
redrive, terminal refund and restart bookkeeping. Legacy child lookups still
see owned children: excluding them would invent absence and risk a refund.
The other owner package must additionally retire competing goal/doctor/wake
and callback snapshot writers for these admissions before activation.

## Freshness for a backend control grant

`reconcile_authoritative_run(run_id, now=None, refresh=False)` reuses exactly
the same claim, one-page transport, typed receiver and cursor transaction.
It returns an internal status/outcome, current worker ID, cursor, has_more,
active_claim and current_execution_id. A trusted control endpoint can pass
the Python keyword `refresh=True` to bypass the normal poll cadence, including
for a terminal admission it is considering for an explicit authorized resume.
It never steals an unexpired nonce, reads under a database lock, grants a new
ticket or reopens a terminal Run. Another active claim returns pending.

The control owner first reads the authenticated CF execution budget, including
its stable-prefix `event_cursor`, then refreshes history outside all database
locks. Before granting, it re-locks the canonical Run and requires
`outcome=history_received`, no remaining history, no active claim, the exact
current execution ticket/projection and persisted cursor equality with the
budget's captured event_cursor. A cursor behind that anchor requires another
drain; a cursor ahead means the control request is stale. Neither a worker
status string nor a caller-supplied cursor can substitute for that evidence.
The control package independently owns current rights, budget, operation,
source and idempotent ticket issuance.

## Original-key dispatch uncertainty

Pending dispatches query the existing authenticated exact original-key
lookup; the logical key can be 255 characters even when the reservation Run ID
uses a bounded alias. A confirmed worker ID is adopted through the existing
non-destructive admission binder. Canonical PK, history, source, spend and
clocks survive; occupied or mismatched identities remain held. This runner
never issues another dispatch POST.

Unknown, timeout, generic 404, missing route or mid-claim outcomes never prove
absence. Only confirmed `not_received`, `received_not_started` or `rejected`
from the exact-key lookup, after the existing 180-second original creation
grace and without any retained event, can produce a private no-effect proof.
`no_effect_dispatch_proof(run)` exposes the exact current admission/ticket/key
proof to the existing original-charge controller. A newer claim, unknown
lookup, bound dispatch, changed ticket or retained event invalidates it.

This proof is a **handoff**, not a refund instruction. The original-charge
controller must still freshly verify the original ledger/entitlement,
retained request and absence of effects before its existing settlement path.
No original request or execution clock is rewritten to fit a legacy refund
helper; no charge is released because the worker is unavailable.
No-effect proofs retain an immutable history keyed by claim nonce; only the
current fresh proof ID can be handed off, and it includes the complete current
execution ticket digest as well as the original admission digest.

## Activation gates

Paired authenticated endpoint/consumer integration, original-charge handoff,
current-rights control tickets, callback snapshot precedence, complete Run
fact ownership, competing controller retirement and capacity/performance
proof remain required. Passing this component's isolated tests does not prove
the full R3 transition or authorize runtime activation.
