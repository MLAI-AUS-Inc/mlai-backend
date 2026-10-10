# Backend documentation index

Start with the repository [`README`](../README.md) and
[`ARCHITECTURE`](../ARCHITECTURE.md). Use this index to find current
subsystem-specific contracts and runbooks.

## Backend maintenance

- [26 September release integration](backend-refactor-release-2026-09-26.md) — current-main fixes, validation and pending migration approval
- [26 September migration proposal](backend-refactor-migration-proposal-2026-09-26.md) — empty graph merge and exact disposable-test inventory
- [15 September release preparation](backend-refactor-release-2026-09-15.md) — historical integration and test scope

- [Runtime and dependency contract](backend-runtime.md)
- [Feature lifecycle register](feature-lifecycle.md)
- [14 September refactor audit](backend-refactor-audit-2026-09-14.md)
- [Refactor implementation status](backend-refactor-implementation-2026-09-14.md)
- [Credential migration and disposable-test scope](backend-refactor-migration-proposal-2026-09-14.md) — local validation complete; production application of credential migrations 0039/0040 approved, rollout pending

## Production API

- [Backend API web handoff](backend-api-web-handoff.md) — verified ingress,
  first adoption, code-only route flips, rollback, and migration limits

## Content Factory

- [Article performance health evidence](article-performance-health.md) — publication age, scoped competitor observations and report metric provenance
- [Topic picker research metrics](content-topic-metrics.md) — difficulty provenance, dated trends and sparse research persistence

- [Editorial and onboarding contract](content-factory-editorial-contract.md) — approved audience/offer persistence and drafting during website setup

- [Content islands](content-islands.md) — paid topic research, measured island suggestions and scoped article ideas
- [Daily article recommendations](daily-research.md) — fresh daily topics, one-use founder preferences and pausing after three unanswered days

## Community chat APIs

- [Valley remote MCP](valley-mcp.md) — client installation, scoped OAuth consent, private narrative handoff and rollout

- [Private member onboarding](community-chat-onboarding.md) — email signup,
  adults-only admission, private preferences and committee review

- [Account privacy controls](community-chat-account-privacy.md) — versioned AI
  consent, deletion-request receipts, and the remaining release gates

- [My startup API](my-startup-api.md) — Chat-authenticated Vibe Marketing, preview scoping, account handoff and rollout switches

- [`community-chat-administration.md`](community-chat-administration.md) — account roles and Moderator appointments

- [`community-chat-account-profile.md`](community-chat-account-profile.md)
- [`community-chat-test-results-2026-09-07.md`](community-chat-test-results-2026-09-07.md) — disposable backend regression evidence
- [`community-chat-home.md`](community-chat-home.md)
- [`community-chat-roo-dm.md`](community-chat-roo-dm.md) — owner-scoped Slack Roo chat
- [`community-chat-coworking.md`](community-chat-coworking.md) — signed booking handoff to Public Roo
- [`volunteer-api.md`](volunteer-api.md) — gated member journey, recognition and Roo contracts

- [Startup settings profile contract](startup-settings-profile-contract.md) — sparse profile saves, reviewed research, logo branding and scoped Chat facade

## MLAI Chat bridge

The bridge contract describes the live MLAI Chat integration. Dated staging and
release evidence may describe earlier deployment stages; verify current runtime
state when using those operational documents.

- [`mlai-chat-bridge-contract.md`](mlai-chat-bridge-contract.md)
- [`slack-unfinished-conversation-recovery.md`](slack-unfinished-conversation-recovery.md) — reviewed recovery of existing unfinished private mirrors
- [`mlai-chat-bridge-staging.md`](mlai-chat-bridge-staging.md)
- [`mlai-chat-membership-bootstrap.md`](mlai-chat-membership-bootstrap.md)
- [Server inbox account bindings](mlai-chat-inbox-accounts.md) — opaque HMAC grouping, verification, revocation and dry-run backfill
- [`mlai-chat-release-runbook.md`](mlai-chat-release-runbook.md)

## Organisational memory

The `org-memory-*.md` documents describe governance, providers, ingestion,
retrieval, review, publication, runtime behavior, and rollout evidence. Begin
with [`org-memory-runtime.md`](org-memory-runtime.md) for runtime boundaries and
[`org-memory-pilot-rollout.md`](org-memory-pilot-rollout.md) for the controlled
rollout sequence.

## Reconciliation and scheduled work

- [`jobs-daily.md`](jobs-daily.md)
- [`stripe-xero-reconciliation.md`](stripe-xero-reconciliation.md)
- [`humanitix-xero-reconciliation.md`](humanitix-xero-reconciliation.md)
- [`reconciliation-knowledge-export.md`](reconciliation-knowledge-export.md)
- [`xero-statement-reconciliation.md`](xero-statement-reconciliation.md)
- [`monthly-update-reminders.md`](monthly-update-reminders.md)
- [`update-covers.md`](update-covers.md) — founder-selected cover uploads and GPT Image 2.5 generation

## HealthHack

- [`healthhack-scoring-data.md`](healthhack-scoring-data.md) documents the
  private scoring-data boundary and runtime provisioning contract.

## Roo

- [Roo rate-card reads](roo-rate-card.md) — authentication, active rates, and empty/error responses
- [`meeting-room-booking.md`](meeting-room-booking.md)
- [`coworking-booking.md`](coworking-booking.md)
- [`office-manager.md`](office-manager.md) includes the backend-first rollout,
  scheduler health chain, rollback/drain procedure, and the mandatory
  read-only audit for historical Roo migration identities `0029`–`0036`.
- [`roo-linear-channel-issues.md`](roo-linear-channel-issues.md)
- [`slack-founder-actor-migration-recovery.md`](slack-founder-actor-migration-recovery.md)

Linear meeting-action reviews are stored by the internal, Roo-authenticated
`/api/v1/integrations/linear/action-batches` endpoints. Batches contain 1–20
requester-bound proposals, expire after
`LINEAR_MEETING_ACTION_BATCH_TTL_SECONDS` (24 hours by default), and delegate
approved work to the existing idempotent Linear issue writer. Deploy migration
`integrations.0042_linear_meeting_action_batches` and these endpoints before
the Roo release that renders durable review buttons.

## Document status

Documents in this directory are subsystem references, not a replacement for
the repository-level setup and architecture. Dated pilot evidence and rollout
documents may describe a particular deployment stage; check their status and
the current code before treating a rollout step as complete.

Files under `plans/` are proposals or implementation history unless explicitly
identified as current by a maintained architecture document.

- [Monthly update evidence and revision contract](monthly-update-evidence-contract.md)

- [Startup Progress dashboard and chart disclosure](startup-progress.md)

- [Chat startup updates](community-chat-startup-updates.md): founder setup, draft, review, approval and community API.

- [Website connection lifecycle and repository consent](website-connections.md) — schema, API, revocation, reviewed cleanup, and local verification.
