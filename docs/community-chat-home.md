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

`GET home/` returns six top-level keys:

- `roo_public_key`: the configured public identity of the deployed MLAI Chat Roo
  assistant, or null when unavailable. `COMMUNITY_CHAT_ROO_PUBLIC_KEY` must be
  a 64-character hexadecimal public key. This does not create or deploy an
  assistant; its DM and tagged-message handler must be operational before
  configuration. Home responses use `Cache-Control: private, no-store`.

- `roo_slack_user_id`: Public Roo's configured Slack bot user ID, or null when
  disabled or outside the MLAI relay. The native assistant public key takes
  precedence. Without that key, Talk to Roo opens the member's existing private
  Slack Roo DM through `POST slack/dms/` with this single recipient. Opening a
  chat sends no message. Disconnected members first complete the existing Slack
  consent/OAuth flow. See [Roo chat](community-chat-roo-dm.md).

- `points`: the caller's spendable balance and their own earned, purchased,
  lifetime-earned, and lifetime-spent totals;
- `earn_actions`: the +4 first introduction, the configured monthly-update
  reward when enabled, and live unassigned volunteer/public Roo tasks;
- `rewards`: active rewards with non-zero or unlimited stock and an affordability
  hint for the caller;
- `feature_flags`: currently `link_love` (false until a verified runtime exists)
  `meeting_rooms` (from `MEETING_ROOM_BOOKING_ENABLED`), and member-scoped
  `coworking_booking` (see [the handoff contract](community-chat-coworking.md)).

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


### Slack catalog member presentation

The authenticated Slack status response's `channel_catalog` now includes a
`participants` array for `im` and `mpim` entries. Each member has `slack_user_id`,
`display_name`, `avatar_url` (empty when unavailable), and `is_owner`. These are
existing source Slack profiles, restricted to the conversation's current
participant IDs. Private-channel entries keep their existing shape.

The catalog remains filtered to mirrors provisioned for the requesting verified
device and its account. It exposes no message bodies and grants no additional
relay access. Clients must use the source people for group avatar stacks rather
than treating transport keys or multiple owner devices as group members.
Older clients can ignore the additive field. Newer clients show a neutral group
icon when an imported conversation has no source profiles; relay/device keys
must never be used as fallback people for a known Slack mirror. No schema change or historical backfill is needed.

## Upcoming event artwork

`GET upcoming-events/?limit=5` includes `cover_url` on each public event.
It is Luma's public cover image hosted at `https://images.lumacdn.com/`, or
an empty string when absent or invalid. Only HTTPS URLs without embedded
credentials or nonstandard ports are accepted. Clients show the botanical
date square first, followed by a separate square cover image, with a neutral
fallback for missing or failed artwork. This additive field does not expose
private event settings or attendee data. The cache key is versioned to avoid
serving the older projection after deployment; no migration is required.

Source: [Luma cover image field](https://docs.luma.com/reference/post_v1-events-create).
## Roo Points member guide refresh (September 2026)

Home includes `boost_startup` (2 points, once for each distinct verified startup
post) and `helpful_answer` (3 points, existing approval and weekly cap). The
canonical volunteer policy supplies the amounts. Startup engagement retains
source verification, activation flags and per-member/per-post idempotency;
the former four-post monthly cap is removed prospectively. Existing awards
and balances are unchanged. Channel destinations are `#boost-my-startup` and
`#i-need-advice-or-help`.

Member journey responses and suggestions omit `monthly_learning_update`;
the policy key remains available to interpret historical receipts. Home now
returns up to 100 available rewards so the Roo Points page can display the
full catalogue; the clients select three popular rewards plus the 72-point
newsletter feature, which is requested through Roo.

Clients display whole points and retain exact ledger strings. Reconciliation
checks and audit timestamps are still returned but update timestamps are not
shown on the simplified member pages. This change requires no migration.
