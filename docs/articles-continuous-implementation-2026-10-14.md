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
