# MLAI Chat bridge contract

The community bridge provides a live, bidirectional view of selected public
Slack channels inside MLAI Chat. Slack remains usable throughout the rollout;
MLAI Chat is another client surface, not a one-time data migration.

## MVP scope

- Operators explicitly map a public Slack channel to one MLAI Chat channel.
- New messages, replies, edits, deletes, the full emoji-mart Unicode 15 reaction catalog
  (including Slack aliases, flags, ZWJ sequences and skin tones), and safe Slack shortcode reactions are mirrored after
  the mapping is enabled. Shortcodes use canonical `:name:` content with an
  inner `[a-z0-9][a-z0-9_+-]{0,61}` name so the full value stays within MLAI
  Chat's 64-scalar reaction limit; longer Slack custom-emoji names fail closed.
  General historical backfill is out of scope for mapped public channels. A
  bounded, operator-confirmed repair command exists only for pre-cutover messages
  whose retained Slack receipts still contain resolvable user/channel references.
- Direct messages, private channels, huddles, workflow payloads, ephemeral
  messages, and Slack Connect channels fail closed unless separately approved.
- Attachments remain represented as safe provider-hosted links in the durable
  bridge event. For Slack image links, the authenticated MLAI Chat preview API
  may fetch the image on demand and return a bounded, private-cache response.
  Public-channel images use the bridge bot and must be shared in an enabled
  Slack channel mapped to MLAI Chat. Private-DM images use the requesting
  owner's current Slack grant and must belong to that owner's live or paused
  private mirror. The proxy never exposes either Slack token or stores image
  bytes in bridge records/the database; unsupported or out-of-scope files fail
  closed.
- Mirrored messages are visibly attributed to the source author and platform,
  but are signed/sent by a dedicated MLAI bridge identity.

## Canonical event

Every verified provider event is normalized to:

- receipt key and source platform;
- source channel, message, optional parent, and author identifiers;
- one delivery operation: `create`, `edit`, `delete`, `reaction_add`, or
  `reaction_remove`;
- sanitized text, deferred Slack user/channel reference metadata, and HTTP(S)
  attachment links; and
- non-secret adapter metadata.

Slack creates also retain the source timestamp and whether Slack explicitly
broadcast the reply into the channel. The adapter signs that timestamp into
provenance while keeping the relay event's durable outbox timestamp unchanged,
so historical repairs remain idempotent and clients can restore Slack ordering.

Provider-specific payloads stay at the ingestion edge and are cleared under the
raw-payload retention policy. The only provider markup copied into canonical
metadata is the Slack message text needed for deferred user/channel resolution;
it is never sent to a destination without sanitization. Delete events
deliberately retain no message content.

## Delivery guarantees

- `(source platform, receipt key)` is the ingestion idempotency boundary.
- A durable outbox is claimed transactionally and retried with bounded backoff.
- Message links map source IDs to destination IDs so replies, edits, and deletes
  address the correct provider object.
- Slack sends use a deterministic `client_msg_id` derived from the durable
  delivery row. Reaction objects use a stable hash of the immutable source
  message, reaction name, and source author so removal targets the exact mapped
  reaction rather than the parent message.
- Delivery is at least once; adapters must be idempotent for a claimed outbox
  row. Ordering is best effort within one mapped channel.
- Replies whose parent mapping is not ready are parked without consuming the
  provider retry budget. Completing the parent wakes its parked children; a
  bounded age and dependency-attempt limit dead-letters unresolved children
  instead of flattening them into top-level messages.
- Exhausted deliveries enter a dead state for operator inspection and replay.

The backend reaches the Rust sidecar with `BUZZ_BRIDGE_ADAPTER_TOKEN`. When the
backend and MLAI Chat share a private network, it uses the adapter's private
`POST /v1/deliveries` endpoint on port 8090. The authenticated
`POST /v1/lookups` endpoint is restricted to the same mapped-channel allowlist
and is used only to reconcile trusted events signed by the bridge key. The
production cross-VPC deployment
uses the exact TLS base `https://chat.mlai.au/_mlai/bridge`; Caddy strips that
prefix and proxies to the same private adapter, which is not bound to a public
port. No other public adapter host or path is accepted. The sidecar posts relay
events to
`/api/v1/integrations/bridge/buzz/events`; it signs the exact raw body and Unix
timestamp with `BUZZ_BRIDGE_CALLBACK_SECRET`. The API rejects bodies over 256
KiB, invalid signatures, and callbacks outside the five-minute replay window
before parsing the JSON.

## Loop prevention

- Slack events authored by the configured bridge bot are ignored.
- MLAI Chat events authored by the dedicated bridge public key are ignored.
- Adapters attach source/message provenance where the provider supports it.
- A mirrored event is never forwarded to a third platform during MVP.

## Identity and privacy

Account-to-Slack and account-to-MLAI-key bindings are separate, verified
records. The bridge never impersonates a human or holds a human chat private
key. Logs contain provider IDs and outcomes, not message bodies, invite codes,
tokens, or private keys.

## Slack application setup

Use a dedicated Slack app and bridge bot, installed only into the explicitly
mapped public channels. Configure the Events API request URL as
`https://api.mlai.au/api/v1/integrations/bridge/slack/events`, subscribe to the
bot events `message.channels`, `reaction_added`, and `reaction_removed`, and
grant only `channels:history`, `channels:read`, `chat:write`, `files:read`,
`reactions:read`, `reactions:write`, and `users:read`. Record the bot user ID as
`SLACK_BRIDGE_BOT_USER_ID` so its messages and reactions are discarded for loop
prevention.

The public-channel normalizer continues to ignore direct messages, private
channels, and payloads marked as shared/external. A separate consent-gated DM
path handles Slack IMs and multi-person IMs for one linking owner. The owner's
verified MLAI Chat key and deterministic shadow keys for the other participants
determine a private destination conversation. Other participants do not need to
link and do not gain access to that owner-controlled copy; if they link, they
receive independent mirrors. Slack Connect conversations remain excluded.
Private message bodies use a dedicated encrypted queue and are erased after
delivery; they never enter public bridge receipts, Roo, organization memory,
search, or analytics. Operators must also confirm that every public mapped
channel is not a Slack Connect channel before enabling it. Rotate the Slack
signing secret, adapter token, callback secret, and bridge Nostr key
independently.

The bridge Nostr public key is intentionally non-secret. Configure that same
lowercase 64-character value as `MLAI_BRIDGE_PUBKEY` in browser, desktop, and
mobile release jobs. Clients trust provenance tags only when the Nostr event is
signed by this key. Never expose the corresponding private key or any Slack
credential to a member client.

## Required backend settings

```dotenv
SLACK_BRIDGE_BOT_TOKEN=xoxb-...
SLACK_BRIDGE_SIGNING_SECRET=...
SLACK_BRIDGE_BOT_USER_ID=U...
BUZZ_BRIDGE_ADAPTER_URL=https://chat.mlai.au/_mlai/bridge
BUZZ_BRIDGE_ADAPTER_TOKEN=...
BUZZ_BRIDGE_CALLBACK_SECRET=...
SLACK_OAUTH_USER_SCOPES=channels:history,channels:read,groups:history,groups:read,im:history,im:read,im:write,mpim:history,mpim:read,mpim:write,chat:write,team:read,users:read,reactions:read,reactions:write,files:read
SLACK_DM_MIRROR_HISTORY_DAYS=30
SLACK_DM_MIRROR_DELIVERY_BATCH_SIZE=20
SLACK_DM_MIRROR_SHADOW_SECRET=replace-with-a-long-random-secret
```

For one-click chat linking, add `message.im`, `message.mpim`, `message.groups`, `reaction_added`,
and `reaction_removed` under **Subscribe to events on behalf of users** in the
Slack app and keep the same signed request URL used by the bridge. Reauthorize
existing users to add `mpim:write`, `reactions:read`, `reactions:write`, and
`files:read`; file metadata and links are mirrored without requesting
`files:write`. The OAuth callback marks DM discovery due after the new user
token is stored.

Each linked member receives an independent, owner-controlled mirror of direct
and multi-person Slack DMs, plus private channels after explicit consent.
The authenticated user token lists internal memberships with
`users.conversations`, using `im,mpim,private_channel` and pages of 20. Existing
7/30-day grants list active conversations. Explicit all-history consent also
lists archived conversations that Slack still allows the owner to read; those
mirrors expose `source_archived: true` and are read-only.
Discovery, paced history and delivery run independently; new mirrors appear
before history completes. Discovery sorts explicit Slack latest-message metadata newest-first and
skips the history request when that marker proves its latest activity is older
than the cutoff, while still creating the conversation in the directory. The channel-metadata `updated` field is not treated as message
activity. Missing or ambiguous activity metadata, and any channel with a staged
live callback, fail open to the bounded history scan. The other participants
are represented by deterministic shadow keys, so linking never gives an
unconsenting participant access to imported history. Participant profiles are
bulk-preloaded with `users.list` and fall back to `users.info` for any IDs Slack
omitted.

History requests fetch up to 200 messages, run at the 50-requests/minute
baseline, persist the oldest timestamp boundary, and honor Slack's
`Retry-After` response without blocking OAuth or Community Home. Each persisted
page is released to delivery immediately, so a large scan becomes visible while
older pages continue loading. Conversations awaiting their first history page
are scanned before deeper pages; the remaining queue is ordered by least recent
attempt. These server workers continue when the client closes. Discovery,
metadata lookup and history all respect retry cooldowns and saved progress. Consecutive top-level creates for one private
conversation are delivered in ordered batches of up to 20 through
`POST /v1/private-deliveries/batch`; the adapter then uses the relay's
trusted-private `POST /events/batch` route. One grant/conversation revocation
fence and one adapter registration lease cover the whole batch, while every
signed event still passes the normal relay ingest pipeline.
For private edits, deletions, and reactions, the adapter's `source_message_id`
is the target Slack message timestamp from `target_source_message_id`. Internal
`slack-event:` and `reaction:` queue keys remain unchanged for deduplication;
`delivery_id` identifies the individual adapter operation. A newly created
message continues to use its own Slack timestamp.
During a rolling deployment, a backend that reaches an older adapter falls
back to the same deterministic single-delivery endpoint; an adapter that
reaches an older relay returns a retryable upstream failure until the relay is
updated.
Backfill status is complete only after every queued history delivery completes;
transient dead rows are safely repopulated from Slack, while a permanently
rejected adapter delivery stays fenced until explicit backfill or renewed
consent. New API callers that omit a history choice retain the seven-day default.
Members can explicitly choose 7 days, 30 days, or **all available history**
(`history_days: 0`). Zero is honored only with the new
`slack-chat-v5-all-available-history` consent; legacy zero-valued grants remain
bounded until the owner opts in. The configured maximum still caps bounded
7/30-day imports, without silently widening their consent. The legacy
`backfill_all` action remains a bounded compatibility alias. Initial all-history
scans omit `oldest`; bounded imports always supply it. The
idempotency key prevents duplicate deliveries. A queued or failed backfill row
that ages past the rolling cutoff is completed as a content-free tombstone
instead of being sent or retried. Periodic source reconciliation rehydrates any
current row that an older importer incorrectly classified as outside the
window.

The backend also starts an hourly reconciliation and requests one after Slack
reports `app_rate_limited`. These refresh requests preserve an incomplete
archive scan's durable cursor. After an all-history import completes, refresh
scans cover the most recent 30 days plus any gap since the latest imported
source timestamp, with a one-day overlap. A content-free queue marker saves
that boundary even for an empty conversation, so reopening it does not restart
an entire archive scan. Delivery and history pages advance `latest_synced_ts`
monotonically; delivering older archive pages cannot move recency backward.
Choosing a wider history scope preserves that latest timestamp while resetting
only the older-page cursor. A recent refresh cannot infer deletion of replies
whose roots are outside its scan boundary; signed deletion callbacks remain
authoritative for those threads. All-history refreshes retain the original
thread relationship even when the root predates the recent refresh cutoff. A message that disappeared from an
otherwise complete bounded scan is mirrored as a delete. Slack history may
truncate the actor list on a reaction (`count` can exceed `users.length`), so
absence from history is deliberately **not** treated as a reaction removal;
only a signed `reaction_removed` Events API callback is authoritative.

This deployment does not configure an app-level token with
`authorizations:read`. Private callbacks are therefore routed only to the exact
user installation identified by Slack's signed `authorizations`/`authed_users`
fields; the backend never copies a DM body into unrelated workspace owners as a
fan-out shortcut. Slack may represent additional installations behind
`event_context`; complete one-callback multi-owner fan-out would require the
separate `apps.event.authorizations.list` contract and credential. The current
production deployment has one active owner grant. If multi-owner live fan-out
is enabled later, add that app-level contract before claiming immediate parity
for every owner (bounded reconciliation remains the recovery path).

All active verified MLAI Chat device keys are included in a one-to-one mirror,
alongside the counterpart shadow key. Revoked keys are removed on the next
discovery. MPIMs and private channels use one conversation-specific synthetic import
identity, leaving room for up to eight verified owner devices within the relay's
nine-key transport limit. Signed Slack provenance preserves each message author.
Private-channel size therefore does not grant other Slack members relay access.
Status/start responses report if an owner has more devices than fit. An active preferred identity is never
silently rebound; a revoked or otherwise inactive preferred device is repaired
atomically to the authenticated verified device (or the newest active device in
worker discovery), marks every destination participant set due for
re-provisioning, and requeues Slack history for the new private destination.
Private registration sends these included owner-device keys separately as
`callback_author_pubkeys`; every callback author must also be a conversation
participant. The adapter polls each private channel only for that registration's
callback-author keys, so one owner's device authorization cannot broaden
another channel's callback scope.

Every private-registration POST has a distinct, content-free durable attempt
row written before adapter I/O. The row binds the exact consent generation,
Slack participant set, destination key set, callback-author key set, and any
returned channel ID. Ambiguous, superseded, and interrupted attempts remain
retryable until their deterministic adapter registration is reconciled and
deleted. Consent resume, participant replacement, and device reactivation stay
fenced while earlier cleanup is pending, so a delayed POST or DELETE cannot
silently replace or remove the current registration. Registration-control rows
never enter the normal private-message delivery worker and contain no message
body.

Community Chat exposes these owner-authenticated endpoints:

- `GET /api/v1/community-chat/slack/` returns delivery-aware backfill (including
  imported and queued message counts), identity-repair, device-capacity, and
  history scope (`history_scope: "recent" | "all"`) and delivery-aware progress.
- `GET /api/v1/community-chat/slack/users/?q=...&limit=...&cursor=...` searches
  internal human Slack users. It excludes deleted, bot, app, Slack Connect, and
  owner rows and never returns email addresses or OAuth tokens.
- `POST /api/v1/community-chat/slack/dms/` accepts `slack_user_ids` containing
  one to eight non-owner IDs, calls `conversations.open`, provisions the exact
  private MLAI conversation, and returns its participant public keys and
  sanitized profiles. The owner key always comes from the authenticated active
  verified Community Chat device; body-supplied owner keys are ignored.
- `PATCH /api/v1/community-chat/slack/` accepts `pause`, `resume`, `backfill`, and
  `refresh_channel`. `backfill` accepts `history_days: 0` only as an explicit
  all-history selection and records the v5 consent. The legacy `backfill_all`
  value remains a bounded compatibility alias for older installed clients.
- `PATCH .../slack/` with `action: "refresh_channel"` and `channel_id`, or
  `GET .../slack/?channel_id=...`, returns only a mirror provisioned for the
  authenticated owner/device. Fields are `status` (`syncing`, `complete`,
  `error`), `history_days`, `history_scan_complete`, `last_synced_at`,
  `imported_messages`, `queued_messages`, `failed_messages`, and
  `source_archived`. Scanning complete alone does not mean message delivery is
  complete. Rapid repeated opens share a refresh; they never reset a partial
  scan.
- `DELETE /api/v1/community-chat/slack/` revokes local consent and Slack token
  access, erases queued private bodies, then makes one best-effort call to the
  adapter's authenticated, idempotent
  `DELETE /v1/private-conversations/{channel_id}`. Success means that the local
  privacy boundary is durable. Every provisioned mirror is recorded in a
  content-free durable cleanup ledger; any remaining registrations or adapter
  failures are retried by periodic reconciliation without restoring Slack
  access or retaining message bodies.
- `POST /api/v1/community-chat/messages/delete-slack-origin/` accepts the
  mirrored Buzz event ID and a caller-generated idempotency UUID. It verifies
  the active MLAI device and linked Slack author, then uses that member's
  connected Slack user token to delete the Slack source message. The signed
  Slack deletion callback remains authoritative for removing the mirrored
  event; requests and provider outcomes are retained as content-free audit
  records.

Private delivery retries are direction-specific: Slack-origin rows retry only
through MLAI Chat, while MLAI-origin rows retry only through Slack with a stable
UUID `client_msg_id`. Discovery and history maintenance run independently from
the delivery retry loop. Adapter provisioning occurs for discovery, explicit
start, or participant changes rather than as a fleet-wide periodic refresh.

Create each public-channel mapping with:

```sh
python manage.py upsert_community_bridge_channel \
  --slack-workspace-id T0123456789 \
  --slack-channel-id C0123456789 \
  --slack-channel-name community \
  --destination-platform buzz \
  --destination-workspace-id chat.mlai.au \
  --destination-channel-id 922c3b22-8002-4c3c-a37b-ce406a5e606e \
  --destination-channel-name community
```

After investigating an exhausted delivery, replay it without changing its
idempotency identity:

```sh
python manage.py requeue_community_bridge_delivery 1234 --confirm
```

If the relay's audited rejection log proves that an otherwise valid delivery
was rejected only because its deterministic event timestamp is stale, and the
operator has separately verified that no destination event or message link
exists, refresh the timestamp explicitly while preserving the durable delivery
ID:

```sh
python manage.py requeue_community_bridge_delivery 1234 \
  --confirm \
  --refresh-event-timestamp \
  --confirm-stale-relay-timestamp \
  --confirm-no-destination-event
```

Never use timestamp refresh for an ambiguous timeout or after a destination
link exists; normal retries retain the original timestamp and signed event ID.

## Chat import API and rollout

The existing authenticated `GET /api/v1/community-chat/slack/` response adds
`discovery_pending`, `private_channels_enabled`, and `channel_catalog` entries
of `{channel_id, kind, last_message_at}` (`im`, `mpim`, or `private_channel`). Only mirrors
provisioned for the caller's verified device appear in that catalog. Clients
use source type rather than relay participant count to classify chats. The
nullable ISO UTC `last_message_at` is the latest known Slack message timestamp,
not the import or channel update time. Discovery reads `conversations.info`
when the directory omits the latest marker; only its timestamp is retained.
Catalog timestamps allow clients to sort pending imports, while newer live
message activity takes precedence. Group avatars exclude `is_owner` participants;
1:1 avatars come from the counterpart, never the owner's other device profiles.

`POST` accepts `{"history_days": 7}` (default) or `{"history_days": 30}`. Other
values return 400. It either activates the existing sufficiently scoped user
connection or returns the user OAuth authorization URL. The signed OAuth
return state preserves the chosen window and private-channel consent. Old
connections must explicitly reconnect for `groups:read` / `groups:history` and
`slack-chat-v4-private-channels` consent. Generic connector callbacks do not
silently broaden consent. Updating the same active account keeps existing
registrations and queues discovery instead of synchronously deleting them.

`PATCH {"action": "backfill", "history_days": 30}` expands the import. Omitting
the window preserves the existing grant's window, including for the legacy
`backfill_all` action. Pause, resume and disconnect remain supported. Disconnect
erases credentials, queued private bodies and source names, retaining only
content-free conversation-type fences to prevent late private events from
falling into a less restrictive route.

No new migration is required: connector metadata stores the owner-scoped
catalog. Release the bridge adapter (private Slack IDs may begin with `C` or
`G`), backend worker/API, and updated clients together. Add `message.groups`
subscriptions and reconnect a controlled test account before a live pilot.
No deployment or pilot is implied by this implementation. The one-minute
objective is first useful results, not a guarantee that every conversation's
history has finished. Slack plan retention, missing scopes and API rate limits
still apply. Archived and external Slack Connect conversations, bot messages,
and unsupported Slack system events retain their existing exclusions.

## Slack thread reconciliation

Slack is authoritative for thread membership. Audit a controlled batch before
applying any mutation:

```sh
python manage.py reconcile_community_bridge_slack_threads \
  --slack-channel-id C0123456789 \
  --max-roots 25
```

The JSON report identifies missing events, orphaned or incorrectly parented
replies, incorrect broadcast state, duplicate bridge events, stale links, and
links that can be restored without republishing. A multi-channel dry-run returns
one cursor per channel in `resume.by_channel`; apply mode deliberately requires
exactly one channel, whose cursor is also exposed as `resume.latest`. Continue
older batches only with that channel's cursor. Apply after reviewing the
mismatch rate and worker health:

```sh
python manage.py reconcile_community_bridge_slack_threads \
  --slack-channel-id C0123456789 \
  --latest <resume.latest> \
  --max-roots 25 \
  --apply \
  --confirm-historical-repair \
  --wait-seconds 120
```

Apply mode restores trustworthy database links, tombstones malformed or
duplicate bridge events, recreates each source message once with its exact
Slack parent and broadcast state, and waits for the durable worker after every
step. Receipt keys include the reconciliation version, source ID, and target
event ID, so repeating a completed batch is idempotent. The GitHub Actions
workflow `Reconcile production Slack threads` exposes the same bounded,
dry-run-first operation for production.

Run the live staging matrix and capture durable, content-free evidence with
[`mlai-chat-bridge-staging.md`](mlai-chat-bridge-staging.md). The final database
check is:

```sh
python manage.py verify_community_bridge_staging \
  --slack-channel-id C0123456789 \
  --slack-message-id 1785550000.000100 \
  --buzz-event-id 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

The deployment, production-settings, backup/restore, security-review, and
rollback gates are in
[`mlai-chat-release-runbook.md`](mlai-chat-release-runbook.md).

## Verified identity links

Identity links are optional presentation metadata. A bridged Slack message is
still signed by the dedicated bridge key, never by the linked human. The stable
key is `(Slack workspace ID, Slack user ID) ↔ Nostr public key`; display names
are mutable labels only.

Before creating a link, an operator must independently verify control of the
Slack account and the Nostr key—for example, an authenticated MLAI/Slack
account check plus a fresh signed Nostr challenge. Put only the non-secret audit
or ticket reference in the command; never put the challenge secret, private
key, token, or email address there.

```sh
python manage.py verify_community_bridge_identity \
  --slack-workspace-id T0123456789 \
  --slack-user-id U0123456789 \
  --buzz-pubkey 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --display-name "Example Member" \
  --verification-method operator_attested \
  --verification-reference MLAI-1234 \
  --confirm-dual-control
```

Revoke immediately when either account is disconnected, compromised, or
reassigned:

```sh
python manage.py revoke_community_bridge_identity \
  --slack-workspace-id T0123456789 \
  --slack-user-id U0123456789 \
  --reason "account disconnected"
```

## Standard emoji data

`integrations/services/community_bridge/slack_emoji.json` pins the same
`@emoji-mart/data` 1.2.1 / Unicode 15 native catalog used by MLAI Chat. The codec
in `integrations/services/slack_emoji.py` translates both directions, including
skin-tone suffixes, while preserving bounded workspace custom shortcodes.
Workspace-specific image assets still use the existing custom-emoji transport;
this catalog contains Unicode glyphs, not workspace images.
Regenerate using `python scripts/generate_slack_emoji.py /path/to/@emoji-mart/data/sets/15/native.json`.
Verify without a database using `python -m unittest integrations.tests_slack_emoji`.


## History consistency audit — 10 September 2026

The directory, backend import queue, and relay history are distinct stores.
Provisioning a directory entry means the owner's devices can address its private
relay destination; it does not mean Slack history has been fetched or delivered.
Clients must show importing/retrying status instead of interpreting a temporarily
empty relay response as an empty Slack conversation. Routine reads use the relay
and a scoped client cache, rather than waiting for Slack history HTTP calls.

The backend persists each list-page checkpoint in connector state. Main history
uses a persisted oldest timestamp; thread replies use per-thread cursors bound to
the same consent, registration, participant boundary, and scan epoch. A page is
queued atomically and released immediately. A repeated/non-progressing main or
reply boundary now raises a retryable import error; it cannot run forever or
falsely mark the conversation complete. Queue bodies remain encrypted until
relay delivery, then are erased. Content-free delivery receipts preserve
idempotency and progress. Relay storage is the durable message read model.

The all-history selection covers supported owner-readable Slack IMs, MPIMs,
and private channels, including archived readable memberships. Historical
messages and reactions by former members or bots in groups use the existing
conversation import identity while preserving source author IDs and available
names/avatars. They never add those people to the relay audience. One-to-one
DMs retain the exact owner/counterpart boundary. Slack Connect remains excluded.
Public channels use the separate explicitly mapped bot bridge; owner consent
never publishes private history into those shared channels.

Validation for this change ran 57 database-free tests covering consent upgrade,
legacy consent preservation, archive cursors, recent refresh boundaries, expired
queues, delivery-aware status, author/audience separation, archived write
rejection, and pagination failures. The runner forcibly disabled both database
connections and network access. No migration was created or applied, and no
production integration was exercised.

Remaining operational and parity gaps:

- A shared cache stores Slack cooldowns by workspace and method, but a
  `Retry-After` also pauses the process-wide history loop. Rate limiting in one
  workspace can therefore delay another workspace. Multiple worker processes do
  not share a proactive request budget; they share only the response-driven
  cooldown. Replace this with a shared per-workspace/method token budget before
  scaling worker replicas. The current loop paces a history call every 1.2
  seconds, plus actual I/O and persistence time. Discovery and profile calls
  consume their own method quotas. Completion time is not a guaranteed day.
- After an initial all-history scan, a missed Events API callback for a new reply
  to an old thread root can fall outside the recent history scan. Completed
  thread checkpoints are currently discarded. A follow-up should retain a
  content-free registry of known roots and rotate durable replies checkpoints
  through them under the same quota, with a persisted fairness cursor. This
  requires database integration tests before claiming complete offline recovery.
- Bot-origin live callbacks retain the existing bot filtering (Roo is the
  deliberate exception). Group bot history is recovered by history scans;
  immediate bot-message parity is not claimed. Archived history remains readable
  but cannot be sent to Slack; old clients should upgrade to display the
  `source_archived` state before editing or composing.
- Scope/retention restrictions in Slack still limit what can be read. Public
  channel history requires its independently reviewed mapping/import path.
- Actual worker deployment, queue age, Slack app rate tier, and relay read latency
  were not inspected against production. Validate these with aggregate
  instrumentation before asserting production completion or full Slack parity.
