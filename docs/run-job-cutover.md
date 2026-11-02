# Content Factory Job to Run cutover

ContentFactoryRun is becoming the backend execution record. The existing dual-write flag mirrors Job facts into dedicated Run fields. Run status, tenant identity, approval and stall timestamps retain their own authority throughout the transition.

## Rollout sequence

1. Verify the deployed revision and effective dual-write setting in web and workers. GitHub variables alone are not runtime proof.
2. Run `python manage.py backfill_run_job_fields --dry-run`. Review conflicts, missing and ambiguous records and proposed article links.
3. Apply the reviewed existing-run-only scope with `python manage.py backfill_run_job_fields --apply --batch-size 500`. Do not add `--create` routinely. Repeat the dry run to establish idempotence.
4. Run `python manage.py compare_run_job_fields --check`. A passing result requires a nonempty, completely matched population. Missing, unfilled, ambiguous, orphan, incomplete-identity and mismatched records block the check. Report-only use without `--check` keeps its previous exit behavior.
5. Resolve every blocker. A date-filtered comparison covers only that subset; the global read flag is not date-scoped. Do not waive missing historical records without enforcing the same eligibility boundary in readers.
6. Enable shadow reads only after parity, then exercise both Slack thread resolvers and the billing/entitlement read guards. Empty mismatch logs without traffic are not evidence. Include equal-rank routing candidates with different Job/Run timestamps.
7. Enable Run reads after observed shadow parity and verify the actual runtime settings. This switches only the existing five read sites; it does not migrate all remaining Job consumers.

`--updated-since YYYY-MM-DD` starts at midnight in the backend's configured timezone, not an exact activation instant. For precise post-activation proof, inspect timestamps against the recorded deployment time without printing founder identities or content.

## Identity and timestamps

The Run's domain and slack_user_id can differ from the old Job's. Preserve legacy read behavior in dedicated job_domain and job_slack_user_id columns; never rewrite Run identity to force parity. The compatibility view also needs job_created_at and job_updated_at for the ordering that used to operate on Job timestamps. Null means not mirrored; empty strings remain valid copied legacy values. Complete the approved identity migration and backfill before switching reads.

## Missing historical records

Do not create live or awaiting Runs from old Jobs. A newly runnable record can be picked up by reconciliation or shown to a founder. For finished Jobs, establish canonical execution identity, current ownership, purge history, billing settlement and article references before allowing creation. A current organisation alone does not prove that its old marketing data was not purged.

Historical `--create` accepts only the explicit phases `pr_opened`, `needs_review`, `confirmed`, `cancelled`, `completed`, `error`, `failed` and `denied`. It excludes `blocked`, `auth_required`, all awaiting phases and unreviewed wire aliases. The shared lifecycle contract still classifies blocked work as stopped; that does not grant permission to recreate it. `create_skipped_live` includes all phases outside this history allowlist. Filling facts on an existing blocked Run remains supported and does not change its lifecycle.

Preserve ledger and article references. Never delete a charged Job or recreate purged content just to make the parity check pass. The current creation command lacks an organisation allowlist/purge exclusion, so it is not suitable for blanket repair of a mixed historical population.

## Rollback

Before run-only writes, disable ARTICLES_RUN_JOB_READ_ENABLED to serve the authoritative Jobs again. Keep dual write during investigation unless the mirror itself is faulty. Disable shadow comparison separately if it is the source of a problem.

Do not use `backfill_run_job_fields --undo --apply` as a flag rollback. It blanks mirrored columns and unlinks articles broadly, and dual write can refill records during the operation. Any undo needs its own reviewed scope. Preserve additive columns when rolling application code back; dropping them is a separate destructive migration.

After Run becomes the write owner for a field domain, rollback additionally requires its one-way Job compatibility projection to be current. Never permit competing Job-to-Run and Run-to-Job authorities for the same field.

## Remaining implementation

Inventory Job creation, callbacks, progress, billing, selection, owner actions, scheduling, reports and foreign-key consumers before migrating writes. RunJobView is deliberately read-only and cannot replace mutable Job objects or their relations. Migrate complete field domains through explicit services, with one owner and a tested compatibility path. Billing changes must integrate the founder P2 escrow/dispatch implementation.

CF run events are currently observational and snapshots still control status. Event-authoritative lifecycle reduction, snapshot retirement and Job-table removal are separate stages. Final removal needs validated retention/restore procedures and approval for its specific migration.
