# Backend documentation index

Start with the repository [`README`](../README.md) and
[`ARCHITECTURE`](../ARCHITECTURE.md). Use this index to find current
subsystem-specific contracts and runbooks.

- [Private Studio report source](studio-report-source.md) — scoped hours/invoice/payment refresh and missing-data barriers
- [Contacts store](crm.md) — MLAI Luma guest sync, CSV opt-in import, consent rules and the `crm` migration

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

- [Alternate sign-in emails and account repairs](email-aliases.md) — identity resolution, delivery policy, and reviewed merges

- [Backend API web handoff](backend-api-web-handoff.md) — verified ingress,
  first adoption, code-only route flips, rollback, and migration limits

## Content Factory

- [19 October backend consolidation audit](articles-backend-rollup-audit-2026-10-19.md) — retained reliability fixes, deferred prototypes, exact component heads and release boundaries

- [Interrupted article publication observation](articles-publication-observation.md) — bounded positive GitHub evidence, exact original intent and preserved settlement obligations
- [Original queued article admission](articles-dispatch-admission.md) — original target/source checks before retrying paid work and held legacy identities
- [R3 original goal audit](articles-r3-original-goal-audit-2026-10-18.md) — persisted partial owner observations, public unknown counts and remaining external coverage
- [R2 bootstrap memory and optional profiling](articles-r2-bootstrap-memory-2026-10-18.md) — independent extraction, unchanged recycling and scoped disposable CI
- [R4 fixture rollout inspection and refund retention](articles-r4-rollout-refunds-2026-10-18.md) — read-only flags and enrollment diagnostics, unknown CF observations, and credits retained through disconnect
- [E3 original inventory count disclosure](articles-e3-native-inventory-display-2026-10-17.md) — scoped “We found N” observations and immutable producer parity, with no readiness grant

- [F2 bootstrap summary and rendering](articles-f2-bootstrap-summary-2026-10-16.md) — default compact reads, explicit full details, and remaining latency/overlay validation
- [F3 recorded usage and operational alerts](articles-f3-cost-accounting-2026-10-16.md) — original-run accounting, company coverage and durable budget/stall/repair notices
- [G6 preview cleanup deadline](articles-g6-preview-cleanup-2026-10-16.md) — durable 24-hour lifecycle limit and one scoped operational incident

- [Articles B1 failure contract](articles-b1-implementation-2026-10-14.md) — immutable taxonomy, trusted enrollment, emitter coverage and regression/provenance evidence
- [Articles B5 setup delivery](articles-b5-implementation-2026-10-14.md) — durable receipts, pure reconciliation and initial implementation evidence
- [Articles B2 goal runner design](articles-b2-design-2026-10-14.md) — proposed milestone proofs, per-site execution and one backend status for Chat
- [B2 implementation and validation](articles-b2-implementation-2026-10-14.md)
- `inspect_articles_zoo_scope --observation <recorded-night.json>` reads saved original-goal/run ownership in a fresh PostgreSQL REPEATABLE READ, READ ONLY snapshot. It emits only safe IDs and closed binding digests; missing lineage remains unknown. It produces scope evidence only, never runtime eligibility, deployment proof or a qualifying zoo night. Collect before adoption/disconnect changes the original selected connection. No flags, providers or saved state are changed.
- [Continuous one-shot implementation progress](articles-continuous-implementation-2026-10-14.md)
- [E2 hosted attempt and recovery design](articles-e2-build-recovery-2026-10-16.md) — temporary preview branches and gated recovery for scoped partial environment receipts
- [E1 site fingerprint](articles-e1-site-fingerprint-2026-10-15.md) — bounded immutable facts, source validation, strategy/deploy hints and pinned producer parity

- [Articles Phase 2 design approvals](articles-phase2-design-notes-2026-10-12.md) — all eight designs approved; implementation waits for Phase 1 deployment

- [Article performance health evidence](article-performance-health.md) — publication age, scoped competitor observations and report metric provenance
- [Topic picker research metrics](content-topic-metrics.md) — difficulty provenance, dated trends and sparse research persistence

- [Editorial and onboarding contract](content-factory-editorial-contract.md) — approved audience/offer persistence and drafting during website setup

- [Content islands](content-islands.md) — paid topic research, measured island suggestions and scoped article ideas
- [Daily article recommendations](daily-research.md) — fresh daily topics, one-use founder preferences and pausing after three unanswered days

## Community chat APIs

- [Dependency signals and polling callers](community-chat-dependency-signals.md) — source-level G3 tracing for preview, Slack storage and cursor callback waits

- [MLAI remote MCP](mlai-mcp.md) — unified Pulse, Articles and explicit-channel Community capabilities with legacy audience isolation

- [Personal Dot on MLAI MCP](mlai-mcp-native-dot.md) — fresh installation-bound consent, mention events and native replies on the existing connection

- [Personal Maek and Maek · community](mlai-community-agent.md) — separate guest identity, tool-free inference and opt-in rollout

- [Valley remote MCP](valley-mcp.md) — client installation, scoped OAuth consent, private narrative handoff and rollout

- [Private member onboarding](community-chat-onboarding.md) — email signup,
  adults-only admission, private preferences and committee review

- [Device management](community-chat-device-management.md) — same-account
  registration reuse, authenticated activity, naming and protected revocation

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
- [Permanent Slack member operations](slack-member-permanent-operations.md) — member-only strategy, processing pause, reader holds and recovery
- [Member read ownership](slack-member-read-cleanup-inventory.md) — current token boundaries and retained bot writes
- [Member-authorized repair](slack-member-repair.md) — durable, bounded recovery without a legacy replay path
- [Slack member migration: Phase 0 operations](slack-member-phase0-operations.md) — read-only coverage command and owner baseline checks
- [Slack member migration: Phase 3 foundations](slack-member-phase3-operations.md) — disabled configuration, exact migration proposal and rollout gates
- [Slack member migration: Phase 4a operations](slack-member-phase4a-operations.md) — feature-off release, scoped public intake, membership epochs and audience reduction gates
- [Phase 4b member-token history](slack-member-phase4b-operations.md) — current reader/source/audience fences, pagination scope and feature-off rollout holds.
- [Phase 4c Members delivery](slack-member-phase4c-operations.md) — immutable Slack actors/requests, current audience fences and combined Chat release gates.
- [Phase 5 Community posting](slack-community-phase5-operations.md) — scoped member selection, verified-account bot fallback and immutable original-poster retries.
- [Historical Phase 6 source comparison](slack-community-phase6-operations.md) — superseded rollout modes and observation gates; use permanent operations for current behavior.
- [Private Slack first-event routing: Phase 8](slack-event-routing-phase8.md) — owner leases, disabled rollout and confirmed-delivery latency evidence
- [Slack member-sync evidence](slack-member-sync-facts.md) — owner decisions, documented contracts and outstanding live observations
- [`slack-unfinished-conversation-recovery.md`](slack-unfinished-conversation-recovery.md) — reviewed recovery of existing unfinished private mirrors
- [`mlai-chat-bridge-staging.md`](mlai-chat-bridge-staging.md)
- [`mlai-chat-membership-bootstrap.md`](mlai-chat-membership-bootstrap.md)
- [Server inbox account bindings](mlai-chat-inbox-accounts.md) — opaque HMAC grouping, verification, revocation and dry-run backfill
- [Server inbox rollout](mlai-chat-inbox-rollout.md) — default-off flag order, controlled Slack acceptance, account erasure and rollback
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

- [Roo event discounts](roo-event-discounts.md) — private quotes, general-point holds, one-use Luma coupons and supervised reconciliation.

- [`roo-points-gst.md`](roo-points-gst.md) — inclusive GST checkout, payment evidence and release configuration

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
- [Shared update benefits](shared-update-benefits.md): sharing requirements, grandfathered discounts, points and rollout dependencies.

- [Website connection lifecycle and repository consent](website-connections.md) — schema, API, revocation, reviewed cleanup, and local verification.
