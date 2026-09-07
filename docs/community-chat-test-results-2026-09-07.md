# Community Chat backend regression evidence

This records local synthetic tests, not deployment or live Slack throughput.
Python 3.11.14 uses both repository requirements files. The isolated loopback
cluster runs PostgreSQL 18.6 and pgvector 0.8.2. Each run creates a fresh database
and removes it in `finally`; settings never load the ordinary `.env` file.

The user approved the exact 301 existing migrations in
[slack-chat-test-proposal.md](slack-chat-test-proposal.md). The runner compares
its computed dependency closure with the proposal before applying migrations.
No migration source files changed.

## Results

- All 10 account-profile API/concurrency tests passed.
- The first broader run executed 320 Slack, Volunteer, session and privacy
  tests: nine failures and 14 errors. Twelve errors were missing test-schema
  prerequisites. The remaining failures/errors were investigated below.
- After the fixes, all 167 tests in the affected suites passed, including real
  PostgreSQL consent/disconnect races. Each test database was removed.
- The full 320-test selection has not yet passed. The additional prerequisites
  are listed in [the complete test proposal](community-chat-full-test-proposal.md)
  for a 334-migration setup, including pull-request CI.

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
migration setup is still awaiting approval before that CI can be triggered.

Client commit `346c43d70fe6444a074280a323576bddf56065e8` has successful hosted
[CI](https://github.com/MLAI-AUS-Inc/mlai-chat/actions/runs/34120233229) and
[Docker builds](https://github.com/MLAI-AUS-Inc/mlai-chat/actions/runs/34120233385).
