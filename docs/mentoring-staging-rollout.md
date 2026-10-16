# Vibe Mentoring: staging rollout handoff

For the staging operator. Source: branch `mentoring/staging` (= `staging` plus the
mentoring commits). The auto-deploy timer will NOT ship it: the change touches
`mlai/settings.py` (an OPERATIONAL path) and adds a migration, and
`ops/staging-chat/auto-deploy.py` refuses both. This is the separate reviewed
manual rollout described in `docs/backend-staging-development.md`.

Nothing here has been run. No migration file exists yet and no database test has
been run. Do not start until the steps in section 1 are approved.

## 1. Approvals needed first

1. Explicit approval to generate `mentoring/migrations/0001_initial.py` from the models
   in `mentoring/models.py`. The exact tables and fields are in
   `docs/migration-proposals/mentoring-0001.md` (3 new tables, nothing existing changes).
2. Review the generated file against that proposal, then record its sha256 in the
   staging `migration_hashes` inventory (the auto-deploy `compatible` check compares
   those hashes) and update the migration approval for the staging database.
3. Approval to apply it to the staging database only.

## 2. What the manual rollout contains

- New app: `'mentoring'` is added to `INSTALLED_APPS` in `mlai/settings.py` (already in the branch).
- Migration: `mentoring/migrations/0001_initial.py` (to be generated after approval).
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
save. Suggested: one with video, one with MLAI space, and keep at least two other
synthetic accounts as founders. Give founders points the usual staging way
(at least 80 Roo so two requests can be tried). Each mentor then signs in as
themselves and PUTs `/api/v1/community-chat/mentor/profile/` with weekly
`availability` (for example weekday 1, start "09:00", end "12:00"), a `video_link`
(an https link on a staging-safe placeholder), and optional `away_dates`.

## 4. Verify

1. `GET /api/v1/points/development/build/` (authenticated synthetic account) shows the
   new release id. Wait for the manual deploy, not the timer.
2. `GET /api/v1/community-chat/home/` shows `feature_flags.mentor_booking: true`.
3. `GET /api/v1/community-chat/mentors/` lists the active synthetic mentors.
4. `GET /api/v1/community-chat/mentors/<id>/availability/` returns slots (UTC ISO times).
5. As a founder, POST `mentor/sessions/` with `mentor_id`, a slot as `starts_at`, `brief`
   (stage, struggle, outcome), a fresh `client_request_id`. Expect 201 and the founder
   balance down by 40. Repeat the same body: expect 200 and no second charge.
6. As the mentor: `GET mentor/sessions/?role=mentor&status=requested`, then POST `.../accept/`.
   Check the founder now sees `location_detail`.
7. POST `.../complete/` as mentor after the start time (or try `.../decline/` on another
   request): payout or refund appears in the points ledger with reference type
   `MENTOR_SESSION`.
8. With the flag off again, the same calls return 404 `mentor_booking_disabled`.

## 5. Rollback

- Quick off switch: set `MENTOR_BOOKING_ENABLED=false` and restart the API. All mentoring
  endpoints return 404 and the home flag turns off. Data and points stay as they are.
- Open requests hold founders' points. Before turning off for good, decline or cancel
  open requests (or let them expire after 48 hours with the flag on) so refunds are issued.
- Full revert: deploy the previous image and only then reverse the migration (drops the
  three tables). Reversing it deletes mentor and session rows; the points ledger rows
  stay and remain the audit trail. The migration reversal needs its own approval.
