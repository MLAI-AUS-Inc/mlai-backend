# Remaining schema: preflight, inbox tests, reputation and reconfirmation

This proposes **one specific migration**, `crm.0004_preflight_and_lifecycle`,
depending on `crm.0003_campaign_waves_and_planner`. It supplies the remaining
schema from the supplied campaign plan. It does not activate any feature.

## Exact operations

1. `Blast`: `preview_text` (CharField, max 200, blank), `sign_off_name`
   (CharField, max 120, blank), `preflight` (JSONField, default dict, blank).
2. `Contact`: nullable/blank DateTimeField `consent_reconfirmed_at`.
3. `SeedMailbox`: UUID primary key and created/updated timestamps;
   `provider` (CharField max 24; gmail, google_workspace, outlook,
   microsoft365, yahoo, icloud, other); unique `address` (EmailField max 254);
   `imap_host` (CharField max 253); `imap_port` (PositiveIntegerField,
   default 993); `auth` (CharField max 16, app_password or xoauth2);
   `secret_env` (CharField max 128); `enabled` (BooleanField default false).
   Only an environment-variable name is stored, never a credential.
4. `SeedTest`: UUID primary key and created/updated timestamps; nullable
   `blast` FK (SET_NULL, related name seed_tests); `stream` FK (CASCADE,
   related name seed_tests); `content_hash` (CharField max 64, indexed);
   `token` (unique UUIDField, default uuid4); `status` (CharField max 10:
   sending, waiting, complete, timeout, error; default sending);
   nullable `sent_at` DateTimeField; `results` JSONField (default dict,
   blank). Provider resource IDs and submission recovery metadata live in
   that JSON alongside results, without adding a separate send table.
5. `ReputationCheck`: BigAutoField primary key; `subject` (CharField max 253,
   indexed); `source` (CharField max 24: spamhaus_dbl, surbl, uribl,
   safe_browsing); nullable BooleanField `listed`; `detail` JSONField
   (default dict, blank); indexed `checked_at` DateTimeField. Composite
   index `(subject, source, checked_at)` named `crm_reputation_history_idx`.
   Unanswered/refused checks remain unknown, and history is retained.

There are **no data migrations**, consent changes, mailbox enablement,
provider provisioning, campaign submissions or seed sends in this migration.
All new tables start empty; fields on existing rows are blank/null/defaults.
Reconfirmation uses an owner-started workflow and signed links in later code;
the timestamp alone cannot grant consent or bypass suppression.

## Existing graph and disposable test boundary

The 383 existing migrations are recorded in
`crm-deliverability-phase-3-existing-migrations.json`. They are the original
381 files plus the separately approved 0002 and 0003. In particular, 0003 has
SHA-256 `816786a2fe343bee10ab13a0b9a69d0978c6b9cf64ba7cc7c10a458b26fb3b49`.

The user approved this exact creation and disposable test scope on
5 November 2026. Created migration file SHA-256:
`0208ab2c000aeeb6a52b4a23e78a087dd1ccd8057a602f018c26ec31ab3ad181`.
The approval covers only the specified 0004 and testing this graph in
disposable local/CI databases containing synthetic data, including reversal
and reapplication of 0004. The test runner will pin every migration hash.
Production and connected staging migrations, deployment, provider writes,
real inbox access and live seed/campaign email sends are excluded. External
services and IMAP are mocked during these tests.

The repository's `AGENTS.md` requires explicit approval for each migration,
including migrations applied indirectly by database tests. Approval for
0002 or 0003 does not cover 0004.
