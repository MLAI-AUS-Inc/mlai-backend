# Volunteer schema and test migration approval proposal

Prepared 5 September 2026. Creation and disposable database execution of
`0009_volunteer` were subsequently approved; the generated migration contains
the seven models below and no existing-table alterations or data migration.

Proposed migration: `community_chat.0009_volunteer`. Inspected leaf migrations
are `community_chat.0008_token_usage` and
`roo.0036_sanitize_coworking_operation_receipts`. Dependencies will include both
and Django's swappable dependency on `AUTH_USER_MODEL` (`core.User`).

The migration creates seven additive, community-scoped models:

| Model | Fields and purpose |
| --- | --- |
| VolunteerProject | UUID, configured community key, public title/brief/purpose, guide user FK, canonical public thread JSON, publication flag, audit timestamps |
| VolunteerOpportunity | UUID, community key, nullable project FK, canonical event ID, title/purpose/description/learning, action and kind, guide/reviewer user FKs, canonical public thread JSON, start/end timestamps, exact integer microroo reward range, recommended level, availability, audience, optimistic version, audit timestamps |
| VolunteerMemberState | UUID, community+user, nullable audited historical contribution opening microroo and reconciliation cutoff, reviewer/reason/timestamps; serialisation row for per-member caps and awards |
| VolunteerRecognition | UUID, community+user, stable canonical outcome key, action, source JSON, optional opportunity, occurrence time, immutable agreed policy snapshot, private evidence/note, status/version, reward microroo, linked existing Roo Ledger, append-only private decision JSON, audit timestamps |
| VolunteerMilestone | UUID, community+user, stable level key, reached time, linked existing Roo Ledger, acknowledgement time |
| VolunteerAttendance | UUID, community+user+canonical event, checked-in timestamp, trusted source, authorised verifier/reason, append-only correction JSON and audit timestamps |
| VolunteerSourceReceipt | UUID, community+canonical source key, origin, actor user FK and optional target user FK, canonical channel/thread/source JSON, occurrence time, type, durable processing status/error, optional recognition FK and audit timestamps |

Constraints: community+user uniqueness for member state; community+user+outcome
for recognition; community+user+stable level key for milestones (no policy
version in this key); community+user+event for attendance; community+source key
for source receipts; conditional community+event uniqueness for event volunteer
opportunities. Reward/opening amounts must be nonnegative, and maximum reward
must be at least minimum reward. Use protected user/ledger relationships for
audited financial records. Opportunity/source snapshots survive optional
project/opportunity removal.

Indexes: community+availability+event on opportunities; community+publication
on projects; community+user+status+created date and reviewer+status on
recognition where applicable; community+source kind+processing state and
actor+occurrence date on receipts. Foreign-key indexes use Django defaults.
Private evidence and source messages are not copied into a public search index.

No existing Roo account, ledger or task-assignment fields change. There are no
data migration operations, resets, automatic backfills or wallet credits.
Existing members with ambiguous historical awards keep their wallet intact;
their journey reports `history_reconciled: false` and unavailable totals until
an authorised reconciliation records an opening. A new member can initialise
to zero only when there are no historical qualifying/ambiguous ledger earnings.
New awards and milestone bonuses remain disabled through separate server flags.

Requested approval scope: **create this one additive migration and execute it,
with its existing dependency chain, only in a disposable database owned
by the targeted test runner**. PostgreSQL is used to exercise real row locks.
No ordinary local database or production
database migration, service startup, deployment, operational data correction or
historical bonus backfill is included. Test database contents are synthetic.
The dependency chain can include existing data migrations when constructing the
empty test database; none executes against a developer or production database.

Before approval, permitted work includes model/service/API code, pure Python
policy tests, source compilation, and Django checks that do not apply or create
migrations. The migration file and database-backed tests wait for explicit
approval under this repository's `AGENTS.md`.

## Additional existing prerequisites for current-model integration tests

The initial approved migration and its 68 existing dependency migrations
successfully applied to a newly created PostgreSQL database, which was removed
after the run. The generated swappable user dependency resolves to an old user
schema; the current user model then failed before any test scenario because
`core_user.community_chat_profile_id` was absent. Current company models also
require their existing migration chain. This is a test setup dependency issue,
not a change to the seven-table Volunteer schema.

Additional proposed test-only migration targets are:

- `core.0056_user_community_chat_profile_id` (current User fields).
- `founder_tools.0010_company_default_audience_visibility` (current startup
  company model, including organisation links and verified business fields).
- `organizations.0002_organization_company_linkedin_url` (current organisation
  fields used when constructing synthetic companies).

Together these targets add exactly 77 existing migrations beyond the initial
approved closure: admin 0001–0003; core 0011 through 0056 including existing
branches/merges; founder_tools 0001–0010 including both 0008 branches;
integrations 0001–0011; organizations 0001–0002; vibe_raising 0001–0002; and
workflow_runs 0001. The plan is calculated from Django's existing migration
graph, rather than running all installed applications' migrations. Their
existing seed/data operations would run only in the new empty disposable
database. No existing migration file would change. The runner clears process
environment, disables dotenv loading, uses synthetic settings and loopback
credentials, refuses a pre-existing test database, and removes only the
database created by that invocation. This additional execution scope requires
approval before use.
