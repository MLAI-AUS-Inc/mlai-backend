# Account ban migration proposal

Approved migration: `core.0069_account_ban` (depends on the current `0068` merge).

The user explicitly approved migration creation and disposable local database
tests on 2026-09-22. The migration file has been generated with this exact scope.

Create an `AccountBan` table with:

- `id`: default primary key.
- `user`: unique foreign key to the MLAI account, protected against deletion.
- `email`: canonical lowercase email, unique (including a case-insensitive unique constraint).
- `reason`: optional text, limited to 1,000 characters by the API.
- `banned_by`: nullable foreign key to the acting administrator, set null if that administrator is deleted.
- `created_at` and `updated_at`: creation and last-change timestamps.
- `revoked_at` and `revoked_by`: nullable timestamp/administrator for lifting a ban.
- `revocation_pending`: boolean, initially true, for retrying chat-device revocations after an adapter outage.

The existing account remains in place. Applying a ban disables it and increments
its existing authentication version. The ban record prevents reactivation and
email replacement while the ban is active. Administrators can lift the ban;
the email and audit history remain. Lifting a ban does not revive old sessions.

Granted migration permission covers creating this one migration and applying
repository migrations only to an isolated, disposable local test database to
validate it. It does not cover a production migration or deployment.
