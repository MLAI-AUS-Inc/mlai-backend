# Articles B2 goal runner design

14 October 2026. Proposed for owner approval.

The backend will maintain one current Articles goal for each connected website
and advance it from validated milestone proofs. A worker message or a completed
child run alone will never establish readiness. Chat will display the backend's
`websiteStatus`, including the outstanding milestone, one action when needed,
and the next scheduled check.

This extends the approved [Phase 2 status and consistency design](articles-phase2-design-notes-2026-10-12.md#21-one-backend-status-owner-and-consistency-sweep)
with the B2 goal receipt in the owner's 13 October one-shot runbook. Implementation
depends on [B1/B5 backend PR #1013](https://github.com/MLAI-AUS-Inc/mlai-backend/pull/1013).
This design branch starts at `origin/main` commit
`c7ab8369170e98b57107abdbcd499c621a6d8ef0`; it does not duplicate or merge that
pending implementation. Implementation should start from fresh main after the
dependency is merged, or use an explicitly identified stack if the owner chooses.

## Scope and decisions

B2 implements goals through a verified live articles surface, the `ready`
target. Its schema includes `first_article_live`, but the public start action
will reject that target with a bounded availability response until C1 and D1
provide consent, billing and generation/publication handlers. A future goal with
that target cannot become `done` at the `live` milestone. B2 does not claim the
nightly one-shot success target before D1 and the full E4 zoo exist.

Reuse the existing prepare operation, dispatch outbox, scoped authority checks,
journey loader, exact setup approval/merge checks and live deployment verifier.
Use existing JSON fields; add no database migration. B3 supplies strategy moves,
B4 supplies goal budgets and escalation, B6 expands event wake-ups, and B7 adds
the doctor interface. B2 records their state slots and preserves the existing
bounded behavior where those policies are not implemented.

Keep the approved flags `ARTICLES_WEBSITE_STATUS_V1_ENABLED=false` and
`ARTICLES_WEBSITE_CONSISTENCY_ENABLED=false`. Add the proposed
`ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=false` and a server-controlled organization
allowlist so display rollout cannot start new work. Goal execution additionally
requires B1 policy enrollment for the same saved organization. No client or
worker field can enroll a website. Turning a display flag off must not abandon
an already enrolled goal.

## Goal identity and storage

Store `receipt.goal` on a `WebsiteConnectionOperation` with `action=prepare`.
It contains `schema_version`, `goal_id`, `revision`, `target`, `started_at`,
`scope`, `milestones`, `attempts_by_class`, `budget_used`, `strategy_pin`,
`fingerprints_seen`, and `consent`. Keep legacy receipt fields for one release.

`scope` binds the saved company, organization, connection ID/generation,
repository ID, selected branch/app root, configuration revision and deletion
epoch. Each child proof also carries its original operation/attempt, source
SHA, selected target and contract digest when relevant. Backend records, rather
than request aliases, supply this authority. `consent` records only explicit
existing owner decisions; unimplemented C1 consent and unknown cost/usage are
null. A goal target grants no approval, merge, billing or publication consent.

Repeated Set up articles requests under the connection lock return the same
current goal. Retries of unfinished work preserve the goal identity and use the
existing explicit fresh-attempt or lost-response replay distinction. Reconnect,
disconnect and changed consent invalidate the old scope. They cannot migrate
proofs or approval into a replacement connection.

Completed, cancelled, denied and stopped goal executions remain terminal.
An accepted source change requiring new readiness creates one deterministic
successor prepare operation under the current connection lock, linked to the old
goal, instead of reopening completed history. Duplicate source observations
reuse that successor. The selector chooses that current goal deterministically;
late children of the predecessor cannot override it. Source movement during an
active goal invalidates affected proofs and reserves a new scoped attempt.
Owner approval never transfers to a changed proposal or head.

## Milestone proofs

Each milestone has `name`, `state`, `required`, and `proof`. A proof contains
`kind`, `sha`, `run_id`, `evidence_digest`, `at` and its relevant scope. Fields
that do not apply are null. States distinguish pending, passed, invalidated and
not required. Retain bounded superseded proof references for diagnosis; do not
retain provider bodies, repository content or credentials.

| Milestone | Evidence required to pass | Existing action or reader |
| --- | --- | --- |
| Access | Current authenticated installation/repository access and the grants required by the next action | `verify-access`, authority and permission readers |
| Understood | Complete repository scan at the selected source, with its fingerprint, validated discovery and target inputs | `scan`, saved scan snapshots and `journey_for_context` |
| Integrated | Scoped adoption or owned integration candidate, with branch/head and validated surface contract | Existing setup dispatch and accepted target/mutation records |
| Previewed | Exact candidate head, valid build/browser/style gates and current preview proof | Existing setup preview and target verification readers |
| Approved | Backend owner approval or already supported policy approval for that exact proposal and head | Existing setup approval receipt and guards |
| Merged | Authenticated GitHub merge observation matching the saved expected head, with recorded merge SHA and lineage | Existing setup merge and source reconciliation |
| Live | Current selected source/target/contract, valid build proof and the verified public route/marker receipt | `current_ci_attestation` and `verify_live_deployment` |
| First draft | Exact scoped article run and passing article gates | Reserved for D1; not required for `ready` |
| First article live | Approved exact article revision, matching merge and live article receipt | Reserved for D1; not required for `ready` |

The collector references the backend validators' accepted evidence; it cannot
promote raw worker assertions. Already integrated sites can satisfy milestones
through their existing validated provenance. A missing historical approval or
merge is not fabricated: record a separately validated adoption/no-change proof
only when existing policy establishes that no new mutation requires those gates.
Otherwise keep the milestone outstanding and use the existing guarded action.

Source, target, contract, permission and consent changes invalidate only their
dependent proofs. The expected merge transition can advance a candidate to its
recorded merge SHA through verified lineage; an unrelated push cannot. Expired
verification cannot be made fresh by copying its timestamp. A `completed` child,
an open PR, a preview URL or `canPublishArticle=true` without the required proof
set cannot complete the goal.

## Advancing and recovering work

Extend [advance_prepare](../content_factory/website_prepare.py), preserving the
claim and compare-and-set persistence. Each pass:

1. Claims the existing due operation and reads its saved goal revision/scope.
2. Runs the real `_prepare_context` and `journey_for_context`, then collects and
   validates their evidence against the current scope.
3. Computes the first unmet required milestone and selects its existing action.
4. Reuses a bound active child or reserves a durable dispatch intent before I/O.
5. Applies the B1 outcome policy and persists proofs, state and the next due time
   together under the original claim, goal revision and authority fence.

Allow at most the existing access probe plus one work dispatch per pass. Derive
stable dispatch keys from goal, attempt, milestone and relevant proof identity;
a repeated scheduler pass, callback or pull cannot create another child. A crash
after dispatch recovers the reserved child through the existing dispatch/outbox
and B5 receipt mechanisms. B5 reception and reconciliation deliver observations;
the goal collector independently decides which milestone those observations
prove.

A failed compare-and-set reports lost ownership and reloads persisted state. It
must not return an uncommitted goal as successful progress. Every action repeats
the current owner/connection/operation fences at its effect boundary. Network
calls remain outside database locks, and disconnect/cancellation wins any race.

Transient outcomes retain the same step and recorded backoff. Waiting is an
observation with a real future check, not a failure. Repairable outcomes use the
existing bounded owned-file repair; strategy outcomes retain the current pin
until B3 can move it. Existing exhausted repair/strategy limits remain explicit
`stopped` results while those later dependencies are unavailable. Only actual
owner review, grants, required merge or manual deploy uses `needs_you`.
Unsupported/platform failures cannot invent an owner permission action.

Count a causal occurrence once across callback, PUT and reconciler observations.
Do not count scheduler passes, duplicate transport events or waiting as failed
attempts. Preserve the B1 class, fingerprint and occurrence identity. B2 must not
pretend to implement B4's compute/cost limits or change a causal class because
delivery was missed.

Reuse B1's registered causal codes. A new causal code requires a reviewed
canonical CF registry addition and pinned parity update before backend use.
Milestone consistency findings remain separate noncausal diagnostics.

## Website status and Chat

Add the approved `website_status.py` reader through both bootstrap and website
connection responses. It produces `version`, `state`, `step`, `title`, `message`,
`nextAttemptAt`, `action`, `evidence`, `operationId`, `updatedAt`, current goal
identity/target/scope, `milestones`, and `expectedTime`.

| Public state | Required persisted facts |
| --- | --- |
| `working` | Unfinished goal with a persisted next check or lease recovery time |
| `needs_you` | Exactly one currently valid owner action, with its scoped link |
| `done` | All required proofs for the goal target passed under current scope |
| `stopped` | Cancelled/denied/exhausted/unsupported work, or missing executable progress, with one specific reason and safe details action |

Never invent a schedule in a GET or infer it from a worker's status. Missing
recovery scheduling is a consistency finding, not an endless `working` card.
`expectedTime` is null until a calibrated estimate exists; `nextAttemptAt` means
the next check, never a promised completion time. Public evidence is bounded and
allowlisted. Old runs, stale generations and a different selected company cannot
change the current card or supply its action.

Chat adds a strict `websiteStatus` normalizer and updates the existing
`WebsitePrepareCard.tsx` and `WebsiteJourneyPanel.tsx` under
`desktop/src/features/my-startup/ui/`. The bootstrap normalizer and shared types
carry the server contract. When enabled, the readiness card renders only that
contract and polling follows its actual schedule/action state. Existing journey
capabilities remain the backend's authorization inputs; this display change
does not replace action guards. A malformed or unsupported enabled contract
shows a bounded refresh/error state rather than combining legacy fields into a
second status. Flag-off or an explicit legacy server response retains the old
display for the compatibility release.

## Consistency and rollout

Add the approved bounded `website_consistency.py` reporting sweep to
`core/management/commands/run_scheduled_discovery.py`. It reuses B5 observations
and identifies terminal-parent/active-child disagreements, unverifiable adopted
setups, missing progress timers and revision batches without accepted revision
proof. Dry-run writes zero rows. A separately approved apply command locks the
saved scope, repeats generation/revision checks and records an idempotent repair
receipt. It can close stale backend child state but cannot publish, merge,
dispatch a replacement, notify, refund or change Roo balances. CF owns its own
terminal artifact repair. B2 implementation approval does not authorize running
this command against production.

Implement backend runner/projection first, then the Chat reader. Add the flags
and trusted enrollment to each consuming configuration inventory. Enable the
receiver/readers before enrolling fixture goals. Keep production enrollment
empty and all new flags off until the qualifying E4 validation and a separate
release approval.

Rollback disables new goal enrollment/execution and consistency work. Preserve
goal/proof receipts and reserved child identities; do not hand an enrolled child
to legacy redispatch. Turning off status rendering restores the compatible
display. Existing child delivery, cancellation and read-only reconciliation
must continue so rollback cannot strand an unacknowledged outcome.

## Acceptance and implementation order

Backend changes belong in `website_prepare.py`, `website_operations.py`, the
existing journey/verifier/dispatch adapters, new `website_goal.py` and
`website_status.py`, and the consistency reader/command. No second lifecycle
engine or parallel source of status is introduced.

Add table tests for every milestone crossed with missing, valid, expired,
wrong-source/target/scope proof and all six B1 outcome classes. Test both target
definitions, including refusal of unsupported first-article starts and refusal
to finish that target at `live`. Assert action/schedule/terminal precedence and
bounded public evidence for every public state.

Extend the real-chain fixture pattern in
[tests_prepare_journey_unit.py](../content_factory/tests_prepare_journey_unit.py):
execute actual `_prepare_context`, `journey_for_context`, `advance_prepare`,
proof collection, action selection and status projection with only ORM,
provider, cache and transaction boundaries replaced. Include wrong-company,
disconnect-after-fetch, lost compare-and-set, duplicate dispatch, crash after
reservation, dropped delivery followed by B5 pull, source push and matching
merge lineage. Pair isolated tests with this chain; do not stub its orchestrators
or use the optional handler injection as the integration proof.

Consistency tests prove zero dry-run writes, repeat-apply idempotency, terminal
child closure without business effects and rejection of stale generation or
revision. Chat tests cover the four states, one checklist/action, malformed
contracts, no legacy-field mixing, scheduled polling, auth termination and
company/community switches. Playwright verifies the real card with the shared
mock bridge and animation helper.

Run new backend suites with `scripts/test_without_database.py`, add their CI
assignment and run the emitter/contract audit. Record unchanged tests failing
before and passing after. Backend SQL/concurrency tests need their separate
exact migration approval. For Chat run the repository's required lint, types,
unit, E2E and `just ci` gates. Production dry-run, customer canary, merges and
deployment remain owner release steps.

Approval is requested for the B2 extension: `ready` only until C1/D1, an
independent goal execution flag with trusted enrollment, and terminal source
changes creating a successor goal rather than reopening completed history.
