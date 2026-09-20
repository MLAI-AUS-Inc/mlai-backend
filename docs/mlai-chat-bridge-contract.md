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
- Chat-origin writes into public Slack require current account AI-sharing
  permission, since that public history can be used as Roo context. Creates,
  edits and reaction additions recheck consent at dispatch, including retries;
  deletion and reaction removal remain possible after withdrawal. Missing
  disclosure or a legacy key-only identity blocks the outbound delivery under
  the normal retry/dead-letter policy. Local Chat delivery is unaffected. See
  [account privacy controls](community-chat-account-privacy.md) for remaining
  downstream context and native-agent gates.

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

Slack progress and read snapshots use a separate polling budget: 120 requests
per minute per authenticated device, plus a 600-per-minute account ceiling.
Legacy account sessions without a verified device binding share the device
budget within their account. Mark-read acknowledgements have an independent
60-per-minute account budget, so background polling cannot block a user's read
action. These API budgets do not change Slack provider admission or fairness.

Per-conversation refresh status counts only deliveries for the current audience.
Cancelled deliveries retained as tombstones after an old room was replaced do
not keep its replacement in an error state. Current-room failures remain visible;
this status check neither replays cancelled deliveries nor changes source reads.

- `(source platform, receipt key)` is the ingestion idempotency boundary.
- Public Slack-to-Buzz creates also deduplicate by mapped destination and Slack
  channel/message identity. Live callbacks and history scans hold the same
  mapping lock before checking existing creates, deletion tombstones and message
  links. A different callback receipt cannot create another copy or replace a
  failed delivery's frozen request. A later explicit thread broadcast retains
  its separate channel representation once; repeat scans do not recreate it.
  Edits, reactions and explicit operator repair queues keep their own paths.
  This prevents new duplicates; existing delivered duplicates require reviewed
  reconciliation that preserves replies and reactions, not deletion by text.
- A durable outbox is claimed transactionally and retried with bounded backoff.
- Receipt creation, outbox creation and receipt status commit in one transaction.
  A crash before enqueue rolls back the receipt so Slack can retry.
- Public Buzz deliveries checkpoint their complete adapter request in the
  reserved `_buzz_envelope_v1` outbox payload field before the first network
  send. Retries reuse that request, including attribution and timestamp, even
  after an uncertain acknowledgement or an author profile change. This uses
  the existing public outbox schema; private message storage remains encrypted.
- Public and private delivery loops run independently. A slow private batch
  cannot delay public delivery. This lane separation does not yet provide the
  durable per-conversation fairness scheduler described in the reliability plan.
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
- Public Buzz edits/deletes whose create has not yet been mapped use the
  dependency queue. Completing the create wakes both replies and mutations.

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

Private message queues preserve Slack `<@USER_ID>` entities until delivery.
Single messages, threaded replies, edits and batches resolve names from that
conversation's participant profiles without a new profile lookup or a change
to recipients. Spaces in the rendered `@Full Name` use non-breaking spaces so
mobile treats the name as one mention. Missing profiles retain the source
entity instead of replacing the identity with `@user`; code literals remain
literal. Slack `<tel:number|label>` entities remain intact for client-side
telephone-link rendering.

Normal authorized history refreshes repair previously delivered lossy mentions
from the current Slack source text. Each repair is an idempotent edit of the
existing mirrored message, retains the source revision timestamp, and passes
the existing consent, participant and stale-mutation checks. It does not expand
the history window. Completion records `mention_format_version: 1` only for new queues whose
entities resolved; legacy pending bodies and unresolved IDs remain eligible for
a later source refresh. Queue bodies are still erased on completion. No database migration or operational repair is required.

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
Optional author avatars are checked against the adapter's HTTPS Slack CDN and
Gravatar host rules at delivery time, including cached profiles and batches.
An unsupported or malformed avatar is omitted so it cannot reject the message.
After a completed authoritative recovery scan, absent legacy failed rows without
a permanent-failure flag become content-free superseded tombstones; a missing
JSON key must be handled explicitly rather than treated as Boolean false.
During a rolling deployment, a backend that reaches an older adapter falls
back to the same deterministic single-delivery endpoint; an adapter that
reaches an older relay returns a retryable upstream failure until the relay is
updated.
Backfill status is complete only after every queued history delivery completes;
transient dead rows are safely repopulated from Slack, while a permanently
rejected adapter delivery stays fenced until explicit backfill, renewed consent,
or the narrowly verified legacy reaction compatibility recovery documented below.
New API callers that omit a history choice default to 30 days.
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

For bounded imports, discovery checks source activity before creating a new
mirror, loading its participant profiles, or queuing history. Known latest
message/reply timestamps are used directly. When absent, `conversations.info`
and, if necessary, one `conversations.history` result with `oldest` set to the
chosen cutoff establish activity. Only timestamps are cached in the existing
connection cursor: recent checks for five minutes, quiet checks for one hour,
scoped to the exact grant, OAuth generation, consent generation, owner,
workspace and history window. Unknown/error results are never cached as empty.
Slack does not offer a last-active filter on `users.conversations`, so directory
pagination is still necessary. A throttled page saves its completed prefix and
resumes at the unfinished conversation after the shared Retry-After cooldown.
Nested member pages, completed membership and sanitized profile lookups also
checkpoint independently in that connection cursor. A deferral resumes the
next member/user-list page or missing individual profile instead of repeatedly
spending quota on the first page. These checkpoints are scoped to the exact
grant, OAuth/consent generation, selected window, conversation and discovery
cycle. Partial membership is never published; it expires after one hour, while
a completed membership snapshot expires after five minutes. Expiring membership
does not discard profile progress. Current consent, verified devices and final
registration authority are still checked before provisioning. A concurrent
membership update, retirement or room-boundary change invalidates cached
membership and fences any in-flight directory write. Budget deferrals
use the provider's actual retry delay and the existing fair owner rotation;
ordinary failures retain their separate backoff. No quotas are raised.
Durable discovery dispatches at most once per second per worker, including when
idle, and successful partial-directory turns become eligible after one second.
The existing workspace/owner rotation, per-grant leases and shared provider
admission still choose when actual source requests may run. Provider cooldowns
and ordinary-error backoff are never shortened. Legacy discovery and adapter
registration-cleanup maintenance retain their five-second cadence. This removes
fixed dispatch idle time; it does not increase Slack quotas or promise an import
completion time.
An owner whose initial directory-list request loses shared-budget admission
keeps its previous fair turn while waiting for the budget deadline. No provider
request has run in that case. A successful list followed by a nested deferral,
or an actual provider rate-limit response, consumes the turn normally. This
prevents a one-second worker cadence from repeatedly favoring the same owner
at a three-second shared admission boundary.

Existing quiet mirrors still refresh their membership/device boundary; their
stored history remains available without scheduling regular archive scans.
Staged live events bypass quiet-activity suppression so a conversation can
become active again. The clients default private channels, group chats and DMs
to 30 days of source/local message activity, preserving pinned/starred, unread,
and currently open conversations. Public community channels remain browsable.
“Show older” reveals already imported conversations and does not widen import
consent. Importing previously unmirrored older conversations requires the
explicit all-history option. Older stored messages are not deleted by this
inbox visibility policy.

Recent replies on old roots count when exposed through Slack activity hints or
live events. A missed reply on a root outside the history window is subject to
the recovery limitation below; a one-message activity probe is not a complete
thread-history scan.

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

`POST` accepts `{"history_days": 30}` (default), `{"history_days": 7}`, or
`{"history_days": 0}` with explicit all-history consent. Other values return 400. It either activates the existing sufficiently scoped user
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

### Native image requests and account unread state (September 2026)

The authenticated `link-preview/image/` endpoint accepts image-only `Accept`
headers, including the header shipped in iOS 1.0.0 (19). Binary success responses
retain their validated image MIME type; authentication and download errors remain
JSON. Slack timeline images use an uncropped `thumb_1024` (or the next available
720–1024px rendition), with the animated original retained for GIFs.
`?slack_file=F…&original=1` retrieves full resolution for the viewer. Authorization
is checked before either cache; account/workspace scopes and rendition keys remain
separate. No Slack credential or private download URL is returned to clients.

`GET slack/?read_state=1&cursor=0` returns `channels`, `next_cursor`, and
`retry_after_seconds`. Each channel has `available`, `last_read`, `latest_ts`,
`is_unread`, `unread_count`, `count_source`, and `fetched_at`. Missing source state
is unavailable, not zero. `channel_ids=<up to four comma-separated MLAI IDs>`
prioritizes visible rows without waiting for a large directory scan. The service
uses the requesting member's Slack user token and existing consent/device fences;
it never substitutes a bot's read cursor. Public mappings also require source
membership, and private mappings remain restricted to the provisioned device.

A shared Slack budget deferral or cooldown returns HTTP 200 with completed
snapshots and `next_cursor` pointing at the first unfinished target. The response
retains cached bootstrap badges and includes the retry delay. If a group's history
lookup pauses after its info lookup, that group remains unfinished; no empty or
zero-count snapshot is fabricated. Authorization failures still reject the read.

Private cursor sweeps exclude old or unknown directory entries outside the
selected 7/30-day window, except current explicitly opened empty IMs. Recent
pending imports remain eligible for read-state prewarming. Shared public targets
are unchanged. Explicit mark-read operations retain the full authorized target
set and source/device/consent checks, so a new displayed message can be acknowledged
before discovery updates its conversation activity.

Snapshots record `fetched_at` before the source read request starts, so a slow
read cannot overwrite a newer acknowledgement. Join, leave, topic and other
control messages never become a readable latest-message frontier.

Source cursors use Slack's microsecond timestamps. IM counts come directly from
`unread_count_display`. Slack does not supply that count for other conversation
types. Other channel/group badges inspect an unread source history page. This avoids
waiting for the message import, and completed private delivery bodies are
intentionally erased. This probe respects the grant's history window and
stores only cursor/count metadata. Thread-only replies and the owner's own
messages do not create ordinary channel unreads. A truncated/consent-limited page
returns an unknown numeric count rather than claiming a complete total.

With `MESSAGE_SYNC_ENABLED`, the independent `read_state` worker lane refreshes
snapshots while all clients are closed. It rotates workspaces and owners under
durable 120-second leases in the existing connector cursor, then resumes each
account by stable Slack conversation ID. Only active consent and currently
verified/provisioned devices qualify; old/out-of-window conversations do not
consume the sweep. A budget pause retains the target and provider Retry-After;
a source error advances past that target so it cannot stall the whole account.
Group info/history pauses retain only allowlisted cursor metadata for 30 seconds,
never message text. Expired worker claims and changed consent reject provider
calls and cache writes.

The client endpoint reads the shared account cache only and returns all known
snapshots in one response (`next_cursor: null`, `retry_after_seconds: 10`). Opening
multiple clients therefore does not multiply Slack requests. Missing snapshots
remain unknown. `authorized_channel_ids` gives the complete current target set;
clients remove revoked targets but retain a known snapshot omitted by a cold cache.
`snapshot_complete` describes cache coverage, not completion of message import.
Cache retention is 24 hours; the worker rechecks snapshots after
60 seconds, with actual freshness dependent on directory size and shared API
capacity. Visible rows use the same source snapshot as every other device.
Deployments with message sync disabled retain the legacy foreground pagination.

`conversations.info`, `users.conversations` and `conversations.mark` use Slack's
documented Tier 3 allowance (one admission per 1.2 seconds).
`conversations.members` and `users.info` use Tier 4 (one per 0.6 seconds).
All allowances are shared by app/workspace/method; Retry-After always wins. The worker
health check now requires a fresh `read_state` heartbeat too.
These are polled snapshots, not Slack's first-party real-time unread feed. Custom
Slack notification preferences and subteam notification counts are not exposed
by this API, so complete first-party badge parity cannot be guaranteed.
See [Slack conversations.info](https://docs.slack.dev/reference/methods/conversations.info/)
and [Slack's RTM availability](https://docs.slack.dev/tools/node-slack-sdk/rtm-api/).

### Read freshness and device continuity

Visible-chat requests enqueue bounded metadata-only hints for the background
worker; they do not call Slack. New authorized Slack message activity and known
unreads receive priority. Three priority turns alternate with one oldest-first
background turn; the existing owner/workspace fairness and shared method budget
still govern admission. A failed target has its own 60-second retry fence and
does not pause an entire account. A secondary history/replies quota deferral
pauses only that conversation for at least 15 seconds and its reported delay;
independent DM info requests can continue. Visible hints expire after 90 seconds, activity
hints after five minutes, and each account retains at most 256 hints.

Every source observation and confirmed write has a monotonically increasing
`revision`. Full directory responses have a separate `directory_revision` under
the same authority lock. Clients reject older responses, including stale unknown
states and stale directories that could restore removed targets. A coalesced
notification outbox uses connection JSON and sends bridge-signed ephemeral kind
20003 to current device keys through `POST /v1/read-state-notifications`. It
contains only recipients and revision. Clients refetch the authenticated backend
snapshot on that hint or reconnect, with foreground polling retained as recovery.
Notification failure retries without blocking source reads or confirmations.

`MESSAGE_SYNC_STABLE_PRIVATE_ROOMS` requires the relay's additive migration 0031
and adapter audience protocol to be deployed first. Its default follows
`MESSAGE_SYNC_ENABLED` when unset; explicitly set false for a staged rollout.
With it enabled, device replacement retains the private room UUID, imported event
IDs and source checkpoints. A bridge-only kind 41014 command compares the exact
current audience and generation before replacing membership. Adapter retirement
rotates the durable relay generation; a late timed-out request cannot reverse it,
even if the device set later returns to its old values. Adapter delivery/callback
leases drain before the transition, and registry persistence fails closed.

Backend registration, consent and verified-device locks still fence each update.
Before promoting a reused room, ambiguous create/reaction deliveries are reconciled
by their stable bridge delivery ID. Found receipts retain the original event ID
and are not sent again with new device tags. Normal human DM membership remains
immutable. Explicit import-window resets and revoked Slack consent retain their
existing reset/erasure behavior. No new Django migration is required.

The 1–2 hour import target must be evaluated against conversation count, message
pages, thread pages, simultaneous owners, observed request rates and Slack's
distribution tier. Thirty days alone is not a size limit. Initial discovery can
need a provider probe for every historical conversation that lacks an activity
timestamp. At 50 requests/minute, 6,433 such probes alone have a lower bound of
about 129 minutes before other users, history pages, retries or throttling.
Extra workers or IP addresses do not increase the shared Slack allowance. Use
the metadata-only capacity tool and production telemetry to report an honest
bound; never present a synthetic replay as a measured fresh-account Slack import.

For example, model four owners sharing one app/workspace, each needing 20
directory pages, 500 info probes, 500 history pages and 100 reply pages:

```sh
python manage.py message_sync_capacity --owners 4 --directory-pages 20 \
  --info-probes 500 --history-pages 500 --reply-pages 100 --import-share 0.5
```

This command performs no provider or database requests. Supply measured workload
counts; its output labels the quota floor separately from an import ETA.

`PATCH slack/` with `{action: "mark_read", channel_id, source_ts}` advances the
owner's Slack cursor through a displayed source message. Successful responses
include `synced`, `last_read`, the server's `confirmed_at` timestamp and the same
`channels` snapshot made immediately available to every device. A proven later
unread retains an indicator with an unknown numeric remainder. An ambiguous head
(including the owner's own post or an ordinary thread reply) returns unavailable
state and requests a fresh source check; clients retain their prior snapshot
instead of inventing zero. Clients compare snapshot revisions when merging
responses, falling back to `max(fetched_at, confirmed_at)` for older servers.
Confirmed acknowledgements reject snapshots fetched before confirmation;
a subsequent source cursor regression can represent an explicit Slack mark-unread.
With durable sync enabled, an authenticated read intent is saved to the existing
connection cursor before source I/O, coalesced per device to its highest requested
timestamp. Exact device ID and verification generation prevent same-key
re-enrollment from replaying old reads. Revoking a device does not discard another
device's lower valid frontier.
Budget pauses return `{synced: false, pending: true, retry_after_seconds: ...}`.
Pending intents never clear badges. A failed intent backs off independently and
cannot starve other unread refreshes. The background unread lane retries them even
after the app exits and removes only the confirmed generation/frontier. Retries
revalidate consent, OAuth identity, write scopes and the requesting verified
device; obsolete intents expire after seven days. No message body is retained.
An in-flight older source fetch cannot overwrite a confirmed read. Ordinary
client polling picks up peer confirmations within its ten-second poll interval
when the service is reachable; this is bounded polling, not a websocket push.
A newer source cursor
is never moved backwards. Missing write scopes return
`{synced: false, needs_reauthorization: true}` without writing to Slack. Existing
IM/MPIM grants already request `im:write`/`mpim:write`; reconnecting Slack now also
requests `groups:write` and `channels:write` for channel read positions. Source
read failures retain the app's local acknowledgement. No schema migration is
required.

Connected clients may POST `refresh_permissions: true` with their current
`history_days` to obtain a fresh Slack authorization URL. This opt-in permission
upgrade preserves the active grant and import until OAuth completes.

### Thread references (September 2026)

Slack message attachments with canonical `original_url`, `from_url`, or
`title_link` permalinks are retained as `Thread` links in both public and private
imports, including edits. Quoted bodies and quoted-author metadata are never
copied from these attachments: a private thread may be forwarded into a public
channel.

The existing authenticated `GET /api/v1/community-chat/link-preview/?url=…`
returns `thread: {channel_id, message_id}` for an available mirrored Slack
message. This is an address only. Clients fetch the original event plus its
edits/deletions with their own relay identity, use the relay's canonical channel
routing after a move, and open that thread within MLAI Chat. Private mappings
require the requesting account's live/paused mirror and non-revoked grant.
Unknown, disabled, or deleted mappings return 422 without public URL scraping.
Thread mapping responses use `Cache-Control: private, no-store`.

To repair older public posts whose reference attachments were dropped, run
`python manage.py backfill_community_bridge_thread_references --slack-channel-id C…`
as an authorized operator. It is a bounded dry run (500 posts by default,
maximum 5000); `--oldest`/`--latest` bound the Slack history interval. Explicit
`--apply --confirm-historical-edits` queues existing-post edits through the
normal bridge outbox. It never creates replacement posts. Deleted posts,
concurrent/pending updates, and already-restored references are skipped. The
report contains counts and a pagination timestamp, never message bodies.

## Durable synchronization rollout (14 September 2026)

Migration `integrations.0047_message_sync_reliability` adds the encrypted callback
inbox, conversation scheduling state, resumable page/thread jobs, shared provider
budgets, heartbeat records and public delivery leases/canonical requests. Apply
this migration before deploying code that reads the new columns. Enable
`MESSAGE_SYNC_ENABLED` only after all bridge worker replicas use this version.

Configure `MESSAGE_SYNC_SLACK_APP_ID`, `MESSAGE_SYNC_SLACK_APP_TOKEN` (an app-level
token with `authorizations:read`) and, when the public bot is enabled,
`MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID`. Slack event recipient expansion is
paginated and checkpointed in the encrypted inbox. Private routing occurs only
after expansion completes; another workspace's installations are excluded.

`MESSAGE_SYNC_SLACK_DISTRIBUTION` defaults to `restricted` (one history/replies
request per minute). Use `internal` or `marketplace` only after verifying that
classification with Slack. Admission and Retry-After are shared by app,
workspace and method across bot/user tokens and worker replicas. Quota updates
use a separate, short PostgreSQL transaction so a rolled-back delivery cannot
refund an upstream API request. Other bridged methods currently use a
conservative tier-2 admission interval. Privacy revocation calls remain exempt.
See [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/) and
[recipient authorizations](https://docs.slack.dev/reference/methods/apps.event.authorizations.list/).

The worker seeds every enabled public mapping and live owner-private
conversation in bounded batches without login. Recent-head, archive and known
thread jobs retain separate checkpoints. Each claim admits one conversation's
page; workspaces and conversations rotate by last served time. Foreground
activity does not override that rotation. Archived thread jobs remain durable
and recur after scan completion. Private pages revalidate the existing grant,
registration, membership and history-window boundaries before their writes.

Public delivery claims admit one row per conversation, with at most four sends
in parallel. The first complete public adapter request is persisted before I/O;
retry uses that immutable request. UUID leases fence stale completion/failure
writes. Private deliveries retain encrypted storage and their consent locks;
claim generations reject results from replaced workers, and quiet conversations
receive turns ahead of more work from recently served conversations.

`python manage.py message_sync_status` reports backlog age, overdue jobs,
expired leases and worker heartbeats without message bodies. A verified callback
is acknowledged only after its encrypted receipt commits; downstream failure
retains it for retry, and successful routing clears its body while retaining the
deduplication identity. Job errors contain bounded machine codes.

These source changes do not certify Slack source parity or deployed readiness.
Public absence-based deletion repair, full discovery pacing, relay ingestion
sequence replay, persistent client bootstrap and the release/soak qualification
are tracked in the cross-repository implementation plan. Operational rollback
stops admission to the new lanes and retains their tables and checkpoints;
never reverse/drop a used sync schema as a routine rollback.

### Distinct public and private Slack apps

MLAI's public bot (`A0BDH1ZG76X`) and owner OAuth app (`A0B0NDG6VL0`) are separate apps. Configure `MESSAGE_SYNC_SLACK_APP_ID` / `MESSAGE_SYNC_SLACK_APP_TOKEN` for the public bot and `MESSAGE_SYNC_SLACK_USER_APP_ID` / `MESSAGE_SYNC_SLACK_USER_APP_TOKEN` / `MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET` for owner OAuth. Each app-level token needs only `authorizations:read`; these credentials belong in deployment secrets. Callback signatures bind the claimed app to its signing secret, and recipient expansion and API budgets use that same app identity. A single-app deployment can omit the user-app overrides.

Deploy callback verification with `MESSAGE_SYNC_ENABLED=false` before verifying a newly configured Slack event URL. The private app's event subscriptions were disabled during the 14 September inspection and must be enabled for live user events after URL verification. Existing users' token scopes and explicit mirror consent remain authoritative; adding a subscription does not authorize additional private access.

The Docker worker health probe runs `message_sync_status --check --local-worker`, checking fresh inbox, history, public-delivery, private-delivery and read-state heartbeats from the current container. Enabled deployments wait for these heartbeats and fail if a previous container is the only worker reporting. Status output contains queue ages, expired leases, source coverage classifications and shared provider cooldowns without message bodies or credentials. History and live delivery run in independent bounded lanes, and source-limited scans report unknown absence instead of claiming empty or deleting records.

### Selected-window publication and retired registrations (September 2026)

Owner Slack import honours the selected 7-day or 30-day window using the original
Slack message timestamp. A recent edit or a delayed callback does not turn an old
message into a newly eligible message. Recent replies may be shown without an
out-of-window parent body. Deletions still remove content previously imported.
Explicit all-history consent remains separate; legacy zero-valued grants remain
bounded. Provider pagination is rechecked before persistence, including after
restarts and changes to the consent window. Source-limited responses preserve
unknown absence: they cannot infer deletions or certify a first complete import.

`channel_catalog` now includes `ready_for_display` and `history_oldest_ts` for
each device-authorized mirror. The latter is a numeric Unix-seconds string, or an
empty string for explicitly authorized all-history. `last_message_at` is source
activity, never relay arrival time. Initial publication requires a completed
selected-window scan, successful delivery of its relevant backfill, an active
current grant and a live conversation with activity inside the selected window.
An explicit open-DM action can qualify an empty conversation after its completed
empty scan, using an intent bound to the exact consent, OAuth generation and
owner device. This does not make a background-discovered empty conversation or
a conversation with actual old activity eligible. Progress counts use the same
durable scan, consent, source-limit and delivery prerequisites.
Clients hide unready imports while preserving native chats. They also filter
cached Slack bodies by the same source cutoff and use full Slack timestamps to
order messages sharing a second.

Once an import is published, a presentation cache bound to the owner, consent,
room, participant hash and selected history window preserves it during ordinary
background refreshes. A new consent or membership boundary invalidates that
qualification. The source activity/window and active grant are checked on every
response. Cache loss fails closed until the import qualifies again; it does not
make the cache an authorization source.

ID-only `ready_for_display: false` catalogue entries fence older registration
UUIDs for the verified owner, including after pause or disconnect and across old
Slack connections. These contain no historical names, participants or
bodies and confer no relay access. Clients must retain these entries as hidden
IDs, rather than counting old relay memberships as native group chats. Group/DM
classification uses Slack's conversation type and people, not device or shadow
identity counts. Missing Slack read snapshots mean unknown unread state; imports
must not fabricate unread badges while those snapshots are loading. No wholesale
Slack mark-read operation is performed by import.

Confirmed quiet conversations skip relay reprovisioning and receive a bounded
background-history cooldown; new callbacks remain independently active. Internal
and Marketplace Slack apps use 200-item history pages, while restricted apps
retain 15-item pages and their existing shared request budget. Larger pages do
not raise the number of allowed requests. Unbounded history omits `oldest`
instead of sending a zero timestamp.

Shared public channel mappings have their own community-wide archive policy and
no owner grant. The selected private-import window applies to every grant-backed
mirror, including private channels; it does not change shared public retention.

### Current-room archive proof

First publication requires archive `import_contract_version=2`, the current
participant hash and channel ID, and complete unrestricted source coverage.
The contract version is recorded when the archive starts. Finishing a resumed
pre-version cursor does not certify pages read under older code. The completed
old scan is followed by a fresh selected-window scan; active leases, partial
checkpoints and provider backoff are preserved.

The fair recovery turn also upgrades known recent conversations missing that
proof, at most one conversation per owner per round. Old and unknown quiet
conversations are not swept. An explicit compose action can refresh an old empty
DM that lacks proof, while preserving any active scan. A source-limited current-version attempt stays
unqualified without causing an immediate rescan loop. The presentation latch
uses a versioned namespace and retains an already qualified view only within
its exact existing owner, consent, device and participant scope.

### Bounded source recovery for failed imports

The durable worker's existing state-seeding tick schedules fresh source recovery
for erased terminal backfill rows. Each round considers at most five owners and
one conversation per owner, with a durable owner turn so a busy or blocked owner
cannot monopolize the queue. Each conversation marks at most 200 rows. Current
grant, scope, consent and registration checks fence the operation. Active leases,
partial history states and between-page checkpoints are preserved; an incomplete
scan finishes before a new recovery begins. No OAuth refresh or message body I/O
runs in this scheduling step.

Scheduling keeps terminal rows terminal and erases any retained old payload. It
marks the selected rows for a fresh scan of the current consent window. Only
source observations can repopulate their bodies through the normal guarded
history writer. After a complete unrestricted scan, remaining absent rows become
superseded tombstones; a source-limited scan leaves them unqualified. Already
scheduled or superseded rows do not continually restart recovery. Rows that have
aged outside consent are recorded as excluded without a provider request.

One explicit compatibility exception addresses private `reaction_add` failures
from the retired seven-emoji adapter allowlist. The successful production rollout
of [chat PR #145](https://github.com/MLAI-AUS-Inc/mlai-chat/actions/runs/34502746861)
completed on 10 September 2026 at 16:52:06 UTC. Recovery requires an exact adapter
HTTP 400 failure before that bound, a standard Unicode reaction accepted by the
current pinned emoji data but rejected by the old allowlist, an in-window source
target, the current participant boundary and a verified registered owner device.
An existing legacy `history_recovery_scheduled` flag can acquire this audited
exception once; staged exceptions remain excluded from repeated scheduling.

The exception preserves the original failure time and fixed error code. The
reaction remains DEAD, permanently fenced and body-free while a fresh archive
records its source metadata. Only completion of that exact unrestricted archive
can reconstruct and release the reaction, or supersede it after qualified
absence. Other scan epochs, source-limited pages, revoked devices, later HTTP 400
errors and all nonmatching permanent failures remain fenced.

A separate diagnosed exception handles a permanently rejected reply whose
completed parent mapping belongs to a retired participant boundary. Target and
outbound-echo lookups require the current boundary; observing an old completed
message in Slack rebuilds its mapping without relabeling the old destination.
Before that replacement, affected failed children retain a content-free audit
of the old parent mapping. Only the exact HTTP 400 / stale-parent condition,
current owner device, registration, consent and selected source window authorize
recovery; ordinary create failures remain fenced. Previously live failures are
included in this narrowly diagnosed repair.

The old failed body is erased first. A new archive may stage a freshly fetched
reply while it remains DEAD and permanently fenced. An unrestricted complete
scan qualifies the reply; normal dependency handling waits for the current
parent and only flattens when that parent cannot progress. Limited coverage
erases staging. A source-body hash prevents retention cleanup from releasing an
empty replacement; a later fresh source observation can recover it. Scalar and
batch delivery keep the existing backend delivery ID: the adapter rebuilds the
signed event with its current channel/audience and deduplicates by event ID, so
a new room cannot reuse an old accepted receipt. Older outbound rows without any
recorded boundary are not relabeled: fresh Slack import may duplicate an old
native root in the same room, because its original destination cannot be proven
from the retained metadata alone.

This path needs no database migration or manual dead-row requeue. Recovery waits
for the existing provider budgets and fair scheduling; a cooldown expiry is not
an import-completion deadline. `integrations.tests_message_sync_recovery` runs in
the durable PostgreSQL CI gate, alongside
`integrations.tests_slack_private_target_boundaries`.

Head and thread repair use the same author admission rules as archive import.
An owner IM does not admit an unrepresented Slackbot author merely because a
history response contains it. Previously queued backfill operations for that
exact system author become content-free superseded records when no registered
source identity can represent them. They do not add recipients or shadow
identities. Human replies can then use the existing unavailable-parent fallback
instead of retrying forever. Unknown human authors retain their failure fence;
group history with an already registered import identity continues to preserve
bot and departed-member attribution.


### Focused Unreads section (September 2026)

Read snapshots expose `has_personal_mention` separately from the existing numeric
badge. It is true only for an unread source post containing the owner's exact
Slack mention entity (`<@USER>` or `<@USER|label>`). Broadcasts such as `@here`,
`@channel`, self posts and already-read posts do not qualify. A truncated page
with no observed personal mention returns null, not a claim that none exists.
A confirmed read covering the latest message clears this flag; a partial read
leaves it unknown pending reconciliation. The flag participates in snapshot
invalidation, so it reaches all devices through the existing owner-scoped feed.
Clients collect unread IMs/MPIMs and personally mentioned channels into Unreads;
ordinary channel activity stays in the regular channel sections.

A client may project a pending read over its retained source snapshot for
immediate feedback. It must retain and retry the intent, never hide messages
newer than the requested source timestamp, restore source state on rejection,
and accept a later deliberate Slack mark-unread after confirmation. This
presentation projection does not alter the authoritative server snapshot.


Slack file preview reads preserve temporary scheduler/provider deferrals as HTTP
503 `preview_pending`, with `Retry-After` and `retry_after_seconds`; they are
`private, no-store`. Clients retry bounded short waits without asking users to
reconnect Slack. Permission/unsupported-file failures remain 422. Both bot and
owner `files.info` reads use the shared app/workspace/method budget (Tier 4,
0.6 seconds between admissions); actual Slack 429 cooldowns take precedence.
Metadata and image caches remain authorization-scoped. Slack-provided PDF/video
thumbnails can use the image proxy; original document/video playback remains in
Slack when no supported preview is provided. Non-image files without thumbnails
produce a usable link card, not a metadata exception.

Successful Slack-file HTTP responses are also `private, no-store`: clients and
servers already cache by account/authorization scope, while a browser HTTP cache
cannot represent a subsequent account's conversation access. Browser and Tauri
CORS expose `Retry-After` without expanding credentialed origins.
