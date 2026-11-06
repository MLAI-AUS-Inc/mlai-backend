# Proposed mentoring 0003 instant booking schema

Generated and validated in an approved disposable local PostgreSQL database. Production application still requires its own exact-plan approval under AGENTS.md.

1. Alter MentorProfile.session_minutes default from 30 to15 (no existing row values overwritten).
2. Add MentorProfile.accepting_bookings=false; booking_mode='legacy_request' (retains instant mode after publication is paused); buffer_before_minutes=15; buffer_after_minutes=15; minimum_notice_minutes=240; booking_horizon_days=30; start_increment_minutes=15; booking_revision=1.
3. Add MentorSession.booking_mode='legacy_request'; before/after buffer snapshots=0; calendar_snapshot JSON={}; booking_snapshot JSON={}; failure_code=''. Add provisioning choice without changing historical statuses.
4. Create MentorBookingAttempt: UUID primary key, one-to-one PROTECT session, payload digest, state, hard deadline, generation, lease identity/expiry, next retry, provider_started, cleanup_pending, safe error, timestamps. Index(status,next_attempt_at).
5. Create MentorTimeReservation: PROTECT session and principal, starts_at,ends_at,active. Positive interval check; unique(session,principal); index(principal,active,starts_at).
6. Create personal MentorCalendarConnection as specified by calendar_models.py (Google account identity, encrypted tokens, scopes, status, conflict/destination calendar selection and revision; unique user/provider/subject).
7. Backfill MentorTimeReservation for existing future requested/accepted sessions. Host interval and founder interval use original meeting times, with no new buffers. Detect/report existing cross-role/host overlaps before adding exclusion; no paid sessions cancelled and no balance/profile values changed.
8. PostgreSQL only: CREATE EXTENSION IF NOT EXISTS btree_gist; add exclusion constraint on MentorTimeReservation: principal_id WITH =, tstzrange(starts_at,ends_at,'[)') WITH && WHERE active. Other database vendors retain portable checks; live booking writes require PostgreSQL.

Reversal removes new schema/constraints only; it must not be run after new bookings without an explicitly reviewed data-retention plan. Tests may apply these operations solely to an approved disposable database; production application is separate.

## Approved local validation

On 5 November 2026, the user approved migration generation and application with the existing migration graph in a disposable local PostgreSQL test database only. `mentoring/migrations/0003_instant_bookings.py` was generated and includes the backfill and PostgreSQL exclusion operations above.

SHA-256: `df55295a8285a7ccfb3602a40592a9431a83e147be9fb09a45dfc2172e577889`.

The local PostgreSQL test database successfully applied the graph, including `btree_gist` and `mentor_reservation_no_overlap`; model drift check reports no changes. Final expanded validation passed 24 real-row booking/ledger tests. The disposable test state contains synthetic accounts and a mocked provider only. This approval does not cover production application.
