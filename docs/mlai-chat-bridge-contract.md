# MLAI Chat bridge contract

The community bridge provides a live, bidirectional view of selected public
Slack channels inside MLAI Chat. Slack remains usable throughout the rollout;
MLAI Chat is another client surface, not a one-time data migration.

## MVP scope

- Operators explicitly map a public Slack channel to one MLAI Chat channel.
- New messages, replies, edits, and deletes are mirrored after the mapping is
  enabled. Historical backfill is out of scope.
- Direct messages, private channels, huddles, workflow payloads, ephemeral
  messages, and Slack Connect channels fail closed unless separately approved.
- Attachments are represented as safe provider-hosted links. The bridge does
  not copy or fetch arbitrary files during MVP.
- Mirrored messages are visibly attributed to the source author and platform,
  but are signed/sent by a dedicated MLAI bridge identity.

## Canonical event

Every verified provider event is normalized to:

- receipt key and source platform;
- source channel, message, optional parent, and author identifiers;
- one delivery operation: `create`, `edit`, or `delete`;
- sanitized text and HTTP(S) attachment links; and
- non-secret adapter metadata.

Provider-specific payloads stay at the ingestion edge and are cleared under the
raw-payload retention policy. Delete events deliberately retain no message
content.

## Delivery guarantees

- `(source platform, receipt key)` is the ingestion idempotency boundary.
- A durable outbox is claimed transactionally and retried with bounded backoff.
- Message links map source IDs to destination IDs so replies, edits, and deletes
  address the correct provider object.
- Delivery is at least once; adapters must be idempotent for a claimed outbox
  row. Ordering is best effort within one mapped channel.
- Exhausted deliveries enter a dead state for operator inspection and replay.

The backend reaches the Rust sidecar only through its private
`POST /v1/deliveries` endpoint with `BUZZ_BRIDGE_ADAPTER_TOKEN`. The sidecar
posts relay events to
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
bot event `message.channels`, and grant only `channels:history`,
`channels:read`, `chat:write`, `files:read`, and `users:read`. Record the bot
user ID as `SLACK_BRIDGE_BOT_USER_ID` so its create, edit, and delete events are
discarded for loop prevention.

Direct messages, private channels, and payloads marked as shared/external are
ignored in normalization. Operators must also confirm that every mapped channel
is not a Slack Connect channel before enabling it. Rotate the Slack signing
secret, adapter token, callback secret, and bridge Nostr key independently.

## Required backend settings

```dotenv
SLACK_BRIDGE_BOT_TOKEN=xoxb-...
SLACK_BRIDGE_SIGNING_SECRET=...
SLACK_BRIDGE_BOT_USER_ID=U...
BUZZ_BRIDGE_ADAPTER_URL=http://buzz-bridge-adapter:8090
BUZZ_BRIDGE_ADAPTER_TOKEN=...
BUZZ_BRIDGE_CALLBACK_SECRET=...
```

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
