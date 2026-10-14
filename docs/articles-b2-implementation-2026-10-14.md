# B2 per-site goal runner implementation

The owner authorized continuing every implementation phase on 14 October 2026.
Backend PR #1014 is stacked on B1/B5 #1013 (`51ee937b`); no merge, release,
production command, migration or enrollment change has been performed here.

`website_goal.py` defines the fixed milestone ledger, exact authority scope,
bounded proof history and causal failure counters. `website_goal_evidence.py`
reads backend-owned scan, target, CI, setup, approval, merge and deployment
records through their existing validators. `website_goal_runner.py` chooses
existing guarded actions from the first unproved milestone. Prepare claims
retain their existing generation/lease checks and additionally compare goal
revision and configuration revision. Losing the compare-and-set returns no
committed progress. Source changes invalidate proofs and cancel the original
child; terminal goals retain their history and receive a deterministic successor.

`websiteStatus` is shared by bootstrap and connection reads. It has exactly
four states, a fixed checklist, a persisted due time and at most one action.
Expected time remains unknown rather than inventing an estimate. Terminal
operations take precedence over children. Completed disconnects do not require
repository access. Chat renders this contract when present, disables the legacy
poller/checklist/receipt, rejects conflicting scope/version/proof states, and
stops scheduled refresh on lost authority. Legacy responses remain supported.

`website_consistency.py` adds a bounded, local reporting-only scheduler entry.
`check_website_consistency --apply --connection-id <id>` locks and rechecks the
selected connection and operation revision. It repairs missing progress timers
and closes bound backend children of terminal operations while reserving their
durable stop. It records idempotent repair references. Invalid goals, unproved
adoption and applied revision batches with no applied change are report-only;
CF owns artifact repair. Production apply still requires its separate request.

All four rollout switches default off and goal enrollment is empty. `ready`
requires seven validated milestones. `first_article_live` is represented but
cannot be requested until C1/D1 provide consent and publication handlers.
The nightly full-flow success target remains E4 work.

Validation:

- Three unchanged regressions failed against B1/B5 and passed against B2:
  readiness without proofs, lost compare-and-set, and the real dispatch chain.
- The actual context/journey/guard/queue chain uses only ORM, provider and cache
  seams. Native proof readers exercise actual model instances and actual CI
  identity/freshness validation, including wrong marker/source/age/origin cases.
- The existing complete journey lane passed 416 tests before the final additional
  proof/report cases; all new cases were rerun separately afterwards.
- Django system/model drift check passed without constructing or connecting a
  database; no migration changes. Failure emitter, CI assignment and tracked
  credential checks passed. Remote canonical parity is rechecked by PR CI.
- Chat typecheck and 5,602 unit tests passed. Six browser cases passed: all four
  states, malformed foreign scope and stopped refresh on permission rejection.
  Four scoped screenshots were inspected; their hashes must remain distinct.
- Full Chat `just ci` and remote backend PR CI are still in progress; their
  completion must be recorded before calling the phase fully validated.
