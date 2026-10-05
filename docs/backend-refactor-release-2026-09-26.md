# Backend refactor integration — 26 September 2026

Status: code integration implemented; specific migration approval and database/hosted validation pending. Not ready for production release yet.

## Integration scope

The refactor commit `50573c33ed74e6e90126a956c05d36e0fa48e983` is integrated with
main `68dbf14da85350cd7af2ad054d7b3868b3d98225`. All eight textual conflicts are
resolved. Main's account privacy and eligibility rules, recent Slack behavior,
article approval/review contracts and candidate-web deployment flow remain.
The branch retains the refactor's tenant/financial ownership, credential
compatibility, shared authentication, queued Jobs worker and Django 5.2.17 changes.

The dependency lock now includes Apple's signed-purchase verifier and all
requirements from both inputs. CI retains every test label from main and the
refactor, adds newly unassigned suites, and keeps its omission baseline unchanged.

## Deployment fixes discovered during integration

- Code-only deploys no longer hit the refactor's unconditional all-writer stop.
- Approved schema transitions stop the complete writer inventory, including the
  transient web candidate. A failed stop aborts before any migration execution.
- The candidate slot is not part of normal required-service startup.
- Jobs and password-email workers are required; committee remuneration follows
  its existing feature flag and stops when disabled.
- Code-only rollback stops new workers that have no prior image before restoring
  old runtime services. If that shutdown fails, the serving candidate stays up
  for recovery. Main's release freshness, migration-plan attestation, route-flip
  checks and forward-only schema recovery remain intact.

## Migration repair and validation scope

The exact [migration proposal](backend-refactor-migration-proposal-2026-09-26.md)
adds an empty `content_factory.0042_merge_credentials_editorial` dependency join.
It does not rewrite 0039/0040/0041 or change schema/data. Creating that file and
replaying the [356-file inventory](refactor-release-test-migrations-2026-09-26.json)
in disposable local/CI databases are awaiting specific approval. Every current
repository and locked-package migration hash was verified against that inventory;
the only absent file is the proposed merge.

The test harness now checks approval status, full file hashes, exact inventory
membership, duplicates/count and graph conflicts. Its mocked guard regression
suite passes without database access. A new `--replay-from-main` mode is prepared
to verify existing editorial attribution survives credential backfill and the
empty graph merge. It has not been executed because the scope is pending.

## Validation completed

All checks below use synthetic settings where Django initializes; no migrations
or database-backed suites were run in this integration pass.

- Application contracts: 66 tests passed, covering authentication/ownership,
  status mapping, current article approvals/progress and startup behavior.
- Standalone editorial contracts: 138 tests passed with environment, network and
  database guards.
- Dependency/CI additions: 39 guarded tests and 164 standalone editorial tests
  passed. These overlap the preceding suites and should not be added together.
- Deployment/runtime: 58 guarded tests and seven standalone handoff tests passed,
  including isolated local Nginx route flips and rollback checks.
- Additional Jobs delivery/scheduler checks: 14 tests passed.
- Migration approval/integrity and disposable database boundaries: 11 mocked tests passed.
- Lock signature, compatibility of all 105 installed packages, Apple verifier
  imports, workflow YAML, Python parsing, shell syntax, whitespace and tracked
  credential scan passed.
- Test assignment: 218 fully selected modules, nine partial, 127 recorded baseline
  omissions, zero new omissions. These omissions remain existing test debt.

## Remaining merge/release gates

1. Approve the specific empty merge migration and exact disposable replay scope.
2. Create that migration, validate the final graph/model state and run current
   SQLite/PostgreSQL lanes, seeded fresh/main-upgrade replay, credential row-lock
   concurrency and broader application regressions. Repair failures rather than
   relaxing the approved migration inventory or test expectations.
3. Open the PR and require passing hosted CI on its final tree before merge.
4. Before a separately authorized production release, inspect the exact deployed
   migration plan/key availability and verify coordinated writer shutdown and
   forward recovery. Previous approval for credential migrations alone does not
   authorize unrelated new production work.
