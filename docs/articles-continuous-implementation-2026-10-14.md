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
