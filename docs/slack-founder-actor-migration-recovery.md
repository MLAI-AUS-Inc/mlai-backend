# Slack-Founder actor migration recovery

`core.0064_guard_legacy_actor_migration_history` is an append-only safety gate
for databases that may have applied an earlier committed body of
`core.0063_canonicalize_legacy_content_factory_actor_ids`.

The guard never logs or reports credential values. It stops migration when it
finds either of these ambiguous states:

- legacy and canonical `UserIntegration` rows have different GitHub state, but
  a dependent record now references the canonical actor;
- legacy and canonical integration state differs and no surviving reference to
  the legacy actor proves that the current safe `0063` body preserved it;
- a canonical `mlai_user:<id>` marker appears in a JSON location that is not a
  named actor field and may have been ordinary user content rewritten by the
  earliest `0063` body.

## Before deployment

Check every persistent environment's `django_migrations` table for the applied
timestamp of `core.0063_canonicalize_legacy_content_factory_actor_ids` and
correlate it with the deployed application commit. A green migration test on a
new database does not identify which body an existing database executed.

Take a database backup before any operator repair. Do not copy access tokens,
refresh tokens, installation IDs, scopes, repositories, or scan state
field-by-field between integration rows.

## If `0064` stops

The exception reports only internal actor IDs and affected model fields. For
each reported legacy/canonical pair:

1. Establish the authoritative GitHub owner from external account evidence,
   deployment history, and the affected organisation or job. Do not infer it
   from whichever token field is non-empty.
2. Repoint only dependent records whose ownership has been established.
3. Keep both actor aliases while any dependent record still requires the
   legacy credential bundle.
4. For a non-actor JSON marker, restore the original value from an audited
   backup or other source evidence. Do not mechanically convert it back to a
   `web_<id>` string.
5. Re-run the migration only after the ambiguous state is gone and record the
   repair evidence in the deployment log.

Rollback is forward-only: both `0063` and `0064` intentionally have no reverse
data mutation. If recovery cannot be proven, leave deployment stopped and
escalate to the data owner.
