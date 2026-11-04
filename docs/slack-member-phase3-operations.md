# Slack member migration: Phase 3 foundations

Phase 3 prepares the member-authorized channel lane. It removes the unused bot
lifecycle worker, callback hooks, reconciliation cursor and bot-membership
field. It does not activate the new router, create rooms, change Slack app
subscriptions or select member tokens for existing community traffic.

## Configuration and consent

Keep these deployment values at their defaults for this release:

| Key | Value |
| --- | --- |
| `SLACK_MEMBER_CHANNELS_ENABLED` | `false` |
| `SLACK_COMMUNITY_SEND_AS_MEMBER` | `false` |
| `SLACK_COMMUNITY_SOURCE` | `bot` |
| `MESSAGE_SYNC_EVENT_ROUTING` | `false` |

All enabled switches require durable message sync. Member channels also require
owner inventory; member sending and non-bot community intake require member
channels. The deployment validator rejects invalid dependencies before rollout.
The retired `COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED` has no runtime consumer.

The owner chose to retain existing Slack consent. Existing accepted v3/v4/v5
consent and current OAuth account/workspace authority remain required; there is
no v6 prompt. Public source eligibility also requires `channels:read`,
`channels:history` and `chat:write`. A reader does not require an attribution
link. A poster additionally needs the author's current verified device,
account-backed Slack identity link and channel membership. Every provider call
rechecks current authority; cached probes do not replace that fence.

## Migration review

The owner approved creating `integrations.0050_slack_member_channels` and
testing it in disposable local/CI databases after Phases 1 and 2 merged.
Production migration approval and release approval are separate owner steps.

0050 adds channel audience, reader and audience-sync fields and the
`SlackChannelMembership` table. It marks enabled, mapped Buzz rows as Community
rooms while preserving their room IDs. New rows default to Members. It removes
`CommunityBridgeLifecycleState` and `slack_bot_member`.

Cleanup is restricted to the 25 private policy seeds frozen in 0049. A row must
match its original configuration, have no destination, have no queued refresh
and have no related records. Edited rows and retained delivery/history records
survive. The public `ethan_test` exclusion survives. No Slack or adapter API is
called. PostgreSQL checks deferred constraints between cleanup and schema DDL
inside the same atomic migration.

The proposed canonical Django plan, rendered without database execution against
the read-only production recorder snapshot, has SHA-256
`701f5cd2a99268365878c3a8d94c316ef6ef4055160c69fe1093ffb16ee24e71`.
The migration file has SHA-256
`8efeff84eec23d7119d1b8d6307ac7b96e11d0dd5793912cc3bf61961dc98fee`.
These identify the proposal, not an approval. Before an authorized release,
recheck the exact reviewed source and require the deployment's actual pending
plan to match its specifically approved hash. Preserve backup, writer-pause,
current-main and post-migration checks. A different pending plan requires a new
review; never set the approval variable just to get past a failed deploy.

## Verification

The migration tests execute real forward and reverse schema transitions on
SQLite and PostgreSQL. They cover exact seed cleanup, operator changes,
delivery/refresh preservation, public exclusion, Community backfill, per-grant
membership uniqueness and reader `SET_NULL` with membership cascade deletion.
Run database suites only under the specific disposable-migration approval.
The CI workflow selects every new test module in its SQLite and PostgreSQL
lanes. `python manage.py makemigrations --check --dry-run` must show no drift.

After the approved deployment, confirm all four runtime values above, no new
Members rooms or automatic member-provider calls, the same enabled community
room mappings, normal health and private/public bridge behavior. Keep evidence
to aggregate counts and outcomes. Do not log credentials or message bodies.

## Rollback and next gates

Prefer reviewed forward recovery. A schema downgrade removes the new empty
membership state and audience fields, recreates the empty lifecycle cursor and
default-false bot-membership field, and restores missing disabled private policy
placeholders without overwriting existing rows. It cannot recover operational
state written after member-channel enablement. Coordinate any downgrade with
the matching code and a separately approved exact migration plan.

Phase 4 needs Phases 2 and 3 deployed and compatible desktop/mobile builds
released. Live Slack callback checks and new Members enablement stay limited to
the owner's approved public canary channel. The mapped-ID rekey helper fails
closed with `rekey_requires_room_migration`; implement the explicit room
migration contract before enabling the ID-change router. Seven days of actual
dual-source observation and fourteen stable member-source days still precede
the later cutover and cleanup.
