# Roo avatar migration proposal — 2 November 2026

Status: the user explicitly approved this exact migration and disposable local
regression databases on 2 November 2026. Approval includes the inventoried 377
existing migrations plus this new migration, synthetic data, and PostgreSQL
concurrency tests. Production/shared database changes and deployment are not
authorized by this approval.

## Exact proposed migration

Create `community_chat/migrations/0016_roo_avatar.py`, depending on
`community_chat.0015_token_insights` and Django's swappable dependency on
`settings.AUTH_USER_MODEL` (`core.User`). Its sole operation is `CreateModel`
for `CommunityRooAvatar`, table `community_chat_communityrooavatar`.

| Field | Schema |
| --- | --- |
| `id` | UUID primary key, Python `uuid.uuid4` default, not editable |
| `user_id` | Non-null FK to canonical `core.User`; ORM `CASCADE`; reverse name `roo_avatars`; Django FK index |
| `community` | Non-null varchar(255), trusted deployment community identifier |
| `hair` | Nullable/blank varchar(32), five fixed choices below |
| `glasses` | Nullable/blank varchar(32), five fixed choices below |
| `jacket` | Nullable/blank varchar(32), five fixed choices below |
| `shoes` | Nullable/blank varchar(32), five fixed choices below |
| `version` | Non-null positive integer, Python default 1 |
| `created_at` | Non-null datetime, `auto_now_add=True` |
| `updated_at` | Non-null datetime, `auto_now=True` |

Constraints:

- `roo_avatar_account_community`: unique (`user_id`, `community`).
- `roo_avatar_version_positive`: check `version >= 1` (in addition to the
  backend's normal PositiveIntegerField constraint).
- No further explicit indexes. No change to an existing table, data migration,
  historical reconciliation, backfill, XP award, spend or points ledger entry.

The migration freezes the following model choices (ID → label):

- hair: `honey_tuft` → Honey tuft; `cocoa_curls` → Cocoa curls;
  `peach_fringe` → Peach fringe; `lilac_bob` → Lilac bob;
  `mint_mohawk` → Mint mohawk.
- glasses: `classic_black` → Classic black; `honey_rounds` → Honey rounds;
  `rose_hearts` → Rose hearts; `ocean_visor` → Ocean visor;
  `lilac_stars` → Lilac stars.
- jacket: `sage_bomber` → Sage bomber; `blue_denim` → Blue denim;
  `berry_varsity` → Berry varsity; `sunflower_raincoat` → Sunflower raincoat;
  `lilac_puffer` → Lilac puffer.
- shoes: `red_sneakers` → Red sneakers; `cream_high_tops` → Cream high tops;
  `bush_boots` → Bush boots; `pink_roller_skates` → Pink roller skates;
  `teal_runners` → Teal runners.

Choice eligibility remains application policy, not persistent rank or entitlement
columns. Level corrections suppress unavailable saved choices in the effective
outfit while retaining the stored preference. The FK cascades outfits if the canonical user is physically deleted. The
existing deletion-request workflow is unchanged.

## Requested approval boundary

Approval is needed to create **this exact new migration**, then construct an
isolated, disposable local test database using the repository's existing
migration graph plus `community_chat.0016_roo_avatar`, and run the Roo avatar,
Volunteer rank and relevant account/session regression suites against synthetic
data only. The PostgreSQL concurrency regression needs a disposable PostgreSQL
database; SQLite alone cannot verify row-lock serialization.

The prerequisite graph is recorded in
[`roo-avatar-migration-inventory.json`](roo-avatar-migration-inventory.json)
by `scripts/test_without_database.py --describe-migrations`. That command reads
migration files and hashes with database/network access disabled. The inventory
intentionally records the 377 existing migrations at approval time. The new
approved file `community_chat/migrations/0016_roo_avatar.py` has SHA-256
`0722d2f03e062f21964c42db5082e8bec81a0aed6b0182d6f7472c6117e4fe64`. Review the complete graph
before approving test-database construction: Django's test runner may apply all
installed-app migrations, not just the new table.

This approval would not authorize a persistent/shared database migration,
production credentials or data, deployment, production workflow execution, or a
production application restart. Deployment requires the new schema before this
endpoint can be used. Reversing the migration drops saved outfits and therefore
is not part of the requested testing authorization outside disposable databases.

## Completed local validation

The approved migration and full existing graph applied successfully in a new
socket-only PostgreSQL 17.11 cluster. All 128 targeted tests passed, including
two-device first-save concurrency. The test database and temporary cluster were
removed. System checks passed and the migration dry-run reported no drift.
See [the test evidence](roo-avatar-test-results-2026-11-02.md).
