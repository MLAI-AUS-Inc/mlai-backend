# Community Home and token usage API

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
positive growth since the prior snapshot to the configured calendar day on
which that report arrives. This is report-arrival attribution, not the
session's start date or an estimate of when each token was consumed. Repeating
the same snapshot adds zero, and growth reported after Melbourne midnight is
credited to the new day. History backfill updates all-time totals only: it
establishes cumulative baselines but does not invent historical daily
attribution. The next live report credits only growth beyond that backfilled
baseline to its own arrival day.

## Leaderboard windows

`GET usage/leaderboard/?window=today|7d|30d|all&limit=100` returns public
opted-in rows and defaults to `today`. `today`, `7d`, and `30d` are inclusive Melbourne calendar-day
windows (`Australia/Melbourne` by default), not UTC or rolling-hour windows.
An optional `date=YYYY-MM-DD` anchors a current or historical calendar window;
invalid and future dates return 400. Invalid window values also return 400.

Responses include `timezone`, `date_from`, and `date_to`. All-time responses
set both dates to null. Daily history begins when live delta buckets are first
collected. Bucket dates always mean the live report-arrival date in the
configured timezone; a history backfill improves all-time totals but
deliberately does not populate any daily window or fabricate past daily
rankings. Public contributors with reported session history remain visible in
every window; when they have no matching live delta, that window returns zero
tokens and zero sessions for their row instead of removing them from the board.
