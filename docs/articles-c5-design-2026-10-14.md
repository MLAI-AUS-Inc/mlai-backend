# C5: one durable owner notification per actionable goal transition

14 October 2026. Implemented behind ARTICLES_GOAL_NOTIFICATIONS_ENABLED=false and
server goal enrollment. No migration, actual message or production operation.

The transaction accepting a prepare lease also records a bounded state-entry
observation and reserves a unique goal-notification operation. Repeated polls are
inert; working-to-needs-you, a different action or done creates a new entry. A lost
lease reserves nothing. Working, repairs and provider waits create no message.
The existing website reconciler independently leases pending notices and expired
running notices, so worker restart cannot leave delivery abandoned.

Each external stage rechecks the active notification lease, refreshed connection
scope/generation/deletion watermark, current latest goal, state entry, initiating
owner and current company ownership. It queries verified nonrevoked Chat devices
and active owner email consent. Cancellation, successor goals, foreign scopes and
revocation fence stale work. A site claim in flight retries its observation rather
than sending or permanently cancelling it. Preparation and delivery have separate
leases; provider failure does not restart or stop website setup.

Native Chat uses existing Nostr kinds41010 (DM open) and9 (channel message), with h
tags for the private conversation and fresh payload-bound NIP-98 authentication.
ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY is an independently registered first-party Chat
member identity, never a customer or Slack bridge key. No endpoint, event kind or
permission bypass was added. DM open can be reissued with a fresh command identity
to recover a lost response; the relay's exact participant lookup reuses the DM.
The signed message is saved before posting and every retry uses its original event
ID. Chat and email acknowledgements are checkpointed independently. A revoked Chat
recipient blocks that channel without blocking consented email. More than8 verified
devices is an explicit unsupported Chat audience (the relay's9-member limit includes
the sender), never a silently truncated or public conversation.

Email uses the existing configured Resend service, with the entire payload frozen
before dispatch and a stable Idempotency-Key. Retries stop at23 hours; an ambiguous
outcome becomes uncertain and never receives a replacement key. Sent/skipped/blocked
channels are not resent. An unavailable provider is recorded as an operational
notification outcome, without asking the owner to restart the site goal. Provider
responses, credentials, error text and source content are excluded from messages
and diagnostics. Channel failure classes remain distinct from site-goal budgets.

Notice content is fixed action text and a scoped Chat link. Completion requires
fresh source-bound proofs; D1's first-article completion also supplies the exact
first_article_live_receipt whose digest matches the final proof and whose public
HTTPS URL belongs to the connected domain. That URL is the completion link. This
handoff is tested, but D1's publisher remains separate work.

Chat recognises canonical first-party goal links and navigates inside its main
TanStack router, preserving explicit company and goal IDs. It retains the external
link policy for other destinations. The startup sub-app's separate React Router is
not used for message navigation. Both left-click and the context-menu action work.

Validation so far:18 focused notice/delivery cases,35 routing/wake/observability
cases and98 broader goal/prepare/permission cases passed. A crash after real signing
and simulated provider acceptance retries the same message ID; accepted email timeouts
retry the same frozen payload/key; independent channels, disabled rollout, owner,
source, device and lease fences are covered. All19 DB-free CI commands passed before
the final focused refinements; final checks are recorded in the progress log.
Chat2 URL units and18 browser cases passed. Full Chat justci is still running.
All transport tests replace the provider; no real DM, email or relay write occurred.

Provider evidence: [Resend idempotency](https://resend.com/changelog/idempotency-keys)
retains the same key/payload for24 hours. [Customer.io's transactional send API](https://docs.customer.io/integrations/api/app/tag/send-messages/sendemail/)
did not establish deduplication in the reviewed contract, so this new outbox does
not silently fall back to its non-idempotent existing email adapter.
