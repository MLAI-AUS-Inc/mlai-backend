# Pulse release test schema after Job identity adoption

The inventory in [pulse-reliability-job-identity-test-migrations.json](pulse-reliability-job-identity-test-migrations.json)
contains 378 migrations. Its SHA-256 is
`6a24940b944af3e7b56ea2715661495672432238a9cd694cf4cb6eeaca0072f0`.

All 377 migrations in the previously approved Pulse release inventory are
unchanged (prior inventory SHA-256
`2a5810e4f9574febe4bc0c6fdf3666ee50fc1d78afe999b462e187672306e22f`).
The sole addition is `workflow_runs/0008_contentfactoryrun_job_identity.py`,
SHA-256 `86bbf8cb5820ad0843a456330c5fdc43e2c20bac661871960e36f702ef3e7b13`.

The owner separately approved creating that specific migration, disposable tests
and normal backend deployment in the Founder Pipeline continuation on 2 November
2026, answering “Approve this migration, tests and deployment.” Its exact scope
is recorded in [the identity migration proposal](../plans/founder-robustness/job-identity-migration-proposal.md):
four nullable Job identity/timestamp columns, preserving Run authority and
recency. This is an existing main migration, not a Pulse schema change.

This inventory is used only for fresh disposable local and GitHub CI test
databases. Pulse release operations continue to require a read-only production
migration plan check and preserve the other release's adoption/deployment order.
This record grants no permission for another migration, data backfill, flag
cutover or destructive rollback.
