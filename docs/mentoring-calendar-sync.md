# Linked mentoring calendar synchronization

The scheduler now reconciles already linked, accepted mentoring sessions with
their original Google calendar. This extends live free/busy protection and the
existing event creation/cancellation worker. It does not import unrelated events.

Google event IDs are deterministic and bound to the session through private
extended properties. The worker reads the event twice around external checks,
then rechecks the local status, original target and connection revision under
the same participant locks as booking. Provider calls are outside transactions.
One-minute next-check scheduling, two-minute claims, batch limits and time budgets
bound work and allow recovery after process restarts. Actual latency also depends
on scheduler load and provider availability.

## Supported changes

- Moving a future event updates the session's time and both participants'
  reservations atomically. Frozen duration, price and ledger references stay the
  same. Google-side moves may be outside regular mentoring hours, but must pass
  external conflicts and PostgreSQL participant exclusions.
- Explicit Google cancellation tombstones use the existing cancellation/refund
  transaction before the session starts. Replays cannot charge or refund twice.
  Existing durable cleanup handles the app-to-Google direction.
- Changed Meet links require capture configuration again when the booking's
  frozen policy (or historical repair policy) requires it.
- Network/permission failures, 404s and ambiguous responses never imply a refund
  or release of booked time. All-day, recurring, duration-changing, past or
  conflicting edits require attention and retain the local booking. Participant
  views show the proposed Google time when it was safely parsed.
- Automatic completion/payout of linked sessions requires a recent successful
  sync; a stale local end time cannot cause payout after a Google move.

The worker requires a stored original calendar target and event ID. Historical
confirmed sessions lacking either must first use the scoped meeting repair
procedure in [the capture runbook](mentoring-automatic-capture.md). Repair checks
capture permission before creating an event. A mentor converted to automatic
booking can use the existing Retry button to prepare an unlinked old session's
repair. A known different organizer is never guessed or replaced.

No migration or new provider permission is required for reconciliation. State
lives in the existing meeting_provisioning JSON field. Capture still requires
the Google Meet settings consent. The owner connection response exposes a
capture_ready boolean; the frontend also displays OAuth callback failures.

## Validation and rollout

Run test_calendar_sync_unit and the existing calendar/service suites without a
database. Run test_calendar_sync_database plus booking suites on the explicitly
approved disposable PostgreSQL graph. Tests cover reservation moves, exactly-once
refunds, concurrent app cancellation, changing provider revisions and ambiguous
provider failures. All provider data is synthetic in automated tests.

Deploy backend first, then the companion participant sync messages. Verify the
scheduled runner and perform a separately authorized move/cancel pilot on a
designated test session. Do not move or cancel a real appointment for testing.

Compatibility review includes Monish's backend PR #1297 and Chat PR #656. Their
30–60 minute choices and new-mentor defaults do not rewrite existing sessions;
reconciliation respects each session's frozen duration, including Sam's existing
15-minute booking. The payout allowance fix remains owned by #1297.

Google references: [event identity and deletion semantics](https://developers.google.com/workspace/calendar/api/v3/reference/events)
and [event reads](https://developers.google.com/workspace/calendar/api/v3/reference/events/get).
