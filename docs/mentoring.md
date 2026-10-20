# Vibe Mentoring (pilot)

Status: pilot, built on the branch `mentoring/pilot`. Not in `main`.
The initial migration `mentoring/migrations/0001_initial.py` is proposed and ready for
review (field list and checksum in `docs/migration-proposals/mentoring-0001.md`). It has not
been applied to any shared database.

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
- Each mentor sets one location, "MLAI space" or "Video"; the founder does not
  choose. For video there is no mentor-supplied link: when the mentor accepts, MLAI
  creates a fresh Google Meet link for that session (see "Google Meet and free/busy"
  below). `meet_link` is shown only to the founder and the mentor, and only once the
  session is accepted (or completed).
- Mentors can set away dates: a list of inclusive local date ranges (`away_dates` on
  the profile, up to 20) with no bookings. Existing accepted sessions are not
  cancelled automatically; the mentor cancels them.
- Availability windows are wall-clock times in the mentor's timezone; slots advance
  in real time, so daylight saving gaps are skipped and a repeated hour is offered twice.
- Price defaults to 40 Roo Points and is stored per mentor. The price is
  snapshotted on each session.
- Each request carries a brief: stage, biggest struggle, outcome wanted, and an
  optional link.
- A request charges nothing. Points are taken when the mentor accepts.
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
| GET `mentors/<id>/availability/?from=&to=` | Free slots (UTC ISO times), up to 31 days, default next 14, plus `busy_check` (`ok`, `failed` or `off`) |
| GET, PUT `mentor/profile/` | The mentor's own profile, weekly `availability` list and optional `busy_calendar_email` |
| POST `mentor/sessions/` | Founder request: `mentor_id` (whole number), `starts_at`, `brief`, `client_request_id`, and `expected_price` (required, the whole-number price the founder was shown) |
| GET `mentor/sessions/?role=founder\|mentor&status=` | My sessions |
| POST `mentor/application/check/` | Invited user checks an invite `{token}`: `state` ready, submitted or approved. 404 `invite_invalid`, 403 `invite_wrong_user`, 410 `invite_expired` or `invite_revoked` |
| POST `mentor/application/` | Invited user submits the profile form with `token`; creates or updates their profile with `is_active` false. 409 `invite_used`, 409 `already_a_mentor` |
| POST `mentor/sessions/<uuid>/accept/` | Mentor accepts; the founder is charged now (409 `founder_insufficient_points` and the request stays open if they cannot pay) |
| POST `mentor/sessions/<uuid>/meeting/retry/` | Mentor asks for the Meet link again when `meeting_status` is `failed` or `unavailable` (409 `meeting_pending` while one is being made) |
| POST `mentor/sessions/<uuid>/decline/` | Mentor declines (no points move) |
| POST `mentor/sessions/<uuid>/cancel/` | Founder or mentor, only before the start time (refund if charged); 409 `session_started` once it has started |
| POST `mentor/sessions/<uuid>/complete/` | Mentor marks done, only once the session has ended (payout) |

A missing or non-integer `expected_price` returns 400 `expected_price_required`; a
mismatch with the mentor's current price returns 400 `price_changed`. A boolean or
non-integer `mentor_id` returns 400 `invalid_mentor_id`. When the flag is off the
flag is checked before authentication and throttling, so every caller gets 404.
If an action hits a request that has just expired, the expiry is committed
first and the call then returns 409 `request_expired`.

Creating a request is idempotent per founder through `client_request_id`. A
repeat with the same id and same mentor and time returns the original session
with 200 (even if the mentor was deactivated or the price changed in between); a
different mentor or time returns 409 `idempotency_conflict`. A
Postgres advisory locks (per mentor, per founder and per request id) serialise
writers, like `MeetingRoomService`, so the two-founders-one-slot and
one-founder-two-mentors races are closed.

Important: overlap protection relies on those Postgres advisory locks plus a slot
recheck inside the transaction. Production and staging are Postgres. On any other
database (for example SQLite in a local run) the locks are no-ops, so double
bookings are not prevented there. There is no database constraint yet; see the open
items in the migration proposal.

The home endpoint exposes `feature_flags.mentor_booking`.

## Points flow

All movements use `PointsService`, ledger source `MANUAL`, reference type
`MENTOR_SESSION`, so no `roo` migration is needed.

1. Request: nothing is charged. The service only checks the founder's available
   balance and returns 409 `insufficient_points` early if it is below the price.
2. Accept: the price snapshot is spent from the founder, key
   `mentor_charge:{session id}`. If the founder's balance is now too low the accept
   fails with 409 `founder_insufficient_points` and the session stays `requested`.
3. Decline and expiry: no points move, because nothing was charged.
4. Cancel (founder or mentor), only before the session start time; from the start
   on it is refused with 409 `session_started`. If the founder was charged, refund with key
   `mentor_release:{session id}`, restoring the purchased and earned split. Cancelling
   a request that was never accepted moves no points.
5. Completion (mentor marks done after the session ends, or automatically 24 hours
   after the end): the price is awarded to the mentor as plain Roo points through
   `award()`, key `mentor_payout:{session id}`.

Known side effect: `PointsService.award` raises the mentor's lifetime earned
total, which also gates the Conference Room and committee eligibility. Maintainers
decided this is fine: the payout is plain Roo points, with no special earned handling.

Deleting a user deletes their mentor profile and sessions (cascade) so account deletion
is never blocked; ledger rows are separate and stay. If a charged, not yet completed
session is removed that way, the other party is not refunded automatically, so handle
those sessions (cancel them first) as part of account deletion.

## Notifications

A new request sends the mentor a Slack DM through `SlackService.send_dm`, best
effort, after the booking transaction commits (via `transaction.on_commit`; on staging,
where requests are atomic, that is when the request commits). The DM runs in the
request thread, so a slow Slack call delays the response by up to its timeout. It is
not queued, only when the mentor has a Slack id. The outcome is stored in
`notification_status` (pending, sent, failed, skipped), never in `status`, so a
failed DM cannot change a booking. Mentors can also poll
`mentor/sessions/?role=mentor&status=requested`.

## Google Meet and free/busy

Code: `mentoring/google.py`. One MLAI-owned free Google account (consumer Gmail, not
Workspace) is the organiser. Mentor and mentee Google accounts are never used to sign
in. Plain HTTPS through `requests`; no new dependency. Everything is off unless
`MENTORING_GOOGLE_ENABLED` is true and the three credentials are set, and it is always
unavailable on isolated staging (capability `google`, never called).

- Accept: after the charge commits (`transaction.on_commit`, best effort like the Slack
  DM) a fresh Meet link is made for every video session. Primary path: Meet REST API
  `POST https://meet.googleapis.com/v2/spaces` with `accessType: OPEN`, then a Calendar
  event on the MLAI account's primary calendar inviting the mentor and the founder
  (`sendUpdates=all`), the link in location and description. If the Meet call fails with a
  permission or feature error, fallback: a Calendar event with `conferenceData.createRequest`
  (`hangoutsMeet`, requestId = session id), reading `hangoutLink` and polling once if pending.
  The path used is stored in `meeting_path` (`meet_api` or `calendar`).
- Session fields: `meet_link`, `calendar_event_id`, `meeting_status` (`pending`, `created`,
  `failed`, `unavailable`), `meeting_error` (short code such as `permission`, `network`,
  `google_off`). A failure never undoes the acceptance. The mentor can retry with
  `POST mentor/sessions/<id>/meeting/retry/`. Each attempt stamps `meeting_claimed_at`; a
  `pending` claim younger than 2 minutes (`MEETING_CLAIM_TIMEOUT_SECONDS`) still answers 409
  `meeting_pending`, an older one (the process died after the commit) is reclaimed by the
  retry. A retry never charges again. The session JSON reports a stale `pending` as `failed` (no write on read) so clients offer Retry. The Calendar event id is fixed per session
  (`mlaimentor` + the session UUID hex) and stored before the insert, so a retry reuses an
  existing event instead of inserting a second, and a cancel can always delete it. With Google off or on staging the status is
  `unavailable`.
- Cancel after a meeting exists: the Calendar event is deleted (best effort; guests are told).
- Slots: when the mentor has `busy_calendar_email` and Google is enabled, the slot list
  drops times the mentor is busy, from the Calendar `freeBusy` call made by the MLAI
  account. Answers are cached per mentor and window for 5 minutes. If the call fails the
  slots are still shown and `busy_check` is `failed` (`off` when not used). The booking
  re-check inside the transaction does not call Google, so a mentor who is busy can still
  decline a request.
- Responses: `meet_link` and `meeting_status` appear only for video sessions that are
  accepted or completed, and sessions are only ever returned to their two participants.
  `busy_calendar_email` is returned only on the mentor's own profile.

## Google setup (for maintainers)

1. Create a free Google account just for MLAI mentoring (a normal Gmail address, not
   Workspace). Use a shared mailbox the team controls, and turn on two-step sign-in.
2. In Google Cloud Console (signed in as that account) create a project. Enable two APIs:
   "Google Calendar API" and "Google Meet REST API". Set up the OAuth consent screen
   (External; add the MLAI account as a test user, and publish the app to Production so the
   refresh token does not expire after seven days).
3. Create an OAuth client (type "Desktop app" or "Web application"). This gives a client
   id and a client secret.
4. Do a one-time sign-in as the MLAI account to grant these scopes:
   `https://www.googleapis.com/auth/calendar.events` (create and delete events),
   `https://www.googleapis.com/auth/calendar.freebusy` (busy times) and
   `https://www.googleapis.com/auth/meetings.space.created` (create Meet spaces).
   Use Google's OAuth consent flow (for example the OAuth 2.0 Playground with your own
   client id and secret, or a local one-off sign-in). Ask for offline access so Google returns a
   refresh token, and copy only the refresh token. This repo deliberately has no script that
   stores secrets.
5. Put the three values in the backend environment, never in git or docs:
   `MENTORING_GOOGLE_CLIENT_ID`, `MENTORING_GOOGLE_CLIENT_SECRET`,
   `MENTORING_GOOGLE_REFRESH_TOKEN`, then set `MENTORING_GOOGLE_ENABLED=true`. They go in the
   secrets store; until then keep them only in the host env file.
6. Mentor step, "Share free/busy with <MLAI account>": in Google Calendar on the web, open
   Settings, pick the calendar, "Share with specific people or groups", add the MLAI
   account's address with the permission "See only free/busy (hide details)". Then the mentor
   saves that calendar's email address as `busy_calendar_email` on their mentor profile. The
   mentor never signs in to MLAI's Google.

Open questions that need a live test before the pilot relies on them:

- Can a free consumer account call Meet `spaces.create` with `accessType: OPEN`? If not, the
  Calendar `conferenceData` fallback is used automatically; check `meeting_path` on a test
  session to see which path was taken.
- Can guests join the call when the organiser (the MLAI account) is not present? This decides
  whether sessions can start with only the mentor and founder.

## Rollout order

1. The migration `mentoring/migrations/0001_initial.py` is generated and ready for
   review. Review it against the proposal.
2. Run the database-backed tests (not yet written) on a disposable database.
3. Apply the migration before or with the code (the app's tables must exist before
   any user deletion or admin page touches them). The deployer sets
   `APPROVED_MIGRATION_PLAN_SHA256` on the host (see the proposal for the exact value
   and how to recompute it), then merge and deploy (the flag is off, so nothing
   changes for users).
4. In Django admin create a `MentorProfile` per mentor and tick active; mentors
   fill in availability through the API or the chat client (and, if they want busy times hidden, share free/busy with the MLAI account and set `busy_calendar_email`).
5. Set `MENTOR_BOOKING_ENABLED=true` in the host env and restart.
6. Release the chat clients that read `feature_flags.mentor_booking`.

## Settings

`MENTOR_BOOKING_ENABLED` (false), `MENTOR_REQUEST_TTL_HOURS` (48),
`MENTOR_AUTO_COMPLETE_HOURS` (24), `MENTORING_GOOGLE_ENABLED` (false),
`MENTORING_GOOGLE_CLIENT_ID`, `MENTORING_GOOGLE_CLIENT_SECRET`, `MENTORING_GOOGLE_REFRESH_TOKEN`
(all empty, env only), `COMMUNITY_CHAT_MENTORING_RATE` (60/minute),
`COMMUNITY_CHAT_MENTORING_WRITE_RATE` (20/minute).

## Tests

No-database suites, registered in `.github/workflows/deploy.yml`:
`python scripts/test_without_database.py mentoring.tests.test_scheduling_unit mentoring.tests.test_google_unit mentoring.tests.test_service_unit mentoring.tests.test_dst_unit community_chat.tests.test_mentoring_views_unit`.
Locking, constraints and real ledger rows need database tests after migration approval.

## Becoming a mentor (invite only)

Nobody can apply without an invite. Staff add a Mentor invite in Django admin for an
existing user; the single-use link (`<chat url>/#/mentoring/apply?invite=<token>`) is
shown once and only its SHA-256 is stored. It expires after 14 days by default and can
be revoked from the list. The invited user, signed in as that user, fills the form
(headline, bio, topics, price 1 to 1000 Roo default 40, session length, location,
weekly availability, optional booking questions). Submitting creates or updates their
profile with `is_active` false and uses up the invite. MLAI staff approve by ticking
`is_active` on the Mentor profile in admin. Until then the applicant sees "submitted,
MLAI will review" and no Mentor hub. An applicant cannot edit after submitting; staff
issue a new invite if they need to resubmit.

## Booking questions

Each mentor can set up to 8 questions (text up to 200 characters, required or
optional, answers up to 1000 characters) in Mentor settings (`brief_questions` on the
profile). An empty list means the default four (stage, struggle, outcome, optional
https link). The founder sends `brief.answers` as `[{id, answer}]` (the old
`{stage, struggle, outcome, link}` shape still works for the default questions). The
question text is copied onto the session with the answers (`brief_answers`), so later
edits never change past bookings; older sessions are shown from their four old fields.
