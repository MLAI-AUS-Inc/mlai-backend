# Website target generation key proposal

Incident 28 required specific migration approval under `AGENTS.md`. On
2026-10-07 the user approved creation of the exact model/constraint/upsert change
described below, without applying it. The reviewed migration file is now
`content_factory/migrations/0044_website_target_generation_key.py`. No migration
has been applied and no database-backed test or deployment has run.

The proposed `0044_website_target_generation_key` depends exactly on
`("content_factory", "0043_merge_credentials_website")`, the current merge leaf
joining `0040_backfill_github_credential_envelopes` and
`0042_website_connection_lifecycle`. Confirm the leaf has not moved before
reviewing deployment of the approved file.

The model change replaces:

```python
models.UniqueConstraint(fields=["connection", "target_key"], name="cf_web_target_key_unique")
```

with:

```python
models.UniqueConstraint(fields=["connection", "generation", "target_key"], name="cf_web_target_generation_unique")
```

The forward operation adds the per-generation constraint first,
then removes `cf_web_target_key_unique`. Existing rows retain their generation;
there is no inferred authorization, row promotion, destructive cleanup or data
backfill. All new target upserts must then include generation in their lookup.
The corresponding model Meta constraint and every upsert must ship together.
The lookup change applies to `record_scan_evidence` in
`content_factory/website_connections.py` and `promote_custom_target` in
`content_factory/website_support.py`: include `generation=website.generation`
in the `update_or_create` lookup rather than only in defaults. Their collision
guards currently deny reuse of an older row. After migration, look up previous
proof within the exact generation and keep all older rows as history.

Before that approval, writes to an existing target from another generation return
`website_target_generation_migration_required`. Exact-generation verified rows
retain their accepted proof and cannot be overwritten by an inventory scan or a
weaker contract. This preserves consent without silently reusing an older row.

Review the exact generated migration and its pending dependency graph before
application approval. Database-backed concurrency, constraint and forward/reverse migration
checks are still required in an explicitly approved disposable database. This
proposal does not grant production migration or deployment authority.

Rollback requires special care after more than one generation exists for a
connection/target key. Re-adding the old unique constraint would fail on those
duplicates. Stop target writes, inventory duplicate `(connection_id, target_key)`
groups, and obtain a separate reviewed archival/data-preservation plan before
reversing. Do not delete historical authorization or choose the newest row
automatically. An immediate reverse before any duplicates have been created is
safe. PostgreSQL constraint creation takes a table lock; choose the deployment
window after checking row count and existing transactions. Approval to create
this migration and test it against a disposable database is distinct from
approval to apply it to production.
