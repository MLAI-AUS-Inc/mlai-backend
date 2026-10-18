# Original goal action observations

R3 needs an original-goal action count, rather than a test runner's own HTTP
count or one article attempt's `user_action_count`. Existing per-run counters
cover selected resume/restart paths. They cannot prove that a person did not
review, merge, change source or deploy externally. This change provides a real
writer and public read for the observations that the backend can support now.
It deliberately leaves the exhaustive count unknown.

## Public contract

The selected-company operation endpoint exposes `operation.receipt.goal_audit`
when B7's doctor and the enrolled goal runner are enabled. It includes:

```json
{
  "version": 1,
  "goal_id": "original-prepare-operation-id",
  "basis": "original_goal_audit",
  "complete": false,
  "human_actions": null,
  "observed_owner_actions": 0,
  "coverage_gaps": ["external_github_review", "external_github_merge"],
  "started_at": "2026-10-18T00:00:00+00:00",
  "observed_through": null
}
```

The example abbreviates the gap list. `observed_owner_actions` counts unique
observed HTTP intents; it is not an exhaustive human-action count. It is null
when no valid audit exists. A new goal starts with a recorded empty observation
journal. Legacy enrollment or starting observation after rollback never
retroactively initializes a measured zero. Every returned audit has
`complete:false` and `human_actions:null`, even if an untrusted saved value
claims otherwise. R3's report must reject this as autonomous-success evidence.

The public projection exposes no actor IDs, idempotency keys, request bodies or
internal event hashes. Only fixed fields, fixed gap codes and counters survive.
The raw bounded journal is removed from operation receipts on rollback too.

## Writer and scope

At actual goal creation, the runner seeds an audit in the original prepare
receipt. The existing owner-operation wrapper records genuine Django/DRF HTTP
intents for its scan/setup/configuration/article/feedback/run controls. The
connection endpoints also observe bind, explicit actions and prepare retries.
Internal goal requests use a trusted server attribute and are excluded; an HTTP
body cannot impersonate it. The initial Set up articles request is excluded once.
Existing header/body idempotency keys deduplicate delivery replays. Without a
key, each observed request has its own generated identity.

Run controls resolve the original first-article goal or saved budget goal. Setup
children resolve their saved parent's child IDs. Every write checks the selected
company, run ownership, connection generation and exact current goal scope.
Completed goals stop accepting observations. The journal is bounded at 512
events; overflow is an explicit coverage gap, never a reset or exhaustive zero.

The original owner wrapper locks its operation row and writes its receipt. It
does not change the worker's `updated_at` lease. A prepare's final compare-and-set
preserves newer audit observations while holding that row lock. This prevents a
worker snapshot from overwriting an owner action without forcing a new lease or
altering the goal's proof revision. The existing lease, attempt, generation and
configuration checks still decide whether the worker can persist.

## Defaults and validation

There is no new rollout switch. All new writes, row locks and public fields are
behind `ARTICLES_WEBSITE_DOCTOR_ENABLED` plus the existing goal runner/taxonomy
flags and both organization enrollments. Defaults perform no audit lookup or
write. When enabled, normal goal creation and observed owner mutations gain
journal writes; prepare persistence gains one locked read to preserve them.
Optional observation failures log one fixed event without exception content and
leave completeness false. No migration or new operation type is introduced.

Strict local tests exercise the real goal runner, founder context selection,
owner wrapper, audit writer, claim preservation and public operation reader,
replacing only ORM and transaction boundaries. They cover replay, initial setup,
legacy/forged data, internal dispatch, tenant/generation rejection, privacy,
overflow, rollback defaults and observation failure. Existing goal/status/doctor,
prepare, billing, refund and alert tests run with them. The remote-only SQL cases
exercise actual persistence, authenticated public reads, real prepare CAS and
transaction rollback. They must run only in the authorized disposable CI.

## Remaining proof needed for exhaustive coverage

Inspection of `website_wakeups.signal_for` found no preserved `sender`,
`merged_by`, review author or deployment creator. It accepts completion hints
for pull-request closure and deployment status, and does not ingest review
events. `website_goal_evidence.collect_proofs` validates milestone outcomes,
not an exhaustive actor journal. Those readers cannot certify zero human
actions from a successful merge or live marker.

Before `complete` can ever become true, a separate reviewed R3 extension must:

1. Bind authenticated GitHub review, push, merge, installation-permission and
   workflow/deployment events to the original goal and owned source/PR/revisions.
2. Distinguish a recorded automatic merge/deploy intent from owner operations;
   an app/bot actor alone does not prove an operation was automatic.
3. Reconcile provider histories through a recorded end checkpoint so dropped or
   out-of-order webhooks cannot hide owner actions. Unsupported or unavailable
   hosting audit history must remain unknown for that provider.
4. Inventory the remaining owner endpoints and preserve attribution through
   successor goals. Declare a measured coverage window ending at article live.
5. Prove the entire attribution chain with fixture faults and actual provider
   payload shapes, then emit exhaustive counts only for a proved coverage class.

Effective service flags and revisions use the R4 checker as their owning
semantics. This audit does not infer CF state, manufacture a service revision or
turn a backend limit into reported provider spend. It is a truthful R3 producer
component; the full zoo and the four gates remain unproved.

## Review mutation intent observation

The authenticated article-review facade now records supported mutation intents
before billing and Content Factory dispatch. A validated attempt still counts
if billing, revision conflict or service availability later prevents completion.
These are authenticated principal intents; a browser or API caller can be
automated, so they do not prove a human origin. Every public audit still returns
`complete:false`, `human_actions:null` and the full existing coverage-gap list.

The new observer requires the run's exact saved connection, generation,
repository, branch, app root, domain and source. It locks the current connection
and an unambiguous prepare parent: agreeing explicit original-goal identities,
or exactly one saved parent reference to that run. It never chooses a latest
goal. The goal's full company/organization/connection/configuration/deletion
scope and any saved budget-scope digest must match. Unowned, conflicting,
orphaned, ambiguous, stale and inactive/terminal parents are not reassigned.
They emit fixed unavailable diagnostics and retain incomplete coverage. This is
not a terminal coverage checkpoint or a solution for successor attribution.

Text/batch/image/undo/AI-permission updates use the real review protocol's
validated `operationId` and an immutable intent fingerprint. The fingerprint
excludes `expectedRevision`, as the actual retry protocol does. Identical retries
count once; changed edits under the same ID and identical edits with distinct IDs
are separate attempted interventions. Batch aliases share their actual normalized
`applyUpdate` identity. `discardImage` has no operation replay contract, so each
request counts even when a caller repeats an idempotency header. GET and `refresh`
do not count: refresh may be automatic polling or recovery. Those ambiguous paths,
comment CRUD, MCP controls and external actions remain coverage gaps.

For direct mutations, this observer validates the action, replay-identity syntax
and revision shape; the service still owns field/content validation. A later
semantic refusal counts the authenticated attempted intervention conservatively,
without claiming that an edit succeeded or that its origin was human. Batched
updates use the backend's existing full normalization. A valid newest-revision
HTTP edit with no explicit original-goal binding, or a saved run whose source is
older than its still-active parent's source, remains explicitly unavailable.
Neither is reassigned to a newer goal or interpreted as zero human actions.

Only a private digest enters the existing bounded 512-event journal. No review
text, actor, raw operation ID or request key is added to public receipts or logs.
Intent hashing is capped at 1 MiB and rejects nonfinite or malformed data; missing
or oversized identities remain unmeasured. Optional observation errors do not
change the review response. Writes preserve the worker lease and use the same
prepare persistence guard as other observations. Existing default-off enrollment
and doctor gates apply; disabled observation adds no lookup or write.

The isolated review tests exercise actual DRF authentication, founder selection,
ownership, review dispatch, normalized batch persistence and the public original
operation reader. Only ORM/transactions and the Content Factory service boundary
are replaced. Before the hook, three regressions observed zero instead of the
new intent count. A paired local check then feeds these actual public receipts
through CF #1089's telemetry and shared zero-action predicate: observed zero,
one and two intents all remain incomplete and nonqualifying. Synthetic test
records are never installed as production or nightly evidence.
