# Vibe Mentoring (pilot)

Status: pilot, built on the private branch `mentoring/pilot`. Not in `main`.
Needs the migration in `docs/migration-proposals/mentoring-0001.md` approved
and applied before it can run anywhere with a database.

## What it is

Founders in MLAI Chat can book a paid session with a mentor. The founder sends
a short brief, the mentor accepts or declines, and Roo Points move through the
existing points ledger. New code lives in the top-level app `mentoring`; the
HTTP layer is `community_chat/mentoring_views.py`.

## Rules

- A mentor is a user with an active `MentorProfile`. Only an admin can set
  `is_active` (Django admin, "Vibe Mentoring"). Mentors cannot activate
  themselves. An admin can also change a mentor's price.
- Mentors set their own weekly availability (windows per weekday in their own
  timezone), their own session length (15 to 120 minutes, multiples of 5), their
  price, bio, topics and location.
- Location is "MLAI space" or "Video" (or "either", then the founder picks per
  request). For video the mentor supplies their own https link. There is no
  Google Calendar or Meet integration. The link is shown to the founder only
  once the session is accepted.
- Mentors can set away dates: a list of inclusive local date ranges (`away_dates` on
  the profile, up to 20) with no bookings. Existing accepted sessions are not
  cancelled automatically; the mentor cancels them.
- Availability windows are wall-clock times in the mentor's timezone; slots advance
  in real time, so daylight saving gaps are skipped and a repeated hour is offered twice.
- Price defaults to 40 Roo Points and is stored per mentor. The price is
  snapshotted on each session.
- Each request carries a brief: stage, biggest struggle, outcome wanted, and an
  optional link.
- A request expires after 48 hours unanswered, or at the session start time if
  that is sooner. Expiry and auto-completion are applied lazily whenever
  sessions or availability are read. There is no worker.
- No ratings, strikes, testimonials or report button.

## Endpoints

All under `/api/v1/community-chat/`, using `CommunityChatAccountAuthentication`.
Responses are `Cache-Control: private, no-store`. Errors are
`{"code": "...", "detail": "..."}` (409 for business conflicts). Reads use
throttle scope `community_chat_mentoring`, writes `community_chat_mentoring_write`.
Everything returns 404 `mentor_booking_disabled` while the flag is off.

| Method and path | Purpose |
| --- | --- |
| GET `mentors/` | List active mentors (no email or Slack id) |
| GET `mentors/<id>/availability/?from=&to=` | Free slots (UTC ISO times), up to 31 days, default next 14 |
| GET, PUT `mentor/profile/` | The mentor's own profile and weekly `availability` list |
| POST `mentor/sessions/` | Founder request: `mentor_id`, `starts_at`, `location_type`, `brief`, `client_request_id`, optional `expected_price` |
| GET `mentor/sessions/?role=founder\|mentor&status=` | My sessions |
| POST `mentor/sessions/<uuid>/accept/` | Mentor accepts |
| POST `mentor/sessions/<uuid>/decline/` | Mentor declines (refund) |
| POST `mentor/sessions/<uuid>/cancel/` | Founder or mentor, before the start (refund) |
| POST `mentor/sessions/<uuid>/complete/` | Mentor marks done, after the start (payout) |

Creating a request is idempotent per founder through `client_request_id`. A
repeat with the same id and same mentor and time returns the original session
with 200; a different mentor or time returns 409 `idempotency_conflict`. A
Postgres advisory lock per mentor serialises writers, like `MeetingRoomService`.

The home endpoint exposes `feature_flags.mentor_booking`.

## Points flow

All movements use `PointsService`, ledger source `MANUAL`, reference type
`MENTOR_SESSION`, so no `roo` migration is needed.

1. Request: spend the price from the founder, key `mentor_hold:{session id}`,
   in the same transaction that creates the session. Not enough points gives
   409 `insufficient_points` and nothing is created.
2. Decline, cancel or expiry: refund to the founder, key
   `mentor_release:{session id}`, restoring the purchased and earned split.
3. Completion (mentor marks done, or automatically 24 hours after the session
   end): award the price to the mentor, key `mentor_payout:{session id}`.

Known side effect: `PointsService.award` raises the mentor's lifetime earned
total, which also gates the Conference Room and committee eligibility. This
is accepted for the pilot and is an open decision for the operator.

## Notifications

A new request sends the mentor a Slack DM through `SlackService.send_dm`, best
effort, only when the mentor has a Slack id. The outcome is stored in
`notification_status` (pending, sent, failed, skipped), never in `status`, so a
failed DM cannot change a booking. Mentors can also poll
`mentor/sessions/?role=mentor&status=requested`.

## Rollout order

1. Operator approves the migration; generate `mentoring/migrations/0001_initial.py`
   from the models and review it against the proposal.
2. Run the database-backed tests (not yet written) on a disposable database.
3. Operator sets `APPROVED_MIGRATION_PLAN_SHA256` on the host, then merge and
   deploy the backend (the flag is off, so nothing changes for users).
4. In Django admin create a `MentorProfile` per mentor and tick active; mentors
   fill in availability and video link through the API or the chat client.
5. Set `MENTOR_BOOKING_ENABLED=true` in the host env and restart.
6. Release the chat clients that read `feature_flags.mentor_booking`.

## Settings

`MENTOR_BOOKING_ENABLED` (false), `MENTOR_REQUEST_TTL_HOURS` (48),
`MENTOR_AUTO_COMPLETE_HOURS` (24), `COMMUNITY_CHAT_MENTORING_RATE` (60/minute),
`COMMUNITY_CHAT_MENTORING_WRITE_RATE` (20/minute).

## Tests

No-database suites, registered in `.github/workflows/deploy.yml`:
`python scripts/test_without_database.py mentoring.tests.test_scheduling_unit mentoring.tests.test_service_unit mentoring.tests.test_dst_unit community_chat.tests.test_mentoring_views_unit`.
Locking, constraints and real ledger rows need database tests after migration approval.
