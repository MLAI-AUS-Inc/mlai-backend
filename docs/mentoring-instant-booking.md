# Personal Google calendars and instant mentoring bookings

This release adds a top-level mentoring Settings page and an explicitly opted-in instant-booking flow. Legacy requests retain their original acceptance and payment behavior. Existing profiles retain their saved duration, price, bio and hours. New profiles default to 15-minute sessions; instant policies start with 15-minute before and after buffers.

## Configuration and rollout

1. Review the locally approved and validated `mentoring/migrations/0003_instant_bookings.py` and `docs/migration-proposals/mentoring-0003-instant-bookings.md`. Production requires its own exact-plan approval before application.
2. Deploy the compatible schema and application with `MENTOR_INSTANT_BOOKING_ENABLED=false` and `MENTOR_CALENDAR_GOOGLE_ENABLED=false`.
3. Configure the Google web OAuth application with the exact `MENTOR_CALENDAR_GOOGLE_REDIRECT_URI`. Set its client ID and secret in `MENTOR_CALENDAR_GOOGLE_CLIENT_ID` and `MENTOR_CALENDAR_GOOGLE_CLIENT_SECRET`. Register the callback `/api/v1/community-chat/mentor/calendar-connections/google/callback/`. Enable the Calendar API and configure production consent/verification for the requested calendar scopes. No Gmail credentials are borrowed.
4. Configure the production connector credential encryption keyring and shared Redis cache. Calendar configuration fails closed without the required server configuration.
5. Enable `MENTOR_CALENDAR_GOOGLE_ENABLED`. An approved mentor opens Settings, connects their Google account, selects calendars to check and an owned writable calendar for bookings, and saves their availability. The destination is always checked for conflicts.
6. Run `python manage.py process_mentor_bookings` through the bounded scheduler. Verify the scheduler is healthy before enabling new booking admission. The scheduler recovers provisioning and cleanup work even when instant admission is subsequently disabled.
7. Enable `MENTOR_INSTANT_BOOKING_ENABLED` for an attended pilot. The mentor explicitly turns on Accept bookings after calendar readiness passes. Test an actual Meet join using separate host and guest accounts before broad video bookings.

The existing `MENTOR_BOOKING_ENABLED` flag remains the parent mentoring flag. Do not disable it as a recovery shortcut for funded sessions. Web deployment and native app release are separate steps; old clients must open the current web app to use converted mentors.

## Booking and buffers

A 10:00–10:15 session with 15-minute buffers protects 09:45–10:30. Another equivalent session may start at 10:45. The full protected interval must fit inside the mentor's offered hours. External busy events are checked as raw intervals; internal mentoring reservations already include their frozen buffers.

The quote does not reserve time. Explicit booking submission locks the participating accounts, reserves time, debits Roo and creates durable provisioning work in one transaction. Provider calls occur outside database transactions. Confirmation requires a usable calendar event and, for video, a usable Meet link. Repeated submissions recover the same attempt. Failed or cancelled setup refunds the original purchased/earned allocation exactly once.

An unresolved provider result is reconciled by deterministic event identity. A timeout does not prove that the calendar event was absent. The financial resolution deadline is the earlier of 15 minutes after submission and the proposed session start. Local cancellation/refund can complete while provider cleanup remains pending; affected time stays quarantined until cleanup is resolved.

## Maintenance

Linked Google event moves and cancellations are reconciled by the scheduler;
see [calendar synchronization](mentoring-calendar-sync.md) for supported changes,
conflict handling and safe treatment of missing/inaccessible events.

Use My sessions for hosted and attended bookings. Existing pending requests remain visible as Legacy requests. Calendar disconnection pauses new booking availability but retains session management and payment history. Reconnect the same Google identity to repair cleanup of events on its original calendar. Existing accepted sessions retain their original terms.

Monitor due provisioning/cleanup work, oldest unresolved attempt, refund/payout state, scheduler failures, provider reconnect state and duplicate-event errors. Run bounded recovery after restoring provider access. Never mark an unknown provider outcome absent merely to clear a task.

## Account deletion

The existing privacy endpoint records a durable deletion request; it does not immediately delete the shared user. The eventual `shared_account_and_dependencies` executor must call `mentoring.account_teardown.prepare_account_deletion(user)` inside the same transaction as `user.delete()`. Funded sessions and unresolved provider cleanup require operator resolution before the cascade; settled dependency records can be pruned by the helper. Do not call this pruning helper when merely recording a deletion request. Ledger records retain their existing domain retention behavior.

## Initial scope

The first implementation supports personal Google Calendar connections, calendar selection, weekly availability, editable notice/horizon/increment/buffers and instant one-to-one bookings. Microsoft/iCloud, reusable event types, date overrides, advanced limits, richer forms and team scheduling remain subsequent work packages in the implementation plan. Calendar-only permissions do not import private events into the CRM.

## Validation

Database-free provider, scheduling and view suites run through `scripts/test_without_database.py`. The real booking/ledger/constraint suites require an explicitly approved disposable PostgreSQL database. All provider network calls are mocked in automated tests. A live Google OAuth/Meet pilot is a separate operational acceptance check, not established by mocked tests.

## Implementation validation on 5 November 2026

- 275 database-free mentoring/provider/HTTP tests passed.
- 24 real PostgreSQL booking/ledger tests passed, including same-slot and cross-product Roo spending races, exact purchased/earned refunds, late provider creation, cancellation, legacy expiry and lock-order coverage.
- The approved 0003 migration applied successfully to an empty disposable local PostgreSQL database alongside the existing migration graph. Model drift check reports no changes.
- Source review fixes were rechecked independently. No production data, OAuth credentials or live provider accounts were used in these tests.

The frontend companion passed 7,337 unit tests, desktop checks, TypeScript, its E2E build and focused browser regressions. The full repository `just ci` was attempted but stopped during dependency downloads as free local disk space dropped. A real Google OAuth/Meet pilot and production migration approval remain rollout prerequisites.


## Automatic transcription and Gemini notes

See [the capture contract and rollout runbook](mentoring-automatic-capture.md).
New explicit activation defaults both features on for video sessions. Existing
profiles and frozen bookings retain their policy until explicitly changed.
Meet settings consent and actual account eligibility are additional prerequisites;
Calendar connection alone does not grant them. New required-capture video bookings
confirm only after event/Meet creation and read-back verification of both settings.
