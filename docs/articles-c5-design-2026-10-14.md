# C5: one durable owner notification per actionable goal transition

14 October 2026. Continuous implementation authorisation applies. Implementation
is still pending; this note does not claim delivery is available. Rollout stays off.

Reserve one operation-backed outbox record in the transaction that accepts the
goal lease/state. Identity includes goal, state, required action and its exact run
or PR/source; repeated polls cannot create another notification for that state.
Working/repair/provider waits create none. A genuinely different owner action gets
its own record. A done record links to the accepted live article proof (D1) or ready
goal. Delivery never blocks preparation. Existing website reconciliation retries
outbox work independently, with per-channel durable acknowledgements and bounded
backoff. No new database migration is needed.

The recipient is the saved initiating owner, revalidated against current company
access, active account and connection generation/deletion watermark. Only that
owner's verified active device keys and active email consent are eligible. Never
read recipients, channels, URLs or freeform message text from worker callbacks.
Messages contain the single fixed action and a scoped application link; raw error,
provider response, source, article body, credentials and account email stay out of
Chat content and diagnostic receipts. Superseded/cancelled goals suppress stale
queued notices. An owner’s platform identity must already have valid Chat access.

Native Chat delivery uses existing Nostr DM-open and channel-message events through
POST /events, with an independently configured notifier signing identity. No new
HTTP endpoint or event kind is required. Use h tags for the private DM. Save the
signed message event before posting, so a lost ACK/restart retries the same event
ID. DM-open may be reissued with a fresh command identity: the relay's participant
lookup reuses the same DM and its response supplies the channel ID, while duplicate
command replay otherwise omits that response. Do not invent a DM channel ID or
impersonate a Slack sender through the bridge-only private-delivery API.

Email must use a transport with verified idempotency semantics. The existing Resend
adapter accepts an Idempotency-Key; the Customer.io adapter currently ignores it.
Resolve that difference before claiming exactly-once email delivery. Record only
bounded status/provider references, not response bodies, and keep ambiguous timeout
outcomes distinct from proved rejection. No real message will be sent by local tests
or during implementation; all transports are faked and the new flag defaults false.

Tests must cover working silence, repeated owner/done state, a changed action, crash
before/after provider acceptance, independent channel recovery, current owner/device
consent, foreign company/scope, cancellation and original-event replay. Chat verifies
safe application links and the native private notification presentation.

Provider evidence checked on14 October: [Resend's idempotency contract](https://resend.com/changelog/idempotency-keys)
requires the same key/payload and retains it for24 hours. New email retries must
stop before that window ends; do not issue a new key after an ambiguous timeout.
[Customer.io's transactional send API](https://docs.customer.io/integrations/api/app/tag/send-messages/sendemail/)
does not establish an idempotency guarantee in the reviewed API description.
Until verified otherwise, the new goal email transport must explicitly use the
existing Resend adapter when configured, while native Chat remains independently
retryable. Unsupported or unavailable email configuration is an operational
notification outcome, not a reason to stop the site goal or ask the owner to retry.
