# Backend refactor implementation — 14 September 2026

Historical validation status for the original checkout: first refactor batch
and the two approved credential migrations are implemented. Its local configured
CI selection passed: 2,081 passed, 30 skipped,
zero failures/errors. Targeted PostgreSQL regressions and seeded migration replay pass.
This is not a production rollout or completion of every structural audit item.

See the [15 September release preparation](backend-refactor-release-2026-09-15.md)
for integration with newer main, current validation and the additional test scope.

The starting checkout was `codex/slack-emoji-reactions` at
`ea3dec37fcacb6665262d610492ffd392fdfc4d6`, with substantial existing community,
editorial and reporting changes. Those changes were preserved. After the user
explicitly approved the named
migrations, validation used only new disposable local databases and synthetic
credentials. No commit, push, production access or deployment was performed.

## Changes mapped to the audit

| Audit area | Implemented behavior | Remaining work |
| --- | --- | --- |
| A: tenant offboarding | A retained organisation with no founder owner is no longer automatically claimable; linking an existing tenant checks ownership under a lock. Offboarding distinguishes targeted cleanup from complete tenant erasure and keeps the domain reserved. | Explicit tenant closure/transfer/release policy, durable lifecycle schema, exhaustive deletion/retention registry |
| B: GitHub credentials | Both model fields now encrypt ORM writes. Approved 0039 changes migration state; 0040 converts plaintext in locked, restartable batches and verifies the result. Seeded replay, invalid ciphertext, concurrent refresh and restart tests pass. | Coordinated production rollout and consolidation of credential authorities remain. Production token storage was not inspected or changed. |
| C: financial ownership | All seven service upsert sites use one transactional writer. Existing records cannot change connection, organisation, user or record type during an ordinary sync. | Existing global upstream identity still means a second connection can be rejected. Explicit shared-account ownership schema and data reconciliation remain. |
| D: worker deployment | One service inventory covers start/stop/rollback; password email and Jobs are required workers; committee remuneration is selected when enabled. | Production rollout and actual worker health verification |
| E: framework and test selection | Django 5.2.17, DRF 3.16.1, SimpleJWT 5.5.1; expanded CI labels; new-module assignment check with explicit dated omissions | Broader CI findings below and disposition of 137 omitted/6 partially selected modules |
| F: scheduler and Jobs | Shared scheduler only enqueues Jobs; dedicated worker consumes runs; returned runner failures now fail the tick while other runners continue; aggregate queue health command added | Durable leases/fencing, interrupted-run recovery, publish idempotency and isolation of other slow selectors |
| G: workflow boundaries | Run/step status normalization moved to workflow_runs; reconciliation no longer imports those helpers from the browser view; finance imports models from their owning apps | Central transition service, callback/reconciliation race contracts and broader Content Factory view extraction |
| H: shared authentication | JWT implementation belongs to core; hospital import remains a compatibility shim; identity/profile responses use one team projection | Versioned removal of event fields needs consumer evidence; existing contracts remain |
| I: duplicate/query cleanup | Removed shadow Home view/route and duplicate deletion route; JSON and HTML Jobs history share prefetched run data | Three-query Jobs history and existing JSON/HTML response regressions now pass; continue profiling other endpoints |
| J: reproducible dependencies | Hashed universal lock includes both requirements inputs; CI and Docker install it; input-signature guard rejects a stale lock | Build one immutable image in CI and promote its digest; current OS/browser/base-image resolution remains mutable |
| K: lifecycle/documentation | Added feature register, runtime contract, test-debt review date and corrected monthly-reminder Compose service name | Assign named product/operations owners and collect authorised usage/retention evidence |

No new table drop is proposed. The earlier keep decisions for eSafety,
HealthHack and Watt/generic hackathons remain in place.

## Observable contract changes

Founder onboarding with an existing unowned domain now requires administrator
ownership review instead of silently reusing the retained tenant. Offboarding
returns `orgDataPurged: false`, `domainReleased: false`, and a separate
`targetedPurgeCompleted` result; retained organisation history means complete
tenant erasure cannot truthfully be reported. Existing company links are kept.

Financial syncs reject conflicting ownership rather than updating the historical
record's owner. Legitimate shared external accounts need a separately reviewed
transfer/shared-account flow. Ordinary same-owner syncs remain upserts.

JWT header/cookie behavior and trusted-origin enforcement are preserved by
moving the same implementation. Existing `hospital.authentication` imports keep
working. Community Home resolves to its existing canonical view. Jobs history
keeps response fields and limits while using prefetched relations. Scheduler
output retains per-runner results, but returned failures now produce a nonzero
command result instead of being reported as a successful tick.

## Validation completed

Final validation completed on 15 September 2026 (Australia/Melbourne).

A separate Python 3.11 environment was installed from the shared lock; the
existing `.venv` was left untouched. All 100 installed packages passed the
dependency compatibility check. Every database used here was newly created,
used synthetic credentials, and was removed after the test process stopped.

- **289 targeted PostgreSQL tests passed**, covering credentials, tenant and
  financial ownership, authentication, Jobs history, connector syncs, Content
  Factory callbacks and provider events. This includes concurrent first-sync
  ownership and credential refresh blocked by the real PostgreSQL row lock.
- **33 additional PostgreSQL memory/search/review/consolidation tests passed**,
  including reviewed corrections and vector constraint validation.
- **58 isolated unit checks passed**, plus the updated scheduler-registration
  test. The previous implementation batch's 136 editorial checks also passed.
- **63 additional SQLite compatibility tests passed** after fixing removed
  Django UTC helpers, NumPy-vector constraint validation, tenant fixtures and
  historical actor migration fixtures. These include both affected historical
  actor migration tests.
- Fresh PostgreSQL and SQLite replay validated all 336 approved historical
  migrations plus the two new migrations. Seeded historical plaintext at 0038
  was readable at 0039 and encrypted by 0040; existing ciphertext, empty strings
  and nulls were preserved. The SQLite credential/query subset ran 11 tests:
  nine passed and the two PostgreSQL concurrency cases were skipped there.
- System checks and `makemigrations --check --dry-run` pass; no extra migration
  was generated. CI assignment reports 126 fully selected modules, 6 partial,
  137 existing omissions and no new omissions.
- Shell syntax, Python parsing, document links, lock input signature and
  whitespace checks pass.

### Issues found by broader validation

The initial full configured main CI test selection ran 2,111 tests under the
isolated SQLite harness. It reported 44 failures/errors and 30 skips. Those
initial failures were investigated rather than treated as an acceptable baseline.

The refactor now uses Python's standard `datetime.UTC` in memory scheduling and
review windows. The embedding model retains field validation and all scalar
constraints while excluding the NumPy vector from Django 5.2's scalar constraint
expression map; no database constraint references that vector. Historical actor
fixtures keep the irreversible credential backfill applied while rewinding only
the actor-related history. The scheduler registration test now exercises the
registered callable instead of matching its old source spelling. Founder run
fixtures establish the ownership that real onboarding creates.

Database validation also found two older source defects: Jobs service Bearer
keys were intercepted by the global JWT authenticator, and memory wake selectors
used PostgreSQL's unsupported `SELECT FOR UPDATE DISTINCT`. The service endpoint
now preserves its opaque-token and Roo-key contract, including 401 for invalid
credentials. Wake selectors deduplicate via an ID subquery and require the same
scope to be selected; generic and Gmail wake regressions pass on PostgreSQL.
Review resolution, consolidation approval and correction also acquire their
mutable row locks directly, avoiding nullable outer joins in PostgreSQL.

### Follow-up failure resolution and combined-change review — 15 September

The previous main selection reported 18 failures. Every one was investigated
against the current contracts, with external networking still forbidden:

| Category | Count | Resolution |
| --- | ---: | --- |
| Token-usage leaderboard | 14 | Local ranking fixtures now supply an empty public-provider result; federation-specific tests retain their explicit upstream entries and the provider adapter retains its own mocked transport tests. |
| Xero preview after an empty scan | 1 | The fixture supplies the active bank-account catalogue and an empty project catalogue, preserving real reconciliation/preview logic and accounting checks. |
| Slack consent renewal | 1 | The test verifies that same-window active reconnect preserves its checkpoint, then pauses, renews consent, verifies history stays blocked, and rediscovers/provisions before resuming the scan. |
| Slack echo/deletion reconciliation | 1 | Synthetic timestamps remain inside the bounded consent window. The no-echo assertion distinguishes content-free scan checkpoints from actual message/reaction deliveries, and still checks exact deletion reconciliation. |
| Slack imported-channel response | 1 | The expected catalog includes nullable `last_message_at` and boolean `source_archived`, matching the current implementation; the owning contract now lists both fields. |

No tests were disabled, no expected-failure markers were added, and the network
and disposable-database guards remain enabled. The two credential migrations
were not changed during this follow-up.

Review used the saved pre-refactor working snapshot and HEAD, focusing on the
15 tracked files shared with existing work, plus new refactor modules and their
callers. The original moderation, Slack import, editorial policy and monthly
reporting changes remain in the checkout. Review covered:

- Shared authentication and Home routes alongside device roles, Volunteer,
  coworking and Slack ownership contracts.
- Financial record ownership alongside the existing invoice/revenue evidence
  logic and immutable monthly revisions.
- Credential ORM reads/writes alongside GitHub scanning and Content Factory's
  existing editorial callbacks; status extraction preserves the prior mappings.
- Migration dependencies and historical actor test fixtures, Jobs enqueue/worker
  wiring, all-writer shutdown and forward recovery.

The review found and fixed two deployment control-flow defects. Writer shutdown
previously ignored a failing `docker compose stop`. The error trap also used
`set +e` and returned, which allowed execution to continue after a failed
migration. Shutdown now runs under the trap without swallowing its status;
recovery exits with the original failure code. Pre-migration failures restore
previous images, while failures after migration execution begins leave writers
stopped. A shell regression executes the real error handler and command boundary
with Docker/management transports stubbed: successful execution, failed stop and
failed migration all take the expected path. No real deployment command is run.

Additional checks passed: **35 PostgreSQL refactor/credential/Slack regressions**,
**81 PostgreSQL checks for the existing moderation, all-history/refresh and monthly
evidence/revision features**, **136 editorial unit checks**, and **55 isolated
refactor/runtime/deployment checks**. These overlap some earlier selections and
must not be summed as unique tests.

The final configured main CI selection completed **2,111 tests: 2,081 passed,
30 skipped, zero failures and zero errors** in 419.127 seconds. This run includes
all 18 corrected cases and the updated deployment recovery test. System checks
and migration drift checks also pass. All disposable databases were removed.
Exact test IDs, prior failure evidence and current results are in
[the validation record](backend-refactor-validation-2026-09-14.json).

Full hosted GitHub CI, container builds and production runtime checks have not
been invoked. Local regression success does not establish live deployment state
or complete every structural item in the original audit.

## Next sequence

The [approved migration scope](backend-refactor-migration-proposal-2026-09-14.md)
covers `content_factory.0039_encrypt_github_credentials`,
`content_factory.0040_backfill_github_credential_envelopes`, and the exact existing
migration inventory used for disposable tests. Creation and local validation are
complete. On 15 September the user approved production application of exactly
0039 and 0040 after this failure-resolution/review pass. That approval is recorded;
production has not been accessed or migrated. Rollout must coordinate every
credential writer and the new Jobs worker. The backfill is irreversible;
rollback must never restore plaintext credentials.

The current checkout also contains the earlier, uncommitted
`community_chat.0010_moderator` and
`startup_updates.0022_reporting_evidence_revisions`. They are approved in the
local test inventory; this conversation does not approve their production
application. The deployment script applies all pending migrations, so inspect
the actual production plan and choose a release composition within the approved
scope before a main merge. Their production status was not inferred from local
files. The existing editorial work also retains its documented backend/worker
release coordination requirements. No files were staged, committed or pushed.

Then address tenant lifecycle/shared-account ownership, Jobs recovery and common
workflow transitions as separate bounded changes. Their acceptance criteria are
in the [audit](backend-refactor-audit-2026-09-14.md); code containment does not
replace those structural changes.

Current maintained references: [runtime contract](backend-runtime.md),
[feature lifecycle register](feature-lifecycle.md), and
[documentation index](README.md).
