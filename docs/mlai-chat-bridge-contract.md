# MLAI Chat bridge contract

The community bridge provides a live, bidirectional view of selected public
Slack channels inside MLAI Chat. Slack remains usable throughout the rollout;
MLAI Chat is another client surface, not a one-time data migration.

## MVP scope

- Operators explicitly map a public Slack channel to one MLAI Chat channel.
- New messages, replies, edits, deletes, common Unicode reactions (`👍`, `❤️`,
  `🎉`, `👀`, `🚀`, `✅`), and safe Slack shortcode reactions are mirrored after
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
  may fetch the image on demand with the bridge bot and return a bounded,
  private-cache response. The proxy permits only supported image types shared
  in enabled, public Slack channels mapped to MLAI Chat; it does not expose the
  Slack token, store image bytes in bridge records/the database, or fetch
  private/unmapped-channel files.
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
SLACK_DM_MIRROR_SHADOW_SECRET=replace-with-a-long-random-secret
```

For one-click DM linking, add `message.im`, `message.mpim`, `reaction_added`,
and `reaction_removed` under **Subscribe to events on behalf of users** in the
Slack app and keep the same signed request URL used by the bridge. Reauthorize
existing users to add `mpim:write`, `reactions:read`, `reactions:write`, and
`files:read`; file metadata and links are mirrored without requesting
`files:write`. The OAuth callback marks DM discovery due after the new user
token is stored.

Each linked member receives an independent, owner-controlled mirror of every
direct and supported multi-person Slack DM visible to their user token. The
other participants are represented by deterministic shadow keys, so linking
never gives an unconsenting participant access to imported history. A bounded
history scan runs once for every discovered conversation, including mirrors
created before the history marker was deployed. The worker scans one page every
two seconds, persists the oldest timestamp boundary, and honors Slack's
`Retry-After` response without blocking OAuth or Community Home.
Backfill status is complete only after every queued history delivery completes;
transient dead rows are safely repopulated from Slack, while a permanently
rejected adapter delivery stays fenced until explicit backfill or renewed
consent. The `backfill_all` action explicitly switches that
grant to full retained history and omits Slack's `oldest` parameter. The
idempotency key prevents duplicate deliveries.

The backend also starts an hourly bounded reconciliation and immediately starts
one after Slack reports `app_rate_limited`. A message that disappeared from an
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
discovery. MPIMs retain the relay's nine-participant cap: the authenticated or
preferred owner key is included first and status/start responses report when
additional active devices could not fit. An active preferred identity is never
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

- `GET /api/v1/community-chat/slack/` returns delivery-aware backfill,
  identity-repair, device-capacity, and full-history status.
- `GET /api/v1/community-chat/slack/users/?q=...&limit=...&cursor=...` searches
  internal human Slack users. It excludes deleted, bot, app, Slack Connect, and
  owner rows and never returns email addresses or OAuth tokens.
- `POST /api/v1/community-chat/slack/dms/` accepts `slack_user_ids` containing
  one to eight non-owner IDs, calls `conversations.open`, provisions the exact
  private MLAI conversation, and returns its participant public keys and
  sanitized profiles. The owner key always comes from the authenticated active
  verified Community Chat device; body-supplied owner keys are ignored.
- `PATCH /api/v1/community-chat/slack/` accepts `pause`, `resume`, `backfill`,
  and the explicit unbounded `backfill_all` action.
- `DELETE /api/v1/community-chat/slack/` revokes local consent and Slack token
  access, erases queued private bodies, then makes one best-effort call to the
  adapter's authenticated, idempotent
  `DELETE /v1/private-conversations/{channel_id}`. Success means that the local
  privacy boundary is durable. Every provisioned mirror is recorded in a
  content-free durable cleanup ledger; any remaining registrations or adapter
  failures are retried by periodic reconciliation without restoring Slack
  access or retaining message bodies.

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
