# Slack-Founder actor migration recovery

`core.0064_guard_legacy_actor_migration_history` is a forward-only safety gate
for databases that may have applied an earlier committed body of
`core.0063_canonicalize_legacy_content_factory_actor_ids`.
`core.0065_recheck_legacy_actor_migration_attestation` runs the corrected guard
for databases that may already have recorded the first `0064` body.

The guard never logs or reports credential values. It stops migration when it
finds either of these ambiguous states:

- legacy and canonical `UserIntegration` rows have different GitHub state, but
  a dependent record now references the canonical actor;
- legacy and canonical integration state differs, because current rows cannot
  prove which committed `0063` body ran—even when a legacy reference survives;
- a canonical `mlai_user:<id>` marker appears in a JSON location that is not a
  named actor field and may have been ordinary user content rewritten by the
  earliest `0063` body.

## Before deployment

Check every persistent environment's `django_migrations` table for the applied
timestamps of `core.0063_canonicalize_legacy_content_factory_actor_ids` and
`core.0064_guard_legacy_actor_migration_history`, then correlate both with the
deployed application commit. A green migration test on a new database does not
identify which body an existing database executed. Never fake `0064` or `0065`;
`0065` is the required recheck when an earlier `0064` was already recorded.

Take a database backup before any operator repair. Do not copy access tokens,
refresh tokens, installation IDs, scopes, repositories, or scan state
field-by-field between integration rows.

## If `0064` or `0065` stops

The exception reports only internal actor IDs and every affected record ID; it
does not truncate the finding list. For each reported legacy/canonical pair:

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

Some valid databases intentionally retain two independent GitHub authorities:
legacy-owned records continue to reference `web_<id>`, while unrelated records
that were already owned by the canonical authority reference `mlai_user:<id>`.
Current rows alone cannot distinguish that valid history from an older `0063`
body followed by later writes. Do not repoint either set merely to make the
guard pass.

If every reported owner and non-actor JSON value has been verified from
external evidence, but the valid state must remain unchanged, use the exact
`attestation_fingerprint` printed by the migration error as
`CORE_ACTOR_MIGRATION_HISTORY_ATTESTATION` for that migration run only. The
fingerprint contains no credential values and is bound to the complete finding
set, referenced record IDs, credential-bundle digests, and ambiguous-payload
digests, so any ownership-relevant change invalidates it. Record the evidence
and fingerprint in the deployment log, set it before retrying the stopped
migration, then remove the environment value immediately after `0065` records
success.
An attestation is not permission to skip investigation or repair a state whose
ownership is still unknown.

## Guard state matrix

| Integration authority | Dependent references | Required result |
| --- | --- | --- |
| Identical bundle | legacy, canonical, both, or none | Pass if no separate non-actor JSON finding exists; ownership does not differ |
| Distinct bundles | legacy only | Audit, then repair or attest; later writes make lineage unknowable |
| Distinct bundles | canonical only | Audit, then repair or attest |
| Distinct bundles | both | Preserve verified independent owners; attest if no repair is needed |
| Distinct bundles | none | Audit bundle ownership, then repair or attest |

A valid and an unsafe history can produce each distinct-bundle row shape. The
guard therefore never treats reference placement as migration provenance.

Rollback is forward-only: `0063`, `0064`, and `0065` intentionally have no
reverse data mutation. If recovery cannot be proven, leave deployment stopped
and escalate to the data owner.
