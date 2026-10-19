# Vibe Mentoring: staging rollout checklist

For the staging deployer. Source: branch `mentoring/staging` (= `staging` plus the
mentoring commits). The auto-deploy timer will NOT ship it: the change touches
`mlai/settings.py` (an OPERATIONAL path) and adds a migration, and
`ops/staging-chat/auto-deploy.py` refuses both. This is the separate reviewed
manual rollout described in `docs/backend-staging-development.md`.

The guarded rollout was approved and applied to the dedicated staging database
on 19 October 2026. Only `mentoring.0001_initial` was applied, taking its history
from 364 to 365 entries. The original eight accounts, passwords and balances
were preserved. Production was not changed.

Synthetic mentors are assigned to `volunteer05` (video unavailable) and
`volunteer06` (synthetic venue). Their test price is 5 Roo so existing test
balances can exercise booking; the ordinary model default remains 40 Roo. No
extra points were granted. Slack notification attempts are stopped by the
staging capability guard before creating a client. Google remains disabled.
The instructions below remain the runbook for a fresh installation.

## Isolated staging profile

The hosted `mlai.settings_staging_chat` profile enables mentoring for synthetic
accounts and explicitly disables Google credentials and requests. Production
continues to default to `MENTOR_BOOKING_ENABLED=false`. The initial migration
still requires its exact staging-only manual gate below; it is never applied by
the automatic updater. A rollback disables the staging flag or restores the
previous image and retains the additive tables and audit records.

## 1. Approvals

The proposed migration `mentoring/migrations/0001_initial.py` is in the branch. Still needed
before rollout:

1. Review the file against `docs/migration-proposals/mentoring-0001.md` (they match exactly:
   3 new tables, nothing existing changes, depends on `roo.0041_apple_iap_digital_credits`).
2. Record the file's sha256 in the staging `migration_hashes` inventory (the auto-deploy
   `compatible` check compares those hashes). The sha256 of the file as committed is in the
   migration proposal.
3. Maintainer approval to apply it to the staging database only. Nothing has been applied anywhere.

## 2. What the manual rollout contains

- New app: `'mentoring'` is added to `INSTALLED_APPS` in `mlai/settings.py` (already in the branch).
- Migration: `mentoring/migrations/0001_initial.py` (already in the branch).
  The deploy check runs `makemigrations --check` and `migrate --check`, so the file
  must be committed and applied together with the new image, never one without the other.
- New env values (all optional except the flag): `MENTOR_BOOKING_ENABLED=true` for staging
  only (default is false and production stays false), `MENTOR_REQUEST_TTL_HOURS=48`,
  `MENTOR_AUTO_COMPLETE_HOURS=24`. No new dependency, no new worker, no new route
  override: `/api/v1/community-chat/mentors/...` resolves through the normal
  `mlai.urls` tree after the staging overrides.
- Slack: staging has no real Slack. The mentor DM will fail with the staging 503
  (`staging_integration_unavailable`). That is expected: the session still works and
  `notification_status` is recorded as `failed` on the session. Do not treat it as a fault.

## 3. Synthetic mentor accounts

Mark two or three existing marked synthetic accounts as mentors (never real people):
in Django admin, "Vibe Mentoring" > Mentor profiles > add, pick the user, tick Active,
set location (MLAI space or Video), save. Suggested: one with video, one with MLAI space, and keep at least two other
synthetic accounts as founders. Give founders points the usual staging way
(at least 40 Roo per session they will be charged for; the charge happens on accept). Each mentor then signs in as
themselves and PUTs `/api/v1/community-chat/mentor/profile/` with weekly
`availability` (for example weekday 1, start "09:00", end "12:00") and optional `away_dates`.
Video sessions have no mentor link any more. On staging the Google calls are unavailable, so an
accepted video session shows `meeting_status: unavailable` and no `meet_link`. That is expected.

## 4. Verify

1. `GET /api/v1/points/development/build/` (authenticated synthetic account) shows the
   new release id. Wait for the manual deploy, not the timer.
2. `GET /api/v1/community-chat/home/` shows `feature_flags.mentor_booking: true`.
3. `GET /api/v1/community-chat/mentors/` lists the active synthetic mentors.
4. `GET /api/v1/community-chat/mentors/<id>/availability/` returns slots (UTC ISO times).
5. As a founder, POST `mentor/sessions/` with `mentor_id`, a slot as `starts_at`, `brief`
   (stage, struggle, outcome), `expected_price` (40), a fresh `client_request_id`. Expect
   201 and the founder balance UNCHANGED (a request charges nothing). Repeat the same body:
   expect 200.
6. As the mentor: `GET mentor/sessions/?role=mentor&status=requested`, then POST `.../accept/`.
   Expect the founder balance down by 40 now (ledger key `mentor_charge:<id>`) and the
   founder now sees `location_detail`. With a founder who has under 40 Roo, accept must
   return 409 `founder_insufficient_points` and the session stays requested.
7. POST `.../complete/` as mentor after the session END time: the payout appears in the
   points ledger with reference type `MENTOR_SESSION`. Cancelling an accepted session (even
   after its start) refunds the founder; declining a request moves no points.
8. With the flag off again, the same calls return 404 `mentor_booking_disabled`.

## 5. Rollback

- Quick off switch: set `MENTOR_BOOKING_ENABLED=false` and restart the API. All mentoring
  endpoints return 404 and the home flag turns off. Data and points stay as they are.
- Requested (not yet accepted) sessions hold no points, so nothing needs refunding. Accepted
  but not completed sessions have been charged: cancel them before turning off for good so
  founders are refunded.
- Full revert: deploy the previous image and only then reverse the migration (drops the
  three tables). Reversing it deletes mentor and session rows; the points ledger rows
  stay and remain the audit trail (cancel charged sessions first). The migration reversal needs its own approval.
