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
| B2 | Implemented in backend #1014 and Chat #465; draft stack, rollout off |
| B3, B4, B6, B7 | Implemented in backend #1016–#1018, CF #985 and Chat #468; draft stack, rollout off |
| C1 through C5 | Implemented in backend #1019–#1023, CF #986, Chat #470–#472; final CI green, rollout off |
| D1 | Backend #1024, Chat #473 and CF #988 final-head CI green; rollout off |
| D2 | #1028 has five passing PostgreSQL provider-chain cases and 155 focused local tests; CF #990 final CI green. Restored legacy CI suites are being repaired; stale refresh/article escalation pending |
| D3 | Draft backend #1029, 146 focused tests pass; rebase and final CI follow the D2 repair |
| E1 through E5 and retained v2 channel/CMS/hosted work | Pending; fixture/provider owner inputs may be needed |
| F1 through F3 | Pending |
| G2 | CF #991 persists the Docker group ID in the deployment environment; 34 focused tests and final CI green. Host application waits for an owner-authorized release |
| G3 | Backend #1031 redacts signed query tickets; 16 focused tests pass. Polling/auth/backoff work remains |
| G1, G4 through G6 | Pending; owner operational steps remain separate |

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


B7 backend PR1018 is attached, head edabee45a765832bee892b358c6c995269477f16,
base feat/goal-wakeups. Chat companion PR468 is attached, head
e7bd2905322f8c900b68706c03d4271a86d784a5, base B2 status branch. Full Chat justci
and required signed-commit/push hooks passed including desktop5606/mobile2731.
Generated B7 CI output preserved in ~/.codex/artifacts/articles-b7-workspace-ci-output.
Backend B6 #1017 and Chat B2 #465 exact current remote checks are all green.

C1/C2 backend is implemented on codex/articles-c1-c2-consent-page with a design note.
One sealed owner answer set per goal; strict price/types/routes; repeat resumes reuse,
explicit changes reserve a successor, source-only B6 edges rebind the consent scope
without altering answers or accepted time. Plan grants include every future repo
write up front and in-repo CI until C3 hosted verification becomes available. A
separate rich v2 permission cache avoids reading legacy missing fields; permission
webhooks clear both. Before scanning, the planner checks all selected grants. C2
prefers a listing with detail children and a confidence margin; /articles is the
missing-listing default; only explicit pick policy asks about true ambiguity.

All19 DB-free CI commands, model drift, credential/639-emitter/assignment checks
passed. Focused70 neighbors and56 consent/planner/source/first-goal cases passed;
final46 consent/permissions/wakeup cases passed after cache change. No migrations.
Chat C1 sheet is under development on codex/articles-c1-setup-sheet based PR468;
parser/types/13 relevant units passed,15 browser cases running. It posts only the
server-priced choice fields and automatically closes/refreshes after success.
Continue C3 exact hosted build proof and consent approval, C4 deployment signals,
C5 durable deduplicated notifications, then D/E/F/G without approval pauses.


C1/C2 backend PR1019 attached, head663f0c2a273fc56d51b0861e7ea74783bfb3b9b2,
base B7. Chat C1 PR470 attached, heada29dd41ca93fd2992ea87a9fd4fe21f0dfa87d6f,
base B7 PR468. Chat fulljustci passed on rerun with RUST_TEST_THREADS=2 after an
unrelated TokenMaxer process-timeout; exact failed test passed alone. Final desktop
5609, Tauri2179 and mobile2731 tests passed, as did signed commit/push hooks,13
focused units and15 browser cases. Two sheet captures distinct; auto sheet inspected.
The form action selected-company bootstrap bug is fixed and covered by startup B
while A is cached. B7 backend #1018 remote checks all green, deploy skipped.

C3 is active, NOT complete. Both backend and CF worktrees are on
codex/articles-c3-hosted-verification. Static receipt/approval code is implemented;
backend5 hosted receipt tests plus90 neighboring chain/merge/permissions tests and
all19 DB-free commands passed. Model/credential checks still need final repeat after
this C3 change. CF50 focused and213 broader neighbors passed;1108-emitter audit
passed. Original request verification_mode is strict/immutable, hosted mode omits
customer workflow reservations, and callbacks mint only a saved nonce-bound exact
builder receipt. Backend accepts skipped build only for this receipt and current
scan source; auto consent repairs missing evidence, review consent asks once.
Default hosted proof stays behind ARTICLES_HOSTED_ATTESTATION_ENABLED=false until
post-merge actual native attestation is implemented. Continue that work using the
reusable CF isolated/native verification and backend full NativeEvidence validators;
keep every output Boolean and original source/operation fence. Do not mark C3 done
or enable flags. All remaining C4-C5/D/E/F/G agent work still required.

C3 post-merge implementation now uses a durable CF hosted-native job and a distinct
backend authenticated_hosted proof origin. Controller config stays outside customer
source; adoption uses its actual installed detail and revised artifact marker.
Pending polls do not complete steps, run IDs are checked, cancellation wins, and
usage history stays bounded without dropping old charges. Backend independently
fetches completed native proof and applies full source/environment/renderer gates.
Chat presents organisation-required review up front and accepts the new doctor proof
kind. Three distinct setup-sheet captures were inspected (organisation review).

All19 backend DB-free CI commands passed; model drift,653-emitter audit,349 full
module assignments/0 new omissions and tracked credential scan passed. Focused12
native/receipt cases plus55 neighboring consent/goal/permissions tests passed.
CF140 focused native/adoption/cleanup/broker/receipt cases passed;9 opt-in real-Docker
checks skipped. Real committed local static renderer fixture passed the native gates
with a test-only uid override. Chat4 plan units and16 browser cases passed. CF full
suite and Chat justci are still running; record outcomes before claiming validation
complete. Rollout flags are false. C4-C5/D/E/F/G work remains.

C3 backend draft PR1020 and Chat draft PR471 are attached. Chat fulljustci and
signed hooks passed (desktop5610/Tauri2179/mobile2731), head75f6a67cd8854c9a61b97b3f8a105cc13371cdc5.
CF's first full run had8560 passes and12 failures: the new task lacked the standard
connection-bound wrapper, now added;11 Node fixture failures came from Hermit's
wrapper overriding fake npm. Underlying Node on test PATH fixes the hermetic test
setup. All214 failed-case/native/task neighbors passed; a full rerun is active.

C4 implementation on feat/deploy-detection identifies scoped config/header and
Actions signals. Only a selected default-branch push without path filters proves
an automatic workflow hint; provider config alone stays unknown. Reads use a fresh
repository-read token, fixed source ref and bounded workflow inventory, then revoke
it. Same-source cached hints avoid repeat calls. Public headers retain only provider
signal labels. Original authority is checked after remote reads before saving.
Manual workflows get one stable provider action immediately; unknown automation
uses A10's propagation window and quiet observer. A real prepare chain resumed
from the manual action when all live markers appeared, without another owner click.

Required owner waits now open/close a replay-safe interval in the goal ledger and
pause only the wall budget; compute, USD, provider waits and attempts still count.
No schema migration.18 final focused tests and55 broader cases passed; all19 DB-free
CI commands passed. Model drift,654-emitter audit,350 full assigned modules/0 new
omissions, tracked credential scan and dependency input signature passed. The new
YAML dependency is pinned6.0.3 and the shared hashed lock preserves existing pins.
ARTICLES_DEPLOYMENT_DETECTION_ENABLED=false. No merge, deploy or live service action.

C4 backend draft PR1021 is attached, head1f970eac601ed9b10575fdd8eb6f4051ee9d8de1.
Final23 deployment/doctor/observability cases passed after applying owner intervals
also to health deadlines. C5 branch codex/articles-c5-goal-notifications is active.
Its design and passive notice projection are saved;4 no-service tests pass for
state silence, replay/action identity, source-only rebind, fixed content, scope,
proof-backed completion and hostile frontend configuration. Delivery/outbox hooks,
Nostr DM transport, per-channel replay and email transport remain UNIMPLEMENTED.
Do not claim C5 complete or enable a notification flag. Native transport must use
existing Nostr events; bridge private-delivery requires real Slack identity and is
not an appropriate substitute. Email retries must respect Resend's24-hour window;
Customer.io send-email deduplication was not established from its reviewed docs.
C5 notice IDs must gain a durable state-entry epoch in the outbox integration so
working-to-needs-you transitions can notify again while repeated polls stay silent.

C3 final checkpoint: CF draft PR986 is attached, head5bf09bf32897864fc571fdae1a48aa6955c3cbc7.
Corrected fullsuite8572 tests/4 subtests passed,10 opt-in skips;1110 exact emissions
passed, canonical bytes unchanged. Backend #1020 checks/migration-tests/postgres-search
all passed remotely; deploy skipped. C4 #1021 remote checks are still pending at this
checkpoint. Chat #471 attached; local full gate and hooks passed. C3 actual merged
native proof is implemented across repos; rollout remains off. C5 implementation
must continue from its passive notice projection through outbox/native DM/email,
then D/E/F/G. No approval pause or production action is required for that code work.

C5 implementation now includes the operation-backed outbox, atomic goal state-entry
reservation, current-owner/company/device/goal/lease fences and scheduler recovery
for expired running notification rows. Native Chat uses saved signed Nostr message
IDs with fresh NIP-98 auth; DM-open retry reuses the existing participant conversation.
Per-channel checkpoints keep accepted Chat/email sends independent. Resend payloads
and keys stay frozen; unknown email outcomes stop retrying at23h. No provider body
or credential is exposed. A sealed first_article_live_receipt supplies D1's live URL.

Final C5 backend validation:20 focused notice/delivery tests,35 routing/wake/observer
cases,98 broader cases and all19 DB-free CI commands passed. Model drift,654 exact
emissions,352 full module assignments/0 new omissions and tracked credential scan
passed. Actual signing plus fake HTTP covers crash-after-acceptance and timeout
replay; no real message was sent. Notification flag defaults false; no migrations.
Chat canonical goal URL parsing and main-router navigation passed2 units and18
browser cases, including a private DM and both left-click/context menu routes while
another company was cached. Fulljustci passed (counts below when committed).

C3 CF remote CI had one fixture error: enclosing GITHUB_REPOSITORY did not equal
its synthetic repo. Test identity is now explicit;27 native/task cases passed with
hostile enclosing Actions identity. Fix e0e9726e is pushed to CF PR986; remote rerun
pending. Production source identity validation is unchanged. C4 PR1021 remote
checks/migration-tests/postgres-search are green; deployment skipped.


D1 billing/refund foundation checkpoint (14 October): feat/first-article-goal.
21 new focused billing/refund tests and all 19 database-free workflow commands
pass. Original quote/payer/ledger survive retries and source-only rebinding;
HTTP cannot grant held-payment authority. Credits have a durable operation outbox
independent of feature flags, cancellation and connection generations. Revocation
preserves payment proof and never waits for credit execution. Erasure retains
only financial proof until settlement. No model/migration change; credential,
emitter (661) and assignment (353 full/0 new omissions) checks pass.
D1 is not complete: continue topic/profile selection, generation, review/approval,
merge/live proof and real-chain coverage. Cover changed owner choices while an
old credit is pending before opening a D1 PR. All rollout flags remain false.

C5 Chat companion is draft PR472 at ab05eff4514ed03e80fdbd89f6176c50478b6c30.
Backend PR1023 gained CI trigger commit293181f0 because its C4 base is a feat
branch; checks are now running. CF986 e0e9726e checks are green; deployment skipped.


D1 controller checkpoint, still incomplete: first-article action/evidence readers,
topic ranking against approved profiles/offers, original held-price dispatch,
canonical approval/publish handoff, goal-bound merge opt-in, A11 publish policy,
read-only manual-merge observation, scoped live-body evidence and C5 receipt.
Parent identity is recorded before workflow reservation and protected against
worker introduction/replacement. Cancellation can fence lost queue responses.
Review/merge owner waits have quiet observer schedules. Source/publish usage is
included; remaining goal budget is passed to generation. Changed choices wait for
an atomic old-credit/new-hold allocation, preserving consent withdrawal.
50 focused tests and 136 neighboring cases pass; all 19 database-free workflow
commands pass, model drift is empty, credential/emitter(677)/assignment(355 full)
checks pass. Full canonical Ready-to-live chain is STILL REQUIRED: use the existing
PostgreSQL CI test lane with OneClickBillingFixture/TransactionTestCase, no new
migration or local database run. Keep D1 in progress, flags off, then finish D2/D3,
E/F/G. Chat branch codex/articles-d1-first-article-goal: 10 status tests and20 browser
cases pass; full just ci running at /tmp/articles-d1-chat-just-ci.log.


D1 real-record CI fixture added: canonical generation with the actual ledger,
workflow reservation, dispatch outbox and lost-reference recovery; plus atomic
successor re-hold/stale-lease settlement. Registered in the existing PostgreSQL
lane; syntax checked only, no local DB/migration run. Draft PR will obtain CI
feedback. Extend this fixture through canonical approve/merge/source/live checks
before treating D1 as complete. C5 backend1023 and Chat472 are fully green.


D2 CI repair, 15 October: b01f6f7e confirms all article-service and dispatch
idempotency cases now pass; restored suite is 61 failures/12 errors across 1,683
cases, plus two legacy callback idempotency failures in the SQLite lane. Fixed
two real defects: sparse snapshots now retain the original connection tuple
(and reject replacement), and identical guarded callbacks avoid a redundant
run save. Fixed the report query aggregate alias that shadowed raw impressions,
with actual ORM annotation resolution and weighted-position regressions.
145 focused database-free cases pass. Synthetic live-target fixtures now publish
the actual certified contract; merge labels remain historical, not readiness.
Boost verified-company fixture now records active ABR status. Database-backed
cases are syntax checked locally and remain assigned to the existing CI lanes.
No local database, migration or production operation was run.


Restored Autofill fixture repair: twenty downstream generation/setup/scan/status
cases now explicitly create the owned website consent they assume; generation
cases additionally create current target/live records. Only GitHub provider I/O
is faked. Initial onboarding and cross-company denial cases do not receive an
implicit website. Status fixtures retain their original saved scope. Reuse the
owned Acme organization in the sibling-company test. The whole Autofill module
is selected in the existing SQLite CI lane because it injects SQLite lock
failures; no test was skipped or removed. All 490 modules remain fully assigned.
These database-backed cases are syntax checked locally and await remote CI.


Additional restored-suite fixtures: real stamp verification is now mocked only
at the ledger I/O boundary in the database-free reuse decision test; an invented
stamp still receives 409, and all five decision cases pass. The archive test
uses an actual HealthHack administrator for its second leaderboard client.
Compact AI evidence asserts bounded unavailable prompt summaries instead of
expecting their removal; raw answer excerpts stay absent without measured
evidence. Luma context tests now assert the existing exact calendar-month
window, excluding both prior- and following-month events. Database cases are
syntax checked only. E1 backend remote CI verified pinned producer bytes using
the existing secret; broader final-head checks remain in progress.


Restored GitHub operability fixtures now distinguish a usable App credential
from generation authority. Ready positive cases retain App-only credentials and
add actual current owner/site/native/live records with provider transport fakes.
The formerly permissive unverified-surface case now asserts 409 and no worker
request or points change until integration is verified. A missing-connection
case retains its denial; setup capability tests use actual founder context.
Provider HEAD observations return a fixed source independently of whether the
local connection has native proof. No credential or admission guard is mocked.
These database cases remain syntax checked only pending the existing CI lanes.


Current-site fixtures now mirror binding by co-stamping the selected App
installation on config and connection. Provider tests configure synthetic App
settings with a fake JWT transport; credential-presence checks remain real.
The reset regression now proves historical articles alone cannot grant readiness,
then starts with an actual verified target and verifies reset removes readiness.
Redetection checks the current integration_verified proof field. Syntax checked
only; existing disposable CI is the database validation authority.


The report aggregate fix exposed one synthetic row still using the old internal
query alias in the health-evidence unit suite. Updated that row only; the public
report fields are unchanged. Thirty focused report/callback/payment cases pass.
Superseded CI runs are being cancelled to avoid duplicate runner work; latest
heads retain the full assigned checks. Full final-head CI remains required.


CI 71b2465e: five first-article chain cases pass; restored PostgreSQL selection
is 1,586 cases with 11 failures/7 errors. Autofill's 97 cases moved to the
existing SQLite job (no omissions); its result is still pending. Fixed the
remaining legacy draft audience defect: resolve_update always returns a row,
so the first save incorrectly inherited its default-private marker. New rows
now use the original scoped run's saved audience after real binding/owner
checks; existing draft choices remain unchanged. Thirteen database-free
disclosure/curation/date cases pass, including no expansion from later company
defaults and private fallback without a valid organization binding.
Fixed two follow-up test defects: removed dispatch assertions after asserting
no dispatch, and replaced an invalid ABN-status fixture argument with actual
revision/approval/publication records for the monthly benefit. Scheduled-confirm
refund now has an owned verified site and original scope, and asserts the
downstream POST was reached. Database fixtures are syntax checked only.


Restored integration fixtures now fake the actual Xero catalogue read seams
and return provider-shaped account records, retaining real filtering/scope and
line-membership checks. A newer incomplete capture must retain its own identity
and cannot validate a line from an older complete capture. GitHub auth-error
classification and Gmail callback/start fixtures now have actual prior company/
website ownership. Email-generation tests use updateId/run identity instead of
the removed reusedExistingRun marker. Stale redispatch starts through the real
API so its update/source scope is current. Explicit regeneration creates a new
run while retaining the saved draft; another month or connection cannot silently
supersede active work. These are current revision-aware API contracts, not
production compatibility changes. All DB cases are syntax checked only; remote
disposable CI still must validate the final head.
