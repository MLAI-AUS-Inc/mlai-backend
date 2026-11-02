# Preserve Job identity during the Run cutover

Status: the owner approved this specific migration, disposable database tests and normal backend deployment in the continuation chat on 2 November 2026. Implementation and validation are in progress; this approval is limited to the four fields below.

## Problem

The production existing-run backfill on 2 November 2026 filled 1,782 Runs and linked 51 articles without changing Run status or updated_at. The comparison found 52 Jobs with differing identity facts: 50 differ in domain and 43 in slack_user_id (overlapping populations). The original fold reused the Run's identity fields and intentionally omitted the Job's timestamps. Those assumptions cannot preserve the existing read behavior. Changing the Run's identity would affect tenant/authority checks; substituting its timestamps can change Slack routing ties.

## Exact proposed migration

Add `workflow_runs/0008_contentfactoryrun_job_identity.py`, depending on `workflow_runs/0007_contentfactoryrun_slack_thread_indexes`, with four nullable fields on ContentFactoryRun:

| Column | Django field | PostgreSQL type |
| --- | --- | --- |
| job_domain | CharField(max_length=255, null=True, blank=True) | varchar(255) NULL |
| job_slack_user_id | CharField(max_length=50, null=True, blank=True) | varchar(50) NULL |
| job_created_at | DateTimeField(null=True, blank=True) | timestamp with time zone NULL |
| job_updated_at | DateTimeField(null=True, blank=True) | timestamp with time zone NULL |

No data migration, indexes, constraints, row creation, defaults or ledger changes are proposed. The four ADD COLUMN operations require a short ACCESS EXCLUSIVE lock; nullable fields without defaults do not rewrite the existing rows. Deploy should use the existing writer-pause and approved migration-plan digest protocol.

The requested approval scope is creating this one migration, applying the repository's exact current migration graph plus this migration to disposable local test databases, and deploying this one new migration through the reviewed backend deployment. Production backfill/flag cutover remains subject to parity and coverage checks.

## Code behavior after approval

Mirror the Job's identity and timestamps to the four dedicated columns under the existing dual-write flag. The view reads those columns, preserving empty strings separately from null (null means not yet copied). The backfill fills only null identity/timestamp columns, never changes the Run's own domain, slack_user_id, created_at or updated_at, and reports conflicts. Do not enable reads until all required records have complete identity data. Preserve the current mirror/read behavior with flags off.

Use Job timestamps for the compatibility view's sorting. Keep Run timestamps authoritative for execution recency and stall detection. Test mapping, partial writes, backfill idempotence, timestamp parity and thread resolution, including differences in Job vs Run identity.

## Rollback and remaining blockers

Disable Run reads before rolling application code back; retain the additive columns and their data. Dropping columns is a separate destructive migration requiring separate approval. Do not run the existing broad backfill undo as a flag rollback.

There are separately 254 Jobs with no Run: 187 terminal and 67 non-terminal; 18 have no current organisation or marketing config. These are historical January–August records. Four have articles and two retain charged billing status. This proposal does not recreate, delete, settle or hide any of them. They require an explicit history/purge policy and reader coverage before global cutover.
