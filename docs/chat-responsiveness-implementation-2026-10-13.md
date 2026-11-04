# Chat responsiveness implementation — 13 October 2026

This records source preparation, not a production rollout or latency result.
Work is isolated on `codex/chat-responsiveness` from local `origin/main`
(`e4eb7754` initially), updated against current main during PR preparation.
The older dirty primary checkout was preserved.

| Step | Source status | Production / gated work still outstanding |
| --- | --- | --- |
| B7 | Provisioning/presentation/ready logs; minute/hour latency aggregates; discovery outcome and five-second heartbeat; discovery/open queue/latency status sections | Observe heartbeat after deployment, then separately add discovery to healthcheck lanes and configure human-owned alerts |
| B4 | Scheduled possibly-unread re-probes use background admission; fresh activity, hints, marks and receipts remain foreground | Measure deferred/admitted calls and p95 read observation age under five minutes |
| B2 | Timestamp open preference, three-of-four discovery-turn reservation, FIFO cooldowns and unused admission-turn return | Measure oldest pending open p95 under 30 seconds |
| B3 | Cross-owner first-message preference in import slots; shared workspace/state/job fences; first archive foreground routing; private delivery preference until presentation | Measure first presentation p50 under 20 seconds / p95 under 60 seconds |
| B6 | Additive provisional importing room UUID, fail-closed verified device/current epoch and nonlegacy adapter audience checks; existing inventory contract unchanged | Deployment must precede provisional room use by supported clients |
| B5 | Disabled-by-default one-room unread prefetch after opens/recovery; all verified devices; consent/window/source revalidation; hourly cap and bounded retry cursor | Enable only after B2/B3 measurements; default remains false and cap six/member/hour |
| P0 / B1 | No operational actions performed | Human baseline and quiet-room backoff configuration |
| B8 | No cleanup or production configuration changed | Inbox/minimum-client rollout, coverage, mentions, two stable weeks, completed read-position migration and written approval gates |

The user approved the proposed read-detection and prefetch policy choices (D2/D7)
through the parent chat. No production workflow, SSH command, GitHub variable,
database migration or database-backed local test was run. Source is packaged
as a signed-off commit and draft pull request for review.

New deploy values are present in settings, `.env.example`, workflow environment,
`deploy.sh` normalization and remote installer, the stdin upsert allowlist,
configuration validator and tests:

- `MESSAGE_SYNC_OPEN_PRIORITY_ENABLED=true`
- `MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED=false`
- `MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR=6`

Provisional API shape remains backward compatible:
`202 {state: "importing", mlai_channel_id: null, retry_after_seconds: N,
provisional_channel_id?: "UUID"}`. Only `state: "ready"` is a readiness claim.
Requests without the current importing device epoch or matching completed,
nonlegacy ACTIVE registration receive no provisional ID. Pending registration
cleanup withholds it. The check never mints a legacy registration.

Validation used Python 3.11 in an existing isolated dependency environment. The
no-database harness clears credentials/dotenv, disables sockets and rejects
database connections and Django database-backed test cases.

- 147 database-free regression tests passed across Slack inventory/open/recovery,
  progressive presentation, unread prefetch, telemetry, actual priority dispatch,
  discovery cadence/heartbeat, worker lanes, app authority, capacity, read
  selection and provider throughput.
- 26 deploy/runtime/inventory configuration tests passed, including atomic stdin
  upserts and invalid boolean/cap rejection.
- `scripts/test_without_database.py --check-models`: system checks clean; no model
  changes detected (inspection only).
- `scripts/check_dependency_lock.py`: both dependency inputs match the lock.
- `scripts/check_test_assignment.py`: zero new omissions; new prefetch suite is
  in the workflow's database-free Slack conversation-opening lane.
- Python compilation and `bash -n deploy.sh scripts/upsert_env_value_from_stdin.sh`
  passed. `git diff --check` passed.

PostgreSQL fixtures were added to existing CI-selected modules for open owner
ordering/rotation/marker cleanup, import-slot cross-owner claims, marker expiry,
post-proof fairness, private delivery rotation, source discovery precedence and
new status sections. They were not run locally because constructing the test
schema requires the separate specific migration approval in `AGENTS.md`.

Regression fixtures were introduced before the corresponding source behaviour
for latency/proof scopes, open-marker/returned-turn semantics, archive routing,
unread prefetch and configuration wiring. Existing ready/catalog, source
window, consent, receipt and provider Retry-After regressions were retained.

Final review added failing-first regressions for fresh Slack archival under
bounded versus all-history consent and for the open-priority rollback flag's
cooldown/direct/wrapped admission turn semantics. A newly archived source cannot
use stale directory metadata to pass prefetch eligibility. Disabling open
priority restores those legacy turn penalties as well as ordering/page priority.

Packaging note: this record is explicitly included in the pull request despite
the repository's existing `/docs/**/*.md` ignore rule. The client implementation
is reviewed separately in `MLAI-AUS-Inc/mlai-chat` on `codex/chat-responsiveness`.
