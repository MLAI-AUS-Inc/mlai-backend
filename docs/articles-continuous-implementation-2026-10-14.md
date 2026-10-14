# Articles continuous implementation progress

14 October 2026. The owner instructed: "Please just keep progressing through
each phase of the implementation plan. dont stop until its finished".
This authorizes continuing design and implementation without per-step design
approval stops. Production/customer mutations, releases, exact migrations and
owner credential/provider/fixture-account decisions remain separately scoped.

Source plan: the 13 October one-shot runbook in the attached files and
`mlai-chat/.claude/worktrees/articles-scaffold-directory-integration-8ff6b3/docs/mlai/ARTICLES_ONE_SHOT_PLAN_2026-10-13.md`.

| Work | Current state |
| --- | --- |
| B1 and initial B5 | Implemented, backend #1013 and CF #982; all final-head CI passed; flags off |
| B2 | Implementation in progress on backend #1014, stacked on #1013; Chat worktree created |
| B3, B4, B6, B7 | Pending |
| C1 through C5 | Pending |
| D1 through D3 | Pending |
| E1 through E5 and retained v2 channel/CMS/hosted work | Pending; fixture/provider owner inputs may be needed |
| F1 through F3 | Pending |
| G1 through G5 | Pending; owner operational steps remain separate |

B2 backend worktree:
`/Users/samdonegan/Documents/Code/mlai-backend/.codex-worktrees/articles-b2-design`,
branch `codex/articles-b2-goal-runner-design`, design commit rebased to `247b54df`
on B1/B5 head `51ee937b`. Runtime changes are currently uncommitted.

B2 Chat worktree:
`/Users/samdonegan/.codex/worktrees/articles-b2-goal-status/mlai-chat`.
Managed worktree is attached to this task. Runtime changes are uncommitted.

Backend additions: `website_goal.py`, `website_goal_evidence.py`,
`website_goal_runner.py`, `website_status.py`, and `tests_website_goal_unit.py`.
Existing prepare/view/bootstrap writers are being integrated. Independent
status/consistency/runner flags and trusted enrollment default off.

Two meaningful runner regressions failed before the change: a bare legacy
`canPublishArticle` flag completed an enrolled goal without proofs, and lost
compare-and-set returned uncommitted progress. Both pass after integration.
The first focused goal/journey/one-click run passed 59 tests. A broader run
found one legacy source-query regression; the extra source lookup was moved
behind enrollment and that lane still needs rerunning. Emitter audit passed
629 sites. No database connection, migration or production call was used.

Chat additions: strict `websiteStatus.ts` normalization/scope/schedule helpers,
`WebsiteGoalCard.tsx`, and `useWebsiteStatusRefresh.ts`; bootstrap types,
normalizer and `WebsiteJourneyPanel.tsx` are being wired. Typecheck passed;
targeted Biome checks passed after fixes. Unit/E2E/full CI remain pending.

Next B2 work: finish status/action compatibility and hide duplicate legacy
checklists; implement bounded consistency report/apply; add actual context,
journey, proof-reader and dispatcher chain tests with ORM/network/cache seams
only; add status/scope/cancellation/source/replay tests; add CI assignments;
finish Chat action handling and scheduled refresh tests/E2E; run required
checks; update draft PR evidence and create/attach Chat PR. Then continue B3
and subsequent agent steps without ending for design approval.


## Checkpoint after B2 and during B3/B4

B2 backend PR #1014 is implemented, still stacked on B1/B5 #1013. Its head was
`a60277d5` before a cleanup-fixture follow-up. A `codex/articles-*` PR-base CI
trigger now runs checks for this stack; deploy remains push/manual main only.
Remote checks found the old setup-example unit fixtures omitted the new reserved
row lookup. The DAO seam is being added without replacing the real orchestrators.
CI job #115681542313 on run #38511719645 contained that failure. Native proof,
status and goal tests pass (21 tests after malformed-container coverage), as do
the previously complete 416 journey tests. No local database was constructed.

B2 Chat PR #465 is open and attached. Latest pushed head before the manual-refresh
follow-up was `44daf5898`; the follow-up is `4763624ca`. Full `just ci` passed on
runtime B2, including 2,179 Tauri Rust and 2,731 mobile tests. 5,602 desktop unit
tests, typecheck and six native browser cases passed. The recovery control now
invalidates the goal/bootstrap read instead of calling the disabled legacy poller.
Its typecheck and all push hooks passed. Remote final-head CI must be checked.

A full build first filled disk. Only this worktree's generated targets were
removed. The completed Phase A Chat managed worktree was archived after preserving
its test artifacts at `/Users/samdonegan/.codex/artifacts/articles-phase-a-chat-evidence`.
Its recoverable Git snapshot remains attached. About 13 GB was freed. Chat checks
use CARGO_TARGET_DIR at its worktree `/target`, CARGO_BUILD_JOBS=2,
CARGO_INCREMENTAL=0 and dev/test debug=0. `just ci` produced untracked screenshot
`output/`; preserve or remove only those generated files before later commits.

B3/B4 backend edits are uncommitted in the B2 implementation worktree on new branch
`codex/articles-b3-b4-strategy-budgets`, currently based on B2 `52b0505e`. An extra
B2 CI worktree at `.codex-worktrees/articles-b2-ci` owns the B2 branch. Reconcile
its newer CI/container/fixture commits into B3 after checkpointing the current work.
New backend modules: `website_strategy.py`, `website_goal_budget.py`,
`tests_website_strategy_budget_unit.py`; design `articles-b3-b4-design-2026-10-14.md`.
Runner/prepare/setup view/settings/env/status/CI assignment edits are being wired.
Eight policy tests and 28 combined B3/B4/B2 tests passed before the latest edits.
Strategies are monotonic and source-bound; fresh repairable outcomes retry on the
same rung, repeated transient fingerprints escalate, duplicate observations do
not advance. New rungs require a fresh matching candidate before old native proof
can complete them. Budgets default off, 48h/7200s/25USD/20 transient/6 repairable.
Unknown metrics never become zero; missing terminal-child metrics stop further
model work. Hosted pin currently waits for channel selection (E5/C1/B7 pending).
Chat does not yet support `choose_article_channel`; wire this before completing B3.

CF B3/B4 edits are uncommitted at `/Users/samdonegan/Documents/Code/cf-articles-phase-b`
on branch `codex/articles-b3-b4-strategy-budgets`, based on B1/B5 `a1280ef4`.
New `backend/harness/goal_usage.py` and `backend/tests/test_goal_usage.py` meter
admitted goal-scoped execution through the existing durable model reservations.
Original goal budget and strategy metadata are immutable across resume. Status
writers emit cumulative numeric usage; durable outbox retains it. Active time is
conservative task elapsed/provider time; build/preview counters are step attempts.
The original source pin/revision/binding is recorded in exhausted-rung evidence.
The new usage flag defaults off. Latest focused CF run passed 122 tests. A missing
B5 taxonomy-emission flag in the new delivery fixture was fixed; that lane passed
62. Goal metadata/schema additions are not yet fully regression-tested.

CF canonical and Docker-packaged `setup-delivery-v1.schema.json` now include optional
strict `usage` and `strategy_exhausted`. Backend schema/pin has NOT been copied or
updated yet. Commit/push the canonical CF revision first, then copy bytes and update
backend `contracts/failure-codes.lock.json` source revision and schema digest; retain
unchanged registry digests. Reaudit production emitters and remote immutable parity.
CF full pytest, regression corpus and the real broker need rerunning when relevant.
CF Python: `/Users/samdonegan/Documents/Code/content-factory/backend/.venv/bin/python`.
Run in the CF worktree with dotenv disabled and synthetic environment; never load
owner credentials. Existing settings have 3600 scaffold provider seconds/72 calls.

Next: fix backend CI fixture seam; finish typed exhausted-pin acceptance and owned
source fences; complete metering/terminal/cancellation tests; run broader safe lanes;
commit/push CF, pin schema on backend, finish companion channel action UI; create
and attach B3/B4 draft PRs with evidence. Then B6, B7, C, D, E, F and G without a
per-step approval stop. Owner production/releases/migrations/provider credentials
remain outside authorized agent scope. The active goal remains unfinished.


## Latest continuation checkpoint

B2 backend final local head is `5a82085766d9da7297e95d1e06cb281c486fa6cc` on its
separate CI worktree/branch. All 19 database-free checks-job lanes passed locally
after adding the old cleanup fixture's missing DAO seam. Remote #1014 checks are
running again; do not assume final-head green. Chat #465 latest runtime head
`4763624ca` includes manual recovery refresh; hooks passed. Full workspace CI
previously passed on runtime B2. Chat worktree now uses uncommitted branch
`codex/articles-b3-b4-channel-action`: it accepts choose_article_channel and links
to the scoped existing article preferences screen. Typecheck passed; tests/PR for
that companion remain pending. The full-CI-generated root output screenshots were
preserved under `.codex/artifacts/articles-b2-workspace-ci-output` and removed
from the checkout.

CF #985 was created with gh CLI because the GitHub connector returns 404 for the
personal private repo; the CLI is already authorized and works. It is attached.
Head `2527acd610600ef7f6cf4f9be37ceaf2926374fb`, stack base B1/B5 #982. 122 focused
tests and 1,107 emission audit passed. Full pytest session 15121 is still running;
last log `/tmp/articles-b3-b4-cf-full.log` was ~89%. New canonical schema bytes
were copied to the Docker package. Backend schema and lock now pin this exact CF
revision. No registry bytes changed. Remote CF build/test/corpus jobs also run.

Backend #1016 is created/attached, base B2 branch. Head
`294c244e0ffc42d99c4b424082f4701f439cf0d9`. Its earlier signed policy commit was
`f465c16f`; a merge from current B2 was amended with signoff (`d7d74a6`). Nine
policy tests pass; 71 combined B3/B4/B2/delivery tests pass; emitter audit 630,
CI assignments and vendored digests pass. Remote #1016 CI is running. Full broader
B3/B4 lanes, model drift, cost/meter cancellation races and proper exhaustion
validation still need evidence. The runtime is already source-bound; strategy
advancement sets strategy_requires_candidate so old native proof cannot complete
its new rung. Repair retries keep the rung, distinct key; repeated transient
fingerprints escalate too. No parallel agents were spawned.

The backend implementation worktree is now on runbook branch `feat/goal-wakeups`,
stacked on #1016. Only ignored, uncommitted `docs/articles-b6-design-2026-10-14.md`
and this progress update exist for B6 so far. B6 design uses a bounded immediate
executor/cache lock plus a durable goal-wake operation on the existing table.
A signal must not flip a running parent to pending or invalidate its lease. The
ordinary operation scheduler is fallback. Raw webhooks only wake, never prove
merge/approval/CI/live. Match immutable install/repo IDs, current source/branch,
or a bound original setup PR. Recover needs_attention only under explicit goal
policy; deterministic/exhausted/malformed/cancelled/denied/deleted work stays inert.

Important source-transition bug discovered for B6: github push handling in
website_reconciliation.handle_website_github_event increments configuration_version
before source reverify, while wake_goal_for_source currently requires the old goal
scope to equal the new configuration revision and therefore rejects it. Record
previous/current configuration revisions in the trusted source-reverify operation;
rebind active goals only for that exact source-only transition, preserve budgets,
record history, invalidate proof and fence old child. Identity changes stay separate.
Terminal goals retain history and create deterministic successors. The relevant
GitHub dispatcher is content_factory/github_webhook.py; existing installation
permissions path calls advance_prepares_for_connection directly. Lifecycle events
and push handling live in website_reconciliation.py.

Continue B6 implementation, B7, C, D, E, F and G without stopping for design approval.
No production release, customer write, credential change, migration or notification
has been performed. All runtime flags stay off. The active goal remains unfinished.


## B6 completion and B7 start checkpoint

B2 backend #1014 final head 5a820857 passed all remote checks, postgres-search,
migration-tests and validation-route; deploy skipped. CF #985 head 2527acd6 passed
all remote tests/corpus/builds; deploy skipped. Local CF complete suite passed
8550, skipped10, subtests4 in22m51s; actual corpus+real broker passed35. Backend
B3/B4 #1016 final head294c244e: migration and postgres-search passed; checks still
in progress at last query. B3/B4 local all19 database-free checks-job lanes passed.
Chat #465 latest4763624ca: frontend checks passed, critical E2E in progress; other
unchanged paths skipped. Full local justci, types, desktop units and browser6
passed earlier. No runtime flag or enrollment/release was enabled.

B6 is now implemented and pushed/attached as backend #1017, head
b251bddfc78eeced9bae49491a40da2cbac1bbdf, branch feat/goal-wakeups, base B3/B4.
Nine wake/event/source/lock/coalescing/malformed-payload tests and nine budget
neighbours passed; earlier104 combined and all19 DB-free lanes passed. Model drift
passed without a database. New website_wakeups.py uses one durable goal-wake
operation per conn/gen and a bounded 4-worker immediate executor with cache lock.
The operation scheduler has a flag-gated goal-wake handler. Running parent claims
retain their state/lease; durable wake waits. Event hints never prove milestones.
Installation additions/permissions, bound PR close/merge, completed checks and
same-branch/source deployments wake only current scoped work. Deterministic,
terminal and exhausted budgets remain inert. Flag ARTICLES_WEBSITE_EVENT_WAKES_ENABLED
is false. CI stack base trigger includes the runbook branch feat/goal-wakeups.

website_goal_scope.py records Django-owned completed goal-source-scope revision
edges from webhook source changes and authenticated CI source changes. Only a
complete same-identity edge chain ending at verified current source rebinds an
active goal, invalidates proof and preserves counters/start/budget. Terminal goals
are not rebound; source handling retains their successor path. CI after-commit
source wake was added. Historical child references are retained for cumulative
metering after source/repair changes (also fixes B3's new-goal-copy overwrite).
These changes are in B6, so merge all dependencies in order before rollout.

The backend implementation worktree is now on new branch
codex/articles-b7-doctor-metrics, based on B6 b251bddf. B7 has ONLY an ignored
uncommitted design note so far: docs/articles-b7-design-2026-10-14.md. No B7 code.
Plan: bounded server doctor inside websiteStatus, fixed whitelist and redaction;
Chat What we checked/Copy diagnostics uses only that contract; inspect command
adds safe child/CI/deploy/source/goal references; one-click outcomes gain reason/
step/class/time/cost with unknown samples distinct; health excludes owner waits
from overdue and evaluates waiting against goal budgets. Actual existing files:
content_factory/one_click_metrics.py, website_health.py, website_status.py,
management/commands/inspect_website_prepare.py and report_website_connections.py.

Chat worktree still on uncommitted codex/articles-b3-b4-channel-action, two small
changes (normalize choose_article_channel and scoped preferences Link) with typecheck
passed; companion tests/PR pending. CF worktree clean on B3/B4 branch, fulltests
completed. B2 CI worktree remains onB2branch for final CI followups. Active goal
unfinished; proceed B7, then C1-C5, D1-D3, E1-E5/retained v2 CMS+hosted, F1-F3,
G1-G5. No per-step approval stops. No exact new migration is authorized. Production,
customer writes, releases and credentials remain separate owner work. Do not claim
nightly90% or live customer publication without qualifying full-flow evidence.


B7 implementation checkpoint: the fixed website_doctor projection is added behind
ARTICLES_WEBSITE_DOCTOR_ENABLED=false. It validates original proof scope/source,
whitelists identifiers/classes/digests/times, omits credential-shaped references
and never copies arbitrary receipt/result/message/URL fields. Local child status
is scoped by organization, connection, generation, repository and deletion epoch;
CI/deploy reads are bounded and current-source only. The read-only PostgreSQL
inspector uses the same projection. Goal milestone observed_at is recorded when
backend proof is accepted. Metrics report causal class counts, reason/step counts,
start-to-observation percentiles and conservative USD bounds with missing samples
separate. Health excludes needs_user and applies the saved goal/legacy wait deadline.

Validation: all19 database-free CI commands passed; focused33 plus new flag-off
case covered scoped DAO, canary redaction, malformed containers, unknown cost/time,
owner waits and deadlines. Model drift passed (no migrations or database access).
Test assignment346 fully selected/10 partial/123 existing omissions/0 new omissions;
credential scan passed; exact failure inventory632 audited emissions passed offline.
Two new diagnostic observations are recorded in the exact emitter manifest.
Chat14 relevant unit tests and12 browser cases passed, including clipboard success/
failure,4 state screens, missing usage and hosted-channel navigation. Four screenshots
have distinct hashes and the working panel was visually inspected. Full Chat justci
is running before its PR. No runtime flag, release, customer or production write.
