# Articles B5 implementation evidence — 14 October 2026

This implements the approved initial B5 delivery slice alongside B1. It is not
the entire one-shot runbook or a release. The approved design is
[backend design PR #1012](https://github.com/MLAI-AUS-Inc/mlai-backend/pull/1012);
the causal failure contract is
[B1 design PR #981](https://github.com/drsamdonegan/content-factory/pull/981).

## Scope and enrollment

The initial protocol covers original backend-reserved `article_system_setup`
and `repo_scan` executions. Independently bound review-update executions and
internal child executions retain the legacy protocol. Unrelated article
generation callbacks retain their existing schema and size behavior. This
sequencing choice does not establish universal callback delivery or the nightly
90% live-article goal.

New enrollment requires `CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED` and
the saved organization primary key in
`CONTENT_FACTORY_SETUP_DELIVERY_ENROLLED_ORGANIZATIONS`. The backend stores
`setup_delivery_v1 = {schema_version: 1}`, the original scope, and a bounded
original request on its reserved operation before dispatch. Worker input cannot
create that authority. Enrollment remains sticky when a new attempt is reserved;
an independent review child cannot inherit its parent's marker.

All new flags default off. With enrollment off, existing legacy request bytes
remain unchanged. With apply off, new unprocessed delivery returns retryable
503 without claiming or applying state; an identical already processed receipt
can still be acknowledged. The separate reconciliation flag can validate pulls
in shadow mode while apply is off. Shadow mode writes only its local transport
lease/probe metadata, never a business projection or processed delivery receipt.
Disabling flags preserves pending and unacknowledged records.

## Receiver and state projection

[Contract validation](../content_factory/setup_delivery_contract.py) checks the
strict versioned schema before any claim, binding, run creation, or write.
Unknown fields, malformed identity, contradictory causal aliases, invalid proof
types, secrets, raw source/log text, excessive depth/size, and digest mismatches
return bounded typed 422 diagnostics containing field pointers. Pending reports
may contain blank optional proof fields; blank values never satisfy the existing
exact-commit proof verifier. Structured repair diagnostics remain distinct from
an actionable repair status.

Canonical UTF-8 JSON uses sorted keys, compact separators, `ensure_ascii=False`
and `allow_nan=False`. Finite diagnostic numbers remain valid. The payload hash
covers the complete immutable envelope except its hash field; the state hash
covers `{scope, state_projection}`. Journal, leases, acknowledgments and
transport receipts are absent from the shared state projection, avoiding a
self-reference or acknowledgment feedback loop.

[Reception](../content_factory/setup_delivery_receiver.py) uses the existing
callback ledger and reserved `ContentFactoryRun.result.delivery_v1` JSON,
without a migration. A short transaction commits the claim first. A 120-second
lease with a nonce permits recovery after a claimant dies; a superseded claimant
cannot acknowledge another claimant's work. The second transaction repeats
original scope, operation, cancellation, generation and pull-pin fences before
committing the state and receipt together. Provider verification runs between
these transactions, outside database locks. The existing lifecycle lock order is
organization, website, operation, run and callback receipt.

State ordering compares `(generation, state_version)`. Equal-version different
state is quarantined; equal-state callback, mirror and pull are idempotent.
Higher execution generations require an explicit backend-reserved resume fence.
The original B1 causal object is preserved unchanged. Its persisted occurrence
identifier deduplicates the same execution outcome across distinct transport
events; B5 does not invent goal-wide B4 failure counts or reclassify a causal
failure as a delivery problem.

The new projector does not invoke legacy callback effect handlers or
`_apply_run_snapshot`: the latter currently has completion-memory and startup
effects. Projection cannot send notifications, refund credits, dispatch work,
approve, merge, or publish. Backend owner approval and previously accepted proof
remain protected against sparse/negative progress. Generic setup/scan readiness
claims become observations. Setup proof uses the existing exact-commit verifier;
real scan evidence uses the current source identity, consent, scan and target
validators, rather than treating generic summaries as proof.

Late observations after an ordinary terminal result return 200 without changing
the terminal business state. Exact original scope that is cancelled, revoked or
superseded returns typed terminal 409 with an identity-bound acknowledgment.
Unknown/mismatched scope does not receive a successful acknowledgment. Genuine
live-lease/capacity 409 remains retryable. The receiver bounds retained receipts
at 2,048 per run, alerts through a typed capacity response, and does not prune
unacknowledged records to make room.

## Reconciliation and compatibility

[Reconciliation](../content_factory/setup_delivery_reconciliation.py) extends
the existing scheduler. It examines at most ten stale enrolled runs, applies a
two-minute probe interval and lease, and uses one authenticated GET to the
configured Content Factory origin with `view=delivery_snapshot`. Redirects are
disabled, timeouts are bounded, and streamed response bytes are capped at 2 MiB.
Missing, partial, malformed or corrupt state remains unavailable; no failed or
successful state is fabricated. A concurrent callback or changed original
binding invalidates the pull candidate before apply.

The pull uses the same receiver and projector. Healthy repeat observations do
not count as repairs. A recovered committed state increments
`delivery_reconciler_repairs_total` and records a warning that normal delivery
was missed. Fully acknowledged owner-waiting gates are not polled continuously.

Legacy snapshot merging preserves reserved delivery metadata and rejects worker
attempts to introduce enrollment. Legacy recovery/redrive/refund paths exclude
enrolled runs. Those filters explicitly include missing JSON paths so SQL NULL
semantics do not accidentally hide ordinary legacy runs.

## Local verification and limits

The database-free command was run with database connections, sockets and dotenv
loading explicitly disabled:

```sh
python scripts/test_without_database.py \
  content_factory.tests_setup_delivery_unit \
  content_factory.tests_reconciliation_unit
```

**56 tests passed.** Tests run actual DRF parsing/authentication/endpoints,
contract decoding, identity fences, ledger claims, state projection and
reconciliation; only ORM persistence and network boundaries are replaced. They
cover malformed input before writes, exact acknowledgments, replays/collisions,
ordering, resume and cancellation fences, terminal observations, concurrent
pulls, lost pushes, settings-off compatibility, proof bypasses, pending fields,
causal alias conflicts, privacy and the audited callback event vocabulary.

The first remote privacy-boundary run exposed an argument-forwarding regression
in the new legacy PUT wrapper: the cancellation guard reads the URL run ID from
keyword arguments, while the wrapper passed it positionally. Forwarding
`run_id=run_id` restores the existing revoked-cleanup receipt path. An actual
DRF legacy `article_generation` cancellation test failed with 404 before the fix
and passes with 200 afterward, retaining the erasure receipt and
`repository_authority=false`. A separate tenant-mismatch test still returns 409
before writes. Neither test uses a local database.

After private-source access was configured, CI reached the standalone editorial
lane and exposed two stale AST fixture assumptions: snapshot tests invoked the
old entry point, and revision tests lacked a module/package context and the
organization's `pk` for the taxonomy import. The fixtures now execute the real
public PUT and legacy wrapper with the real pure routing predicate, while the
database receiver remains isolated. Revision fixtures preserve the legacy 503
code when taxonomy is off. The unchanged 60-test snapshot/revision selection
failed with 23 errors before these fixture updates; the complete 168-test
editorial lane passes afterward with database, network and dotenv access blocked.

[The checked-in fixture](../contracts/setup-delivery-v1.example.json) was
generated by the actual Content Factory ArtifactStore writer and durable journal
with network sends paused. It is consumed through the real callback/PUT/pull
chain: the original failure survives all three once. Dropping both pushes and
pulling the committed snapshot recovers that failure without effects.

For fail-before evidence, the unchanged service view from backend base
`c7ab8369` was loaded read-only with the same persistence fixtures. Three new
endpoint regressions failed there (eight assertions/subcases): valid versioned
delivery, malformed typed diagnostics, and late terminal observation. The
corresponding current tests pass. CI assignment validation reports zero new
omissions and includes the new database-free module.

No database was created or migrated, and no production flags, customer writes,
deployments or merges were performed. The in-memory persistence seams do not
prove PostgreSQL transaction isolation, real multi-process leases or crash
durability. Those need separately approved database/volume fault tests. The
producer's shared persistent Docker volume survives process/container restarts;
droplet or disk loss remains outside this boundary. E4's qualifying zoo and
explicit owner release approval remain required before any customer canary or
production policy/apply flag.
