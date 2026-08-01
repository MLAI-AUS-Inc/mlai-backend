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

