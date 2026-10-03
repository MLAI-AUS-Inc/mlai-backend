# Backend refactor release preparation — 15 September 2026

Status: prepared on current main; database regression validation and merge pending.
No refactor branch has been pushed, merged or deployed.

## Release scope

The original checkout at `ea3dec37fcacb6665262d610492ffd392fdfc4d6` was 112 commits
behind main. Release branch `codex/backend-refactor-release` starts at
`b5ecbbecf7a9f4774785013f38a6c00b08682d2c`. It contains the refactor delta against
the pre-refactor working snapshot, reconciled with current main. The original
checkout and its unrelated uncommitted work are preserved.

The tenant/financial ownership protections, credential encryption, authentication
extraction, worker inventory, Jobs queueing, dependency lock and test fixes are
included. New main's Office Manager failure classification, scheduler heartbeat,
feature flag recovery and migration attestation remain in place. The deployment
trap now also covers writer shutdown and aborts migration if shutdown fails.
Current-main Slack import metadata expectations are retained. Five newly
unassigned test modules have been added to CI; the omission baseline is unchanged.

The earlier 2,111-test result belongs to the older combined checkout. It does not
certify this integration with current main.

## Validation of this integration

- 91 focused tests passed without database or external network access, including
  shared-runner failure propagation, Office Manager delivery failures, heartbeat
  success/failure updates, deployment recovery and credential field behavior.
- Django system checks and migration drift dry-run passed.
- The requirements lock matches both inputs; CI assignment reports 159 fully
  selected modules, six partial selections, 131 existing omissions and zero new
  omissions. Existing omissions are recorded debt, not passing test evidence.
- Shell syntax, Python syntax and whitespace checks passed.
- No database migration has been applied during this integration pass.

## Exact additional test approval required

The read-only migration loader found 347 files: the original 336 approved
historical files, the two approved credential migrations, and nine newer files
already on main. All original 336 hashes are unchanged. Both credential
migration hashes are unchanged from their approved implementation.

Approve replay of the nine additional historical migrations below in newly
created disposable local databases, and replay of the full
[347-file inventory](refactor-release-test-migrations-2026-09-15.json) in
GitHub CI's disposable databases. All test data and credentials will be synthetic.
This proposal does not authorize applying additional migrations to production,
using production data locally, or modifying any historical migration.

| Additional historical migration | SHA-256 |
| --- | --- |
| `integrations.0047_message_sync_reliability` | `2190726769574a6f91ea123fa4df76cd9b16ff9ff45fc500c113fa2a419af27f` |
| `roo.0034_officemanagerday_coworkingbooking_booking_source_and_more` | `80f66fce7d33a436a671f74c5bbc76fa214583e2fdc7e82c43d4bfe9981a3fb0` |
| `roo.0035_protect_office_manager_assignment_day` | `46320a03d1028898c7b6e96694c5d7834772f4d761ac0b43078d08f78001f3cc` |
| `roo.0036_office_manager_attempts_and_provenance` | `785ebcd719db7077f14d7e5839dc482c2c5a4e6d336d2f882d003229c0b42834` |
| `roo.0037_quarantine_legacy_office_manager_provenance` | `561f6ecbe1aa562f50af2713c9fcd5755f785a8a273cdd0769a0c8b795b21e53` |
| `roo.0038_office_manager_claim_generation` | `f71d8640ba09137826139955d1362df90594ab8955a518396d0117d42feda70d` |
| `roo.0039_supersede_reopened_office_manager_attempts` | `2d25b976b9e07d5e213e77ddbe56ba6da7a74817516b12e36f5c7780471a0b4f` |
| `roo.0040_merge_coworking_operations_office_manager` | `66bbe16163b664d0105e85d2089f3d03d9dcf99a01e9a3318d4196ff33d6f96b` |
| `startup_updates.0023_independent_update_identity` | `d770852987262d11c2af5a438f9fc6ca0973c76d8a0a45c62dd6fb945e37f318` |

The existing disposable test harness still enforces the original approved
inventory. It has not been widened or bypassed. After approval, its guard can be
updated to the exact inventory above and the current SQLite/PostgreSQL regression
lanes rerun. Hosted CI must then pass on the final release tree before merging.

[AGENTS.md](../AGENTS.md) requires: “Never create, run, or apply a database
migration without explicit user approval for that specific migration.” It also
requires approval before database-backed Django tests. The original approval
was explicitly limited to disposable local databases and the original inventory;
this is why current-main replay and hosted CI need the scope extension.

## Production boundary

The user has already approved production application of
`content_factory.0039_encrypt_github_credentials` and
`content_factory.0040_backfill_github_credential_envelopes`. That approval is
preserved; no further approval for these two is requested.

Read-only release inspection found a healthy deployed web, scheduler and bridge,
and `migrate --plan` reported no pending operations for the running release.
Main's deployment at `b5ecbbe` was still in progress during this inspection, so
these observations do not establish that its latest migration has landed.
Before merging, verify the completed main release and actual pending plan again.
The refactor release must introduce only the two approved credential migrations
in production. All writers must be stopped successfully before applying them;
partial backfill requires forward recovery, never a plaintext-writing rollback.
