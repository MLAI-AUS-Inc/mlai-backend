# Pulse generation reliability

Implemented locally on 31 October 2026 across `mlai-backend`, `valley-backend`
and `mlai-chat`, on `codex/pulse-generation-reliability` in each repository.
This document describes source behavior and synthetic validation, not a deployment.
No model or migration files were added or changed.

## Failure and receipt contract

The reported failure happened after saving a draft: an owner-facing serializer
added `financial_history_evidence: null` to the worker's evidence snapshot.
Valley correctly required equality with the captured snapshot, so that derived
field caused a receipt mismatch even though the saved evidence was intact.

Workers negotiate `X-Pulse-Receipt-Version: 2` on draft results. Version 2 returns
the exact persisted `evidenceSnapshot`, `snapshotHash`, `organizationId`,
`updateId` and reporting month. Derived financial history proof lives in sibling
`reviewContext.financialHistoryEvidence`; it never changes the snapshot hash.
Owner/editor responses retain their existing shape. The new Valley validator also
accepts the known version 1 enrichment from an older backend. Unexpected changed
evidence, conflicting proof, wrong identities, periods or audiences still fail.
Review receives the derived proof separately from receipt comparison.

## Admission and source barrier

Generation persists its request before external collection. It pins the requesting
user, startup, selected sources, provider connection IDs, reporting configuration,
timezone, dates, document IDs, notes and intended audience. Reusing a creation key
with different inputs conflicts rather than changing an existing run.

New runs have `source_preflight_version: 1`. Valley checks every selected provider
before source collection or model work. Checks run with at most four concurrent
probes, bounded timeouts and a relevant read permission check. The response lists
all selected sources in order, including all failures. Document checks require
owned, processed text. Notes/documents can generate without Gmail. An explicit
empty selection never silently selects Gmail; unsupported inputs are reported.

Successful probes are reusable for 120 seconds while paginated resource discovery
continues. Discovery persists cursors, visited pages and exact resource IDs.
Changed connection IDs stop preparation; cached data from another user's account
cannot fill gaps. Failed probes, discovery and preparation warnings remain in
`result.source_preflight`, with `source_preflight_complete: false`.
Downstream endpoints enforce this barrier independently of the worker.

Transient-only preflight failures without a provider retry deadline wait 15, 30
and 60 seconds before requiring user action. HTTP 429 and connector quota errors
instead retain `retry_after_seconds`, `retry_at` and `reason: rate_limited` in
each affected source check. Valley checkpoints the longest provider wait and
resumes preflight after it without consuming the short transient retry budget.
Provider probes, resource discovery and Linear preparation share this failure
classification. Temporary connector failures do not request a reconnect; only
revoked credentials or missing permissions do.
No collection/model work starts during the wait. Revoked permissions and
unsupported sources stop immediately, including when another source is limited. Clients
show each failed source, keep saved progress visible after reload, and offer
`POST /api/v1/community-chat/startups/runs/<id>/retry/` for safe recovery.
The company must be explicitly owned. No selected source is silently omitted.

Xero preflight uses the same bounded HTTP client and shared Redis app/tenant
cooldown as accounting reads and writes. Reconnecting or changing users does not
reset the same Xero tenant's quota; other tenants remain independent. Checks show
the UTC retry time and explain that reconnecting cannot reset the limit. A quota
hit during financial preparation or report refresh keeps the preflight barrier
closed and preserves other source results. A sync quota does not invalidate a
connected credential or advance its successful-sync cursor. Only quota metadata
is shared, never tokens or financial evidence. See Xero's
[rate-limit contract](https://developer.xero.com/documentation/best-practices/api-call-efficiencies/rate-limits/).

The chart-only financial refresh retries a short wait (up to 5 minutes) at most
twice per read and shows the retry time in the founder's reporting timezone. A
longer wait, such as Xero's daily limit, fails the refresh at once and names the
local time to try again, so the panel never sits on "Refreshing" for an hour. A
recovered read clears its retry deadline and attempt count. Every Xero 429, every
response with fewer than 1,000 daily calls left, and every 250th call otherwise
logs `Xero API usage` with the endpoint, remaining minute/day budget, problem and
caller. The daily budget is shared by every environment using the same Xero app
and tenant, so a drop between logged counts that these lines do not explain
points at another caller.

## Collection, execution and review

- Slack, Linear and Analytics discovery is checkpointed rather than silently
  stopping at a fixed catalog-page cap. Repeated cursors are rejected.
- Bank feeds read one transaction page per continuation from the pinned Basiq
  user. Pagination cannot change origin or account. Only dated posted cash facts
  are frozen; bank credits are not treated as revenue, MRR, burn or runway.
- Humanitix and Luma import bounded event catalog pages from the selected account.
  Dated catalog facts get distinct canonical keys and completed deterministic
  evidence receipts. They do not imply attendance or sales.
- Stripe refresh failures remain retryable rather than marking cached figures
  fresh. Financial observations and context reads match selected connection IDs.
- Execution claims increment a durable token under the run-row lock. Collection
  GETs, writes and worker syncs carry that token. Superseded workers cannot commit
  downstream results. Backend-owned request, fence and preflight state cannot be
  replaced by stale worker state. Existing Slack authority lock order is retained.
- Recovery scans the entire paginated backlog, includes queued runs with missing
  heartbeats, and performs a durable transition before enqueueing a failed run.
  Automatic dead-letter recovery is capped at five attempts. Cancellation and
  changed draft revisions cannot be overwritten by recovery.
- Generation checkpoints paid synthesis units and adaptively splits oversized
  context without dropping evidence. Shared production Redis leases bound
  concurrent calls per model (`SHARED_LLM_CONCURRENCY`, default 8). Capacity waits
  release the run to the queue; quota failures stop repeated identical attempts.
- Saved revision review is independent. Failed factual review can create at most
  two immutable repairs against the original snapshot. Lost acknowledgements
  replay the saved revision. Remaining review problems produce a saved draft
  requiring founder review, not a false ready result. Publication still requires
  the founder's explicit exact-revision approval.

## Validation and reproduction

The source-selection test visits all 2,047 nonempty subsets of eleven supported
inputs. The real preflight endpoint is exercised for every singleton, all 55
pairs and all inputs together. External provider boundaries are mocked: these
tests establish selection, isolation and failure handling, not live credentials.

The database suite uses PostgreSQL 17, including eight competing execution
claims, real HTTP/ORM capture and save, immutable repair/replay, tenant boundaries,
local month boundaries, and the actual Valley receipt validator. The notes-only
flow uses a claimed execution through capture, save, lost-ack replay and review.
No test publishes a synthetic update to a live service.

The user approved only the exact 374-file migration inventory in
[`pulse-reliability-runtime-migrations.json`](pulse-reliability-runtime-migrations.json)
for fresh disposable **local** databases; SHA-256
`6571effcd2fba39449b5984517314d0774d15f17e450fe1a8259d518832e1530`.
The inventory digest alone does not authorize a migration or another environment.
The runner disables dotenv and external networking, verifies every migration
hash, and permits only its disposable database on a dedicated local PostgreSQL
port. Production data and credentials are excluded.

```sh
python scripts/test_without_database.py \
  startup_updates.tests_generation_reliability.SelectionMatrixTests \
  startup_updates.tests_generation_reliability.CachedAccountScopeTests \
  startup_updates.tests_month_scope
python scripts/test_without_database.py --check-models
python scripts/check_test_assignment.py

# Use the explicitly approved isolated environment and its dedicated test port.
python scripts/test_pulse_reliability_database.py \
  --approved-inventory-sha256 6571effcd2fba39449b5984517314d0774d15f17e450fe1a8259d518832e1530 \
  --postgres-port 55479 --worker-source /path/to/compatible/valley-backend \
  --include-regressions
```

Valley: `pytest -o addopts='' -q`. Chat: activate Hermit, then run
`pnpm test:ci` in `desktop`; build with `pnpm build:web:e2e` and run Playwright's
`playwright.web.config.ts` against `pulse-composer.spec.ts`,
`pulse-preflight.spec.ts` and `startup-updates.spec.ts`. Mobile validation is
`flutter test test/features/startup_pulse --no-pub` plus scoped `flutter analyze`.

Backend CI selects the database-free matrix and the independent database boundary
tests. The two tests importing the real private Valley implementation run through
the explicit local `--worker-source` gate. A mandatory cross-repository CI job
still needs a separately configured read credential and pinned Valley checkout;
the existing Content Factory credential must not be repurposed.

Local acceptance results on 31 October 2026:

| Check | Result |
| --- | --- |
| PostgreSQL backend and real Valley contract suite | 119 passed; disposable database teardown completed |
| Backend database-free selection/account/month checks | 21 passed, including 2,047 source subsets |
| Complete Valley suite | 454 passed, including real Redis concurrency |
| Desktop JavaScript suite | 5,975 passed |
| Canonical browser Pulse journeys | 33 passed |
| Native desktop mock-bridge Pulse journeys | 31 passed plus the new source-check/reload/resume test passed |
| Flutter Pulse suite | 92 passed |
| Model drift, new Python module lint, changed-file Biome checks, TypeScript builds and scoped Flutter analysis | Passed; no model changes |

Provider responses and model outputs were synthetic. A broader browser run was
stopped after a missing WebKit executable and a personal-agent composer assertion;
it is not a passing whole-repository browser gate. The native desktop's default
Python asset server also caused JavaScript chunk fetch resets. The failed Pulse
cases passed against a Vite preview of the same E2E build. Use an isolated Vite
preview and an explicit verified port when reproducing native desktop journeys.

## Rollout requirements

1. Deploy compatible Valley first: it handles both old and new receipt shapes.
2. Deploy backend next, then the Chat clients. Old workers cannot bypass the
   new backend's preflight and execution guards.
3. In an approved environment, test real provider permissions, refresh-token
   expiry, rate limits, revoked connections, empty periods, large catalogs,
   worker restart, queue failure and two simultaneous users/startups.
4. Inspect persisted source checks, cursor progress, execution epochs,
   `next_retry_at`, heartbeat, repair counts and dead letters. Confirm the
   existing reconciliation worker is scheduled and consumes its queue.
5. Verify the original saved draft can resume without creating another update
   or touching its published revision. Operational repair/deployment requires
   separate authorization under `AGENTS.md`.

Live provider acceptance, a production canary and production latency/error-rate
measurement remain release gates. Authentication revocation, provider downtime
and an empty reporting period cannot be guaranteed away; the contract is an
actionable source result and durable progress, with no fabricated or silently
incomplete update.

## Release validation against current main

On 2 November 2026, the user approved the exact 377-file set in
`pulse-reliability-release-test-migrations.json` for fresh disposable local and
GitHub CI test databases. Its SHA-256 is
`2a5810e4f9574febe4bc0c6fdf3666ee50fc1d78afe999b462e187672306e22f`.
The prior 374-file inventory remains unchanged. Three existing migrations from
main were added; this change creates no migrations and grants no production
migration permission. The rebased PostgreSQL Pulse/member-perk suite passed
199 tests, with database teardown completed. Another 57 provider, worker and
ticket-policy checks passed without database access.

For the rebased disposable runner, add
`--migration-inventory docs/pulse-reliability-release-test-migrations.json`
and pass the new approved digest. Deployment retains the production migration
plan gate and must stop if a new, unapproved pending plan is found.

The final PostgreSQL Pulse, source-cache, existing collection, member-perk and
Home regression selection passed 366 tests. The legacy service admission
selection passed six tests, and the complete founder API suite passed 99 tests.
Founder status responses now use the saved preflight run even without Gmail
authorization, so one missing connector cannot hide other failed source checks.
Active runs retain their pinned source selection; changing it requires a new run.

### After Job identity adoption

The current main graph adds the separately approved
`workflow_runs/0008_contentfactoryrun_job_identity.py` to those unchanged 377
files. Use the [378-file inventory](pulse-reliability-job-identity-test-migrations.json)
with SHA-256 `6a24940b944af3e7b56ea2715661495672432238a9cd694cf4cb6eeaca0072f0`
for fresh disposable tests. The [approval record](pulse-reliability-job-identity-test-approval.md)
links the owner's existing approval of the four nullable identity/timestamp
columns; this Pulse correction creates no migration.

The combined local PostgreSQL selection passed 263 tests covering Pulse source
barriers, receipts, connector scoping, CRM protections and Job identity races.
Another 52 database-free regressions passed, including all 2,047 selected-source
subsets. All 11,612 current canonical CI selectors collect; model checks find no
drift. Hosted validation and production acceptance remain required before
declaring the release complete. Preserve the CRM release's adoption/deployment
order and require a fresh production migration plan before promotion.
