# Community Home and token usage API

## Correcting an invalid daily baseline

An operator can remove a single opted-in member's invalid daily delta without
changing their cumulative session history or all-time leaderboard total. The
command is a read-only preview unless `--apply` and an exact email confirmation
are both supplied:

```bash
python manage.py correct_token_usage_daily_buckets \
  --email member@example.com \
  --usage-date 2026-08-26

python manage.py correct_token_usage_daily_buckets \
  --email member@example.com \
  --usage-date 2026-08-26 \
  --apply \
  --confirm-email member@example.com
```

Production execution is exposed through the manually dispatched
`Correct production token usage daily buckets` workflow. Apply mode requires
the separate confirmation checkbox and accepts only one account and one date.

This document is the current backend contract for MLAI Chat's Community Home.
The endpoints live below `/api/v1/community-chat/`.

## Authentication and privacy

`GET home/`, `GET usage/leaderboard/`, and `usage/token/` require an
authenticated MLAI member session. The normal MLAI Chat account/bootstrap
credentials and the existing MLAI user JWT are accepted. A reporter token is
not an account credential and cannot read either endpoint.

Community Home returns only the caller's aggregate Roo balance, public or
volunteer work that is currently claimable, active in-stock rewards, and
verified feature flags. It does not return other members' balances, Slack ids,
reviewer or assignee ids, internal tasks, redemption history, or task metadata.

## Community Home

`GET home/` returns four top-level keys:

- `points`: the caller's spendable balance and their own earned, purchased,
  lifetime-earned, and lifetime-spent totals;
- `earn_actions`: the +4 first introduction, the configured monthly-update
  reward when enabled, and live unassigned volunteer/public Roo tasks;
- `rewards`: active rewards with non-zero or unlimited stock and an affordability
  hint for the caller;
- `feature_flags`: currently `link_love` (false until a verified runtime exists)
  and `meeting_rooms` (from `MEETING_ROOM_BOOKING_ENABLED`).

Task actions include the command `@Roo task claim <task_code>`. The endpoint
does not use `TaskTemplate`, so closed or unpublished templates cannot appear
as current opportunities.

## Reporter ingest and history

A member mints or rotates a credential through `usage/token/`. The returned
`mlai_usage_...` token is scoped only to the two reporter writes:

- `POST usage/api/ingest` is the live hook endpoint;
- `POST usage/api/history` is a one-time cumulative-snapshot backfill.

Both accept tokenmaxer's `{source, sessions}` wire format and return
`{accepted, rejected}`. Counts are self-reported community statistics, not
billing records or a basis for prizes.

Each reporter row is a cumulative `(source, session_id, model)` snapshot.
All-time totals come from the latest monotonic snapshot. Live ingest adds only
positive growth since a prior snapshot to the configured calendar day on which
that report arrives. An unseen live snapshot establishes a baseline and adds
nothing to the daily window; otherwise a member's entire cumulative history
could be mislabelled as today's usage. This is report-arrival attribution, not
the session's start date or an estimate of when each token was consumed.
Repeating the same snapshot adds zero, and growth reported after Melbourne
midnight is credited to the new day. History backfill updates all-time totals
only: it establishes cumulative baselines but does not invent historical daily
attribution. The next live report credits only growth beyond that backfilled
baseline to its own arrival day.

## Leaderboard windows

`GET usage/leaderboard/?window=today|7d|30d|all&scope=mlai|australia&limit=100`
returns ranked public rows and defaults to `today`. `scope=mlai` ranks only
opted-in MLAI reporter accounts. `scope=australia` adds the read-only public
Tokenmaxer federation and ranks the combined result; it remains the API default
for compatibility with clients released before scopes were introduced. MLAI
Chat always sends an explicit scope and defaults its UI to MLAI-only.

`today`, `7d`, and `30d` are inclusive calendar-day windows in the configured
leaderboard timezone (`UTC` by default), not rolling-hour windows. Sessions are
assigned by `started_at`, so a history import appears in the period when each
session began instead of the day the import arrived. An optional
`date=YYYY-MM-DD` anchors a current or historical calendar window; invalid and
future dates return 400. Invalid window or scope values also return 400.

Responses include `scope`, `timezone`, `date_from`, and `date_to`. All-time
responses set both dates to null. A history backfill therefore contributes to
the appropriate historical windows as well as all time. Every public opted-in
MLAI reporter account remains visible in every MLAI window. Rows include
`has_reported`: false means the member connected but the backend has not
accepted a session yet; true with zero window totals means the member has
history but no session that began in that period.
