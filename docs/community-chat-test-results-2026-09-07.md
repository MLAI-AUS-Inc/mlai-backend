# Community Chat backend regression evidence

This records local synthetic tests, not deployment or live Slack throughput.
Python 3.11.14 uses both repository requirements files. The isolated loopback
cluster runs PostgreSQL 18.6 and pgvector 0.8.2. Each run creates a fresh database
and removes it in `finally`; settings never load the ordinary `.env` file.

The user approved the exact 301 existing migrations in
[slack-chat-test-proposal.md](slack-chat-test-proposal.md), then explicitly
approved the expanded [334-migration setup](community-chat-full-test-proposal.md)
on 2026-09-07 for disposable local tests and pull-request CI. The runner compares
its computed dependency closure with the proposal before applying migrations.
No migration source files changed.

## Results

- All 10 account-profile API/concurrency tests passed.
- The first broader run executed 320 Slack, Volunteer, session and privacy
  tests: nine failures and 14 errors. Twelve errors were missing test-schema
  prerequisites. The remaining failures/errors were investigated below.
- After the fixes, all 167 tests in the affected suites passed, including real
  PostgreSQL consent/disconnect races. Each test database was removed.
- The complete selection passed: **334 tests**, including all account-profile,
  Slack import/catalogue/deletion, consent and device races, connector authority,
  account sessions, device auth, Volunteer and Roo reward regressions. The runner
  verified all **334 approved migrations** before applying them.
- The final run removed its disposable database, and the dedicated PostgreSQL
  cluster was stopped. No ordinary local or production database was touched.

## Corrections

Volunteer input serializers now return structured validation errors for unknown
fields or a non-object payload, rather than crashing while constructing a 400
response. Conversation-receipt filtering now preserves records with absent
optional JSON flags on PostgreSQL while excluding invalidated and service-account
records; pagination checks cover both retained and hidden rows.

Slack fixtures now mock the owner's `users.conversations` discovery path and
expect the bounded 200-message reply pages. The ordered-delivery test uses
timestamps inside the current history window, rather than aging fixed dates.
The ambiguous-registration test explicitly pauses the grant before testing a
new consent generation; the separate active-upgrade test preserves the existing
generation and registrations. Privacy assertions were retained. A Roo fixture
now expects the existing canonical lowercase placeholder email.

The workflow registers the new Slack/Volunteer tests and PostgreSQL concurrency
cases so these regressions are exercised by future PR checks. The expanded
migration setup is approved for those checks; hosted CI is the next verification
step.

Client commit `346c43d70fe6444a074280a323576bddf56065e8` has successful hosted
[CI](https://github.com/MLAI-AUS-Inc/mlai-chat/actions/runs/34120233229) and
[Docker builds](https://github.com/MLAI-AUS-Inc/mlai-chat/actions/runs/34120233385).

## CI follow-up, 2026-09-08

Hosted run [34127964599](https://github.com/MLAI-AUS-Inc/mlai-backend/actions/runs/34127964599)
passed the migration round-trip job and all 106 tests across its PostgreSQL
search, meeting-room and privacy steps. Its 1,889-test SQLite job found four
errors in two test areas; no migration or production-code change is needed.

Two historical-bonus history tests exercise PostgreSQL JSON containment, which
SQLite does not support. They now declare that database feature requirement,
and the entire historical-bonus test class is included in the PostgreSQL CI
job so their permission, reviewer and pagination assertions still execute.

Two legacy Slack webhook fixtures omitted event-recipient authorization and
expected the shared bridge to receive private events. They now include the
recipient/workspace envelope and verify that private channels, group DMs and
1:1 DMs go through owner import discovery without creating shared deliveries,
even when a legacy shared-channel mapping exists. The production privacy
boundary is unchanged.

The corrected selection passed **351 tests on PostgreSQL** with the exact
approved 334-migration closure. A separate isolated, in-memory SQLite run of
the affected classes passed 24 tests and skipped the two unsupported JSON
containment cases (26 total). Both databases were removed/closed, and the
PostgreSQL cluster was stopped. Hosted CI is rerun for this correction.
