# Account privacy controls — pre-release implementation

Migration `community_chat.0011_account_privacy_controls` was specifically approved
by the user on 20 September 2026, including applying the migration closure in a
disposable local test database. No production migration or deployment has occurred.

## API

Both endpoints require a Chat account session. Bootstrap credentials cannot change
privacy preferences. Cookie writes retain the session-bound Origin check. The user
is derived from authentication, and unknown body fields are rejected.

- `GET /api/v1/community-chat/account/ai-consent/`: current public recipients,
  disclosure version, provider digest, effective permission and whether an older
  permission can still be withdrawn.
- `PUT` at that URL: `granted` boolean. Granting additionally requires the exact
  `version` and `provider_digest` displayed by GET. Withdrawal does not require a
  current disclosure. Changes take the user/session locks and recheck revocation,
  expiry, auth version and access-token rotation under the locks.
- `GET /api/v1/community-chat/account/deletion/`: configured timeframe/contact
  and the caller's recent receipts.
- `POST` at that URL: `scope` (`shared_mlai_account` or `chat_data`),
  `policy_version` (`2026-09-20`), and `confirmed: true`. A new request requires
  a session created in the last ten minutes; refreshing a token does not count as
  reauthentication. Retrying an open request returns the same receipt.

Receipts accurately say `requested`; recording a request does not delete data,
deactivate the account, revoke credentials or claim completion. The read-only
admin queue exposes request status and dates without letting staff manufacture
consent or mark cleanup complete. Keep the deletion feature unconfigured until an
operational owner and real cleanup process are established.

## Configuration

- `COMMUNITY_CHAT_AI_DISCLOSURE_VERSION`: public version, changed whenever the
  actual data-sharing disclosure changes.
- `COMMUNITY_CHAT_AI_PROVIDERS`: JSON array of objects containing exactly `name`,
  `purpose`, `data`, and an HTTPS `privacy_url`. Names/data must be verified against
  the deployed Roo service. Empty/invalid disclosure prevents new permission grants.
- `COMMUNITY_CHAT_AI_CONSENT_REQUIRED`: defaults to true. Disabling this is not an
  App Store compliance solution and must not be used to make review accounts special.
- `COMMUNITY_CHAT_DELETION_TIMEFRAME` and `COMMUNITY_CHAT_DELETION_CONTACT`: the
  operator's real completion commitment and responsible contact. Both are required
  before the deletion endpoint accepts requests. No timeframe is guessed by code.

## Implemented AI boundary

Opening the configured Public Roo Slack DM requires current consent. Private
outbound Slack deliveries check consent again during dispatch, including retries,
threads, edits and media-bearing messages. Without consent, recipient membership is
refreshed rather than trusted from a cached mirror: writes to any Roo-visible
conversation are blocked, including untagged channel messages. Unknown membership
fails closed. Deletes remain possible after withdrawal. The dispatch holds the
same user-first lock as consent changes, so a withdrawal serializes with in-flight
outbound I/O. Already-sent Slack data cannot be recalled by withdrawing consent.

The legacy coworking chat command also requires an account-backed identity and
current consent. The worker resolves the signed device again under the account
lock before posting the Roo-visible Slack root and before each direct Roo call.
Withdrawal between those sends blocks the second send; checkpointed retries do
not reuse an old consent decision. Legacy key-only identities cannot opt another
account into AI. The deterministic `coworking/today/` API does not call AI and
does not require AI consent.

Public Slack delivery of Chat-origin messages, replies, edits, attachments and
reaction additions also requires current account consent. Public Slack history
can be used as Roo context without a mention, so this boundary does not infer
permission from message text or bot membership. The worker resolves the signed
device to its account, rechecks that identity under the user lock, and holds the
lock through each external write. Queued retries use the current disclosure and
permission; unknown/legacy identities and changed recipients fail closed.
Deletion and reaction removal remain available after withdrawal. Slack-to-Chat
imports and Discord-origin bridge deliveries are outside this Chat-account gate.
When provider disclosure is unconfigured, ordinary Chat messages remain in Chat
but their public Slack delivery is blocked by the existing retry/dead-letter
policy. Operators must disclose this sharing scope before enabling grants; do
not disable consent enforcement to make the outbox drain.

## Gates before release

This is **not a complete account-erasure or universal AI-consent implementation**.

1. Implement and verify cleanup of account dependencies protected by foreign keys,
   relay messages/media, devices, sessions, bootstrap proofs, Slack OAuth grants,
   other MLAI services, logs and backups. Failed external cleanup must remain
   pending; completion must leave only a non-identifying receipt.
2. Apply consent enforcement to native relay agents and other AI entry points,
   plus downstream Roo context and retrieval. Public/private Slack and legacy
   coworking gating do not retract previously imported context or establish
   permission for other authors' data. Coordinate with the actual Roo deployment
   before enabling it. Discord-origin content needs its own identity/consent model.
3. Confirm provider recipients and deletion ownership/timeframe. Update the public
   policy and App Store privacy labels from those facts.
4. Validate locking/concurrency on PostgreSQL and test the coordinated release on a
   physical iPhone. SQLite tests cannot establish PostgreSQL concurrency behavior.
5. Deploy the reviewed backend migration and app together only after these gates
   are satisfied. The client fails visibly when privacy endpoints are unavailable.

No production account, message, token or credential was used in local tests.
