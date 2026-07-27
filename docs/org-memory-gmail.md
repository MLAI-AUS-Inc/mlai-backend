# Organisational memory: labelled Gmail

PR16 replaces Gmail's metadata-only memory registration with a restricted,
read-only adapter for approved organisation-owned mailboxes and explicit
user-created labels. It does not change Public Roo or allow Admin Roo to send,
reply to, delete, or relabel email.

## Source and privacy boundary

- A Gmail memory configuration binds one existing `GoogleConnection` to one
  organisation. That binding is the approved mailbox boundary.
- Discovery returns user-created label IDs and names only. Broad system labels
  such as Inbox, Sent, All Mail, Important, Drafts, Spam, Trash, and category
  labels cannot be selected.
- Content backfill uses Gmail's `labelIds` filter plus the configured historical
  cutoff. Unlabelled mail and messages outside every selected label are not
  hydrated into memory.
- Incremental history events are inspected for current label membership before
  a full body is fetched. If an event omits labels, the adapter performs a
  transient metadata read first. An unselected changed message is not body-
  hydrated or persisted by this adapter.
- A thread is reconstructed only from messages with an active exact-label
  mapping for this configuration. Another message in the same Gmail thread does
  not cross the boundary merely because one message is selected.
- The memory representation includes subject, From, To, Cc, date, cleaned body,
  and selected-label evidence locators. Bcc, Reply-To, raw provider headers,
  OAuth credentials, and raw MIME/base64 payloads are excluded.
- Email whose scope is configured as `internal` is upgraded to `executive`.
  External commitments, commercial terms, contact details, and relationship
  changes are marked as requiring review by downstream extraction policy.
- Personal mailboxes, unlabelled mail, and authentication, medical, HR, or
  legal correspondence remain explicitly out of scope. Do not create or select
  labels that combine these exclusions with approved organisational mail.

## Durable sources and versions

| Durable object | Memory source | Stable identity | Citation unit |
|---|---|---|---|
| Selected Gmail messages grouped by thread | `gmail_thread` | `gmail_thread:<thread-id>` | message ID plus character range |
| Extracted, non-inline attachment | `gmail_attachment` | hash of message, part, and attachment IDs | message ID, part ID, filename, attachment-ID hash |

`GmailScopedMessageArtifact` owns exact current label membership, scan
generation, lifecycle, and the link to the existing durable Gmail message.
`GmailMailboxWatch` stores optional watch health and notification identifiers;
it never stores message content. Thread versions include normalized selected
message content, message/history revisions, selected labels, and the exact ACL
snapshot. Unchanged reruns therefore deduplicate to the same immutable version.

Attachments are emitted only when the existing attachment pipeline has already
produced non-empty `extracted_text` on a non-inline
`GmailAttachmentArtifact`. Memory never copies its `raw_content_base64` field.
Unsupported, failed, inline, and unextracted attachments remain absent until a
later successful extraction schedules another memory wake.

## Operator flow

1. Connect an approved shared or role mailbox with the existing Google OAuth
   flow and the read-only Gmail scope. Do not connect an administrator's
   personal mailbox.
2. Attach that exact `google_connection_id` through the Admin Roo source-control
   API. Registration alone cannot activate ingestion.
3. Discover labels, then select only purpose-specific user labels such as
   sponsor, partner, or event correspondence. Review the label's actual message
   population before approval.
4. Apply the Executive classification or a stricter reviewed policy, set the
   historical cutoff, preview, and dry-run. These operations create no active
   email memory.
5. Approve the immutable preview and request backfill. Keep the memory scheduler
   and worker running until its scan, stale-membership reconciliation, and
   thread emission phases finish.
6. Sample current thread/attachment sources and verify exact message/part
   locators, classification, ACL, exclusion of Bcc/raw payloads, and absence of
   unlabelled messages before expanding scope.
7. Keep `ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400` and
   `ORG_MEMORY_GMAIL_FULL_RECONCILE_SECONDS=86400` unless a reviewed policy is
   stricter. Daily reconciliation is the correctness path even when push is
   enabled.

The backfill cursor contains only scan IDs, label position, provider page
tokens, and the last emitted thread ID. Incremental cursors contain Gmail
history IDs and timestamps. Neither cursor contains bodies or credentials.

## History, access, and deletion reconciliation

- Successful full scans record the current mailbox history ID. Incremental
  runs request message-added, message-deleted, label-added, and label-removed
  history events.
- Gmail history IDs can expire. A stale history request causes an immediate
  resumable full selected-label scan; the recovered cursor is not advanced
  until the replacement scan completes.
- A label removal, message deletion, message falling outside the approved
  cutoff, or completed full scan that no longer sees a mapping marks it
  inactive. Thread and attachment sources no longer represented by any active
  mapping emit access revocations, deactivating current retrieval chunks while
  retaining immutable audit history.
- Clearing the mailbox identity/read scope/refresh credential marks active
  mappings `access_lost` and emits the same retrieval revocations.
- A later approved scan can reactivate a previously removed mapping and capture
  a fresh ACL-aware source version. Hard tombstoning remains reserved for the
  explicit connection deletion lifecycle.

## Optional authenticated Pub/Sub wake

Push is an early wake hint, not evidence and not the correctness mechanism.
Leave the three Pub/Sub settings blank to disable it safely. When configured,
each successful sync calls Gmail `users.watch` with exactly the selected label
IDs and `labelFilterBehavior=INCLUDE`.

```text
POST /api/v1/org-memory/webhooks/gmail/push
Authorization: Bearer <Google-signed OIDC identity token>
```

Configure a Google Cloud Pub/Sub push subscription whose audience is the exact
HTTPS endpoint, and grant its push authentication service account permission to
invoke that endpoint. Set the same audience and service-account email below.
The receiver verifies Google's token signature, audience, email, and
`email_verified` claim before parsing the envelope. It also bounds publish age,
decodes only `emailAddress` and `historyId`, and deduplicates the Pub/Sub message
ID. Its receipt retains hashes and presence flags only; the history value and
source content are not retained in the receipt. A valid event debounces every
active configuration for that exact mailbox, after which the adapter reads
authoritative state from Gmail.

Gmail watches expire within seven days. The adapter renews a configured watch
at most daily after successful content reconciliation; the daily scheduler is
therefore required even when notifications are flowing. PR18 adds consolidated
watch-expiry alerts and provider-wide freshness reporting.

Provider details: [Gmail synchronisation](https://developers.google.com/workspace/gmail/api/guides/sync),
[Gmail push notifications](https://developers.google.com/workspace/gmail/api/guides/push),
and [authenticated Pub/Sub push](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions).

## Configuration

```text
ORG_MEMORY_GMAIL_BACKFILL_DAYS=365
ORG_MEMORY_GMAIL_PAGE_SIZE=10
ORG_MEMORY_GMAIL_CHUNK_TARGET_CHARS=6000
ORG_MEMORY_GMAIL_MAX_MESSAGE_CHARS=50000
ORG_MEMORY_GMAIL_MAX_ATTACHMENT_CHARS=100000
ORG_MEMORY_GMAIL_FULL_RECONCILE_SECONDS=86400
ORG_MEMORY_GMAIL_DEBOUNCE_SECONDS=60
ORG_MEMORY_GMAIL_PUBSUB_TOPIC=
ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE=
ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL=
ORG_MEMORY_GMAIL_PUBSUB_MAX_AGE_SECONDS=86400
ORG_MEMORY_GMAIL_WATCH_RENEW_SECONDS=86400
ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400
```

The backfill/page/text limits are bounded in code. Pub/Sub settings are blank by
default, so an unconfigured public endpoint fails closed and no Gmail watch is
registered.

## Rollout and rollback

The governance manifest still marks Gmail draft/disabled. Record data,
security, privacy/legal, review, and operations owners; approve the exact
mailbox/labels, retention, cost, and terms; then enable `gmail` in
`ORG_MEMORY_ENABLED_PROVIDERS` for a one-label pilot only.

Rollback is fail-closed: pause the configuration or remove `gmail` from the
deployment allowlist. Remove the Gmail watch/Pub/Sub subscription or clear the
three Pub/Sub settings to stop early wakes. Use the normal source-connection
deletion path when evidence must be tombstoned. Do not reverse migration
`org_memory.0015` while a Gmail configuration or watch is active.
