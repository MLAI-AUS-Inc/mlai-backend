# Credential migration and disposable-test proposal — 14 September 2026

The [15 September release preparation](backend-refactor-release-2026-09-15.md)
records newer main migrations and the proposed extension for current local/CI tests.

Status: explicitly approved by the user on 14 September 2026 for creation and
disposable local testing. Both migrations are implemented and have been replayed
against fresh PostgreSQL and SQLite databases with synthetic credentials.
On 15 September 2026 the user additionally approved production application of
these exact two migrations after resolving the 18 outstanding failures and
reviewing the combined working changes. That approval is recorded; production
application has not occurred. See the
[implementation report](backend-refactor-implementation-2026-09-14.md) for results.

The compatibility field can be reviewed in
[`integrations/fields.py`](../integrations/fields.py):
`LegacyPlaintextEncryptedTextField` reads legacy plaintext and existing valid
credential envelopes, encrypts new writes, and rejects corrupt encrypted values.
It is now bound to both GitHub credential columns in the model. Ordinary ORM
writes use encryption; historical plaintext remains readable during conversion.

## Approved migration 1

Exact name: `content_factory.0039_encrypt_github_credentials`.

Dependency: `content_factory.0038_delete_seo_topicmap_researchsession`.

Alter the migration state and model declarations for
`OrganizationContentConfig.github_token_encrypted` and
`OrganizationContentConfig.github_refresh_token_encrypted` to use
`integrations.fields.LegacyPlaintextEncryptedTextField(blank=True, null=True)`.
Keep the existing table and column names, SQL text storage type, nullability and
relationships. No table or row is removed. This enables compatibility reads and
encrypted new writes; it does not claim existing plaintext has been converted.

## Approved migration 2

Exact name: `content_factory.0040_backfill_github_credential_envelopes`.

Dependency: `content_factory.0039_encrypt_github_credentials`.

Backfill only those two credential columns in
`content_factory_org_config`. Use the existing credential keyring
and envelope format, inspect raw stored values so encrypted rows can be
distinguished, preserve null/empty values, validate existing envelopes and fail
closed on unreadable ciphertext. Encrypt only plaintext values. Never log or
put credentials into fixtures, migration text or result output.

Use a non-atomic outer migration with small transactional batches and row locks;
re-read each locked row before updating its two columns. This avoids replacing
a concurrently refreshed credential with an older value. Process rows in stable
primary-key order and make reruns safe if a batch fails. Complete with a raw
verification that no nonempty plaintext values remain. The backfill is
forward-only: reversing it must never decrypt stored credentials into plaintext.
Use synthetic keys and values in tests.

All writers must use the compatibility field before a real-environment
backfill; older binaries can still write plaintext. The production deployment
sequence must coordinate all writers and retain forward recovery. The existing
automatic deployment applies every pending migration, so approval of these two
files does not authorize other pending migrations in the combined checkout. A later strict
encrypted-field conversion or credential-authority consolidation is a separate
change, not included here.

## Approved disposable local validation scope

The user approved creation of the two named migration files and replay of the exact
existing migrations listed in
[`refactor-test-migrations-2026-09-14.json`](refactor-test-migrations-2026-09-14.json),
plus the two migrations above, **only in newly created disposable local test
databases with synthetic data and credentials**. Existing local databases remain
outside that approval. The subsequent production approval is limited to the two
credential migrations named above.

The inventory contains 336 migration files under Django 5.2.17: 318 from this
checkout and 18 from installed Django applications. It records every app/name
and file SHA-256, not just terminal migration names. It includes the pre-existing
uncommitted `community_chat.0010_moderator` and
`startup_updates.0022_reporting_evidence_revisions`; those were not created by
this refactor. Regenerate and review the inventory if any listed file changes.

Validation covers fresh migration replay, credential conversion from seeded
legacy/encrypted/empty values, corrupt-envelope failure, restart after partial
conversion, financial ownership conflicts, founder offboarding/domain claims,
Jobs queue/history behavior and targeted authentication regressions. Use a
disposable PostgreSQL database for row-lock/concurrency behavior; SQLite alone
cannot verify those guarantees. After narrow checks pass, run the affected CI
lanes with the locked Django 5.2 environment. Any failures need investigation
against the existing working-change baseline before claiming regressions.

Approval does not include tenant lifecycle columns, shared external-account
tables, Jobs lease/fencing columns, data retirement, or other production backfills.
Those require their own concrete schema proposals.

The pre-existing `community_chat.0010_moderator` and
`startup_updates.0022_reporting_evidence_revisions` are included in the disposable
test inventory, but this conversation does not approve their production
application. Review the actual pending production plan before choosing a release
that includes them. No production plan was read during this local validation.

The approval boundary comes from [AGENTS.md](../AGENTS.md): “Never create, run,
or apply a database migration without explicit user approval for that specific
migration.” It also requires migration approval before database-backed Django
tests. The user's explicit approval satisfies this boundary for the operations above.
