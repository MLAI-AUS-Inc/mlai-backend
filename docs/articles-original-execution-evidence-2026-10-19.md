# Original-goal backend and worker execution observations

The original runtime diagnostic inspected the process answering a later poll.
It could not prove which backend or Content Factory process executed the goal.
The existing private runtime endpoint now includes evidence captured on the
actual execution paths and joined to backend-owned original-run locators.

## Backend controller journal

New enrolled goals with the existing doctor observation enabled record an
admission snapshot inside their acceptance transaction. The existing prepare
claim transaction records the process that obtained each lease. Its outcome is
saved with the normal lease/revision/generation compare-and-set, so a stale
worker cannot publish a completed observation after losing ownership.

An expired running lease is recorded as `superseded` only when the next claim
actually takes over after the saved `next_attempt_at`. This is an authority
fence, not an invented return from a dead process. Unfinished claims, missing
attempts, legacy goals, disabled observation, unknown full source revisions and
the 256-record capacity limit remain explicit coverage gaps.

Each private record contains the original goal, its authority and exact scope
digests, attempt and goal revision, process instance, full runtime revision,
named Boolean flags and the existing per-phase backend eligibility predicates.
Flags are captured at execution time; a later settings change cannot rewrite
that history. No new rollout or enrollment default is enabled.

The bounded journal lives in the existing operation receipt and introduces no
model or migration. Public operation summaries remove it. Optional collection
errors preserve the controller's response and never serialize exception text.

## Exact worker join

`GET /api/content-factory/articles/runtime-observation/` retains its existing
service-key-only authentication and exact original-goal selectors. Its two
PostgreSQL read-only snapshots still reject moved ownership, scope or saved run
evidence during remote reads. It additionally returns `backend_execution` and
`content_factory_executions`; the latter reads the paired CF private endpoint
`/api/internal/articles/execution-observations?run_id=...` for saved owned runs.

The reader requires matching original goal, request scope and goal-lineage
digests and field vocabularies, and validates every task entry/outcome record.
Up to 32 exact request epochs retain authority across legitimate same-run
resumes. Only the reserved operation attempt may increase; source, connection,
operation, payer/request identity and lineage remain stable. Raw epoch tuples
are validated privately and removed from the backend projection, which exposes
only field names, digests and attempt numbers.
Older generation history may remain in the journal; it cannot name a generation
newer than the backend mirror. State versions are bounded within the matching
generation. A `generation_superseded` outcome closes an old entry only after
observing a strictly newer durable generation, while normal return/error
outcomes must remain in their entry's generation.

Reads require explicitly configured HTTPS and the CF service key, verify TLS,
follow no redirects and ignore ambient proxy/netrc credentials. Each response is
bounded to 4 MiB, the collection to 8 MiB, and one ten-second elapsed budget is
checked between streamed chunks. Remaining or unavailable runs stay partial;
remote exception text and unrecognized fields never enter the response.

## What this can prove

`coverage: complete` on either added section describes only the bounded backend
controller journal or the selected admitted CF task-body journals. The endpoint
still does not assert fleet/scheduler completeness, exhaustive owner-action
coverage, zero human interventions, full effective rollout or a qualifying night.
The zoo must consume these actual joined records and independently retain every
remaining coverage gap. The existing current-process diagnostics remain
separate and unbound. Historical records are never backfilled as observed work.

Validation uses the repository's database/network-denying runner. Regressions
exercise the actual goal admission, prepare claim and compare-and-set outcome,
read-only selector, and HTTPS streaming/projection path with only storage,
transaction and HTTP boundaries replaced. Real PostgreSQL and deployed worker
observations remain disposable-CI/live qualification evidence.

## A retained predecessor no longer strands new consent

An owner can change first-article choices while the cancelled predecessor's
last result is still arriving. The successor waits for its credit. If the late
result proves a useful draft was already delivered, settlement correctly retains
the predecessor's charge. Previously that completed settlement never resolved
the waiting successor, because only a refund could wake its reservation.

The same locked settlement path now resolves both outcomes. A refunded charge
is re-held as before. A positively retained, complete settlement may reserve a
separate charge only for the latest active successor with independently sealed
owner consent, its accepted quote, matching payer and exact predecessor wait
binding. The original charge remains untouched. Unknown effects, withdrawn
consent and changed quotes cannot cause a spend. Insufficient balance produces
the existing explicit billing action instead of an indefinite wait. Replays
reuse the successor's own reservation key.

An actual settlement regression fails on the original `advance_refund` and
passes after this correction. A PostgreSQL-only case in the existing CI lane
checks two distinct spends, no refund, duplicate settlement and rejection of a
stale successor claim after the new reservation. No financial operation or
database test is run locally.

Website owner action observations also now hash the bounded intent contents.
Reusing a request key with different choices cannot collapse two interventions;
oversized or nonfinite input remains unmeasured. This improves the existing
partial audit and does not assert exhaustive transport or external coverage.

## Durable owner transport coverage in selected sandboxes

Authenticated DRF mutations and grant-checked MCP write tools now enter a
server-owned observation context before their domain mutation transaction. The
original admission writes the initial setup event with the goal, so a lost HTTP
response cannot erase or recount the initial press. Later accepted or rejected
intents are committed before domain work; a domain rollback does not roll them
back. Replay handling follows each actual operation contract. Different choices
with the same replay identity count separately, while non-idempotent edits count
as separate intents. Payloads are bounded and hashed; article or credential
contents are not retained in the audit.

`ARTICLES_OWNER_AUDIT_REQUIRED_ENABLED` and the explicit
`ARTICLES_OWNER_AUDIT_REQUIRED_ORGANIZATIONS` list are both off/empty by default.
Only enrolled doctor-enabled sandbox organizations can require this policy. In
those scopes, missing durable pre-mutation observation refuses the mutation
before provider effects, and unobserved legacy goals cannot begin execution.
The loaded HTTP dispatch chain, registered facades and MCP write-tool inventory
are checked at admission and projection. An added unobserved route, changed
manifest, request-level atomic transaction, missing initial binding, malformed
metadata or bounded journal overflow keeps internal coverage partial. Production
best-effort observation can never report complete internal source coverage.

The private endpoint adds `owner_transports` with the original goal, target,
scope digest, window, required policy, manifest digest and observed action
count. It also joins the separately reviewed `provider_action_archive` reader
between the two original-goal snapshots. Complete internal transport coverage
is only one input to the zoo's overall action proof: historical observations,
external access, reviews, merges, pushes and hosting actions still need their
own complete, bound evidence window. No complete zero-action night is inferred
from current permission snapshots or from this journal alone.

## Isolated controller faults and original policy observations

The acceptance deployment can use `ARTICLES_SANDBOX_FAULT_MODE=isolated-acceptance`
only with `APP_ENV=sandbox`, an unexpired operator-owned frozen fixture manifest,
a private HTTPS sidecar URL and its separate control token. The default path
reads no manifest and contacts no sidecar. The manifest must match the saved
company, original goal, connection generation, repository identity and fixture
repository prefix. The backend independently resolves the owned child operation;
the sidecar repeats the saved CF request and armed-goal check. A claimed fault
has one nonce and can be completed once. Product requests expose no fault command.

Merge and live-deployment delays wrap the actual setup/article controller
boundaries and record measured elapsed delay before continuing ordinary work.
The disconnect case calls the existing lifecycle method before article dispatch;
the ordinary cancellation, generation fences and refund outbox perform settlement.
The hook never credits points or manufactures cancellation proof. An interrupted
or unobserved effect remains unavailable in the sidecar evidence.

A frozen `goal_budget_profile.active_seconds` can lower the ordinary active cap
only for the exact strict-audited sandbox budget fixture at original admission.
Its saved digest binds the original goal and authority, case and manifest.
Changing source configuration preserves that lower cap; moving authority or
altering the profile invalidates it. The existing cumulative usage evaluator
performs the stop. A single CF `RUN_BUDGET_EXHAUSTED` strategy failure is not
whole-goal terminal budget proof.

`GET /api/content-factory/articles/policy-observation/` uses the same service-only
key and exact original selectors as runtime observation. Its read-only snapshot
can inspect the original cancelled generation after precisely one disconnect,
while current company/config/connection ownership and all other authority fields
still match. It validates real charge/refund ledger identity, payer, exact amount,
refund relation, sealed reservation and complete original settlement/outbox.
Only digests and bounded policy facts leave the backend. A billing status string
alone cannot prove credit. This endpoint does not reconcile or mutate anything.

The same reader exposes genuine source-bound `MISSING_CONFIG` prerequisite
failures, including nested directory-build diagnostics, with registry-auth input
names only. It retains the canonical needs-user failure instead of inventing a
private-package success or a new failure code. A budget policy additionally needs
the original finite controller stop and completed settlement. Historical
owner-transport, worker and provider observations remain independently joined;
missing evidence cannot turn an expected-stop label into a passing result.

`GET /api/content-factory/articles/provider-observation-profile/` freezes the
reduced current provider observer configuration before admission without starting
a dummy goal. It accepts no query parameters and uses the private service key.
No deployment, fixture action or financial operation is performed during local
validation; real SQL and end-to-end acceptance remain disposable CI work.

A lifecycle transaction that cancels a currently claimed paid goal records
`authority_revoked` with the same durable cancellation. This closes that claim
as revoked, never as a returned controller result; the stale controller's normal
outcome still loses its existing CAS. The historical policy projection includes
this backend journal rather than silently omitting unfinished execution.

Ordinary article generation and run controls after a completed first goal are
recognized under the original row lock as outside its closed window. They may
proceed using their ordinary admission/payment path and do not append owner
actions retroactively to the completed goal. Active original-goal observation
failures still refuse effects in the strict sandbox policy.

The whole-goal budget sidecar receipt is completed from the existing refund
outbox only after its transaction commits and a measured active-compute stop,
original reservation and complete settlement match. A separate model reservation
denial is unnecessary on a site whose deterministic path makes no model call.
Optional receipt failure cannot undo credit or trigger another financial attempt.

## Ordinary repeat publication reaches its live ledger during normal polling

After the first goal completes, an ordinary separately admitted article can merge
and deploy while its live ledger still lacks a fresh body observation. Its normal
run-status GET now calls the bounded existing refresher for the exact ordinary
writer and its backend-merged publication child. Current selected company,
connection, source, operation attempts, repository, branch, root and deletion
fences must match. First-goal, legacy, portable, unbound or foreign runs are
skipped before extra queries. Existing refresh cooldowns and a 30-second scoped
throttle apply; provider timeouts retain their established best-effort bounds.

This advances the existing source/body/canonical evidence, copies only the fresh
release observations into the poll response, and preserves presentation fields.
It creates no admission, approval, merge, deployment or charge and borrows no
first-goal reservation or consent. The actual ordinary GET regression fails on
current main without this wiring and passes with the same saved ordinary charge;
the companion test executes the real body/canonical comparison and live-ledger
advancement with storage/DNS/provider boundaries replaced.
