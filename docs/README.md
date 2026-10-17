# Backend documentation index

Start with the repository [`README`](../README.md) and
[`ARCHITECTURE`](../ARCHITECTURE.md). Use this index to find current
subsystem-specific contracts and runbooks.

## Backend maintenance

- [26 September release integration](backend-refactor-release-2026-09-26.md) — current-main fixes, validation and pending migration approval
- [26 September migration proposal](backend-refactor-migration-proposal-2026-09-26.md) — empty graph merge and exact disposable-test inventory
- [15 September release preparation](backend-refactor-release-2026-09-15.md) — historical integration and test scope

- [Runtime and dependency contract](backend-runtime.md)
- [G4 worker memory profile](articles-g4-worker-memory-profile-2026-10-18.md) — default-off process samples and disposable Gunicorn lifecycle acceptance; sustained application measurements and tuning pending
- [G4 bootstrap application memory](articles-g4-bootstrap-worker-profile-2026-10-18.md) — equal synthetic application windows across baseline/candidate limits in disposable PostgreSQL CI
- [F2/G4 bootstrap run lifetime](articles-f2-bootstrap-run-lifetime-2026-10-18.md) — release request-owned reverse-prefetch cycles after current projections; application memory comparison passes
- [G4 measured recycle limit](articles-g4-recycle-limit-2026-10-18.md) — 1,000/100 defaults after the 339 MiB application comparison, with CI fixture memory guards and rollback
- [Feature lifecycle register](feature-lifecycle.md)
- [14 September refactor audit](backend-refactor-audit-2026-09-14.md)
- [Refactor implementation status](backend-refactor-implementation-2026-09-14.md)
- [Credential migration and disposable-test scope](backend-refactor-migration-proposal-2026-09-14.md) — local validation complete; production application of credential migrations 0039/0040 approved, rollout pending

## Production API

- [Backend API web handoff](backend-api-web-handoff.md) — verified ingress,
  first adoption, code-only route flips, rollback, and migration limits

## Content Factory

- [D2 trusted source stop history](articles-d2-source-fence-2026-10-18.md) — pre-cancel cause, permanent old-write fence and canonical historical metadata; remote fencing and execution remain pending

- [D2 old publication observation](articles-d2-publication-observation-2026-10-18.md) — current read token, union of old ledgers/references and provider identity checks; owned fencing and execution remain pending

- [D2 stale article inspection](articles-d2-refresh-inspection-2026-10-18.md) — default-off real controller stage, private read-only reservation and bounded producer observations; execution remains pending

- [E5 native CMS provider CI](articles-e5-native-provider-ci-2026-10-18.md) — digest-pinned disposable WordPress/Ghost API acceptance and remote-only fixture isolation
- [E5 explicit CMS withdrawal](articles-e5-cms-withdrawal-2026-10-18.md) — owner-scoped status-only journal, leased execution and public-absence proof; UI/provider acceptance pending
- [E5 initial CMS intake](articles-e5-cms-initial-intake-2026-10-18.md) — initial owner entry, explicit platform preference and original acceptance recovery

- [E5 initial CMS runtime](articles-e5-cms-initial-runtime-2026-10-17.md) — one original acceptance/hold through encrypted verification, expiry, lifecycle and native setup
- [E5 initial CMS onboarding](articles-e5-cms-onboarding-2026-10-17.md) — initial owner offer and same-operation credential/payment/setup design
- [E5 durable CMS access recovery](articles-e5-cms-durable-access-2026-10-17.md) — encrypted acceptance before HTTP, original payment/post preservation and bounded independent expiry
- [E5 owner CMS access form](articles-e5-cms-owner-access-2026-10-17.md) — scoped credential destination and actual accepted request nonce without key export or provider replay

- [E5 original CMS credential renewal](articles-e5-cms-credential-renewal-2026-10-17.md) — same principal/site grant preserves original paid source, private key identity and single-create journal

- [E5 old-policy access recheck](articles-e5-cms-policy-access-2026-10-17.md) — authenticated read-only access observation with original native choices and payment preserved

- [E5 completion after owner review](articles-e5-cms-review-completion-2026-10-17.md) — original preparation dates remain past milestones only with exact fresh publication proof

- [E5 recorded first publication](articles-e5-cms-publication-achievement-2026-10-17.md) — immutable original publication fact, separate from expiring live health and new-work permission

- [E5 durable native setup coordinator](articles-e5-cms-setup-coordinator-2026-10-17.md) — one accepted hold through discovery, original-policy recovery, same-operation native goal and private settlement

- [E5 preparatory setup authority](articles-e5-cms-setup-authority-2026-10-17.md) — private original intent, expiring discovery lease and lost-acknowledgement policy replay
- [E5 stable setup payment identity](articles-e5-cms-financial-subject-2026-10-17.md) — one original site/owner price through real policy discovery, preserving legacy holds

- [E5 original artifact reads](articles-e5-cms-artifact-read-2026-10-17.md) — paid original handoff access without model or provider-write permission after stopping

- [E5 native per-site controller](articles-e5-cms-goal-controller-2026-10-17.md) — one owner start/hold, exact capture/topic, original dispatch recovery and private proof completion

- [E5 native stop-before-credit](articles-e5-cms-goal-stop-2026-10-17.md) — original-key CF cancellation proofs, financial leases and retained artifact recovery

- [E5 native work guards](articles-e5-cms-goal-work-guards-2026-10-17.md) — original dispatch/goal/budget admission and GET-only proof recovery after stopping

- [E5 native first-article payments](articles-e5-cms-goal-payments-2026-10-17.md) — original accepted holds, durable credits and financial proof through lifecycle erasure

- [E5 native CMS goal proofs](articles-e5-cms-goal-proofs-2026-10-17.md) — separate provider ledger and original paid-source proof reader, with no invented Git facts

- [E5 durable CMS publication](articles-e5-cms-publication-2026-10-17.md) — one-create journal, uncertain-outcome reconciliation and private native/public body proofs

- [E5 exact CMS approval](articles-e5-cms-approval-2026-10-17.md) — original owner review/automatic consent, durable proof refresh and exact final native preview

- [E5 CMS publishing](articles-e5-cms-publishing-2026-10-17.md) — independent provider authority, encrypted credentials and WordPress/Ghost publication recovery

- [E5 publishing-channel interface](articles-e5-channel-interface-2026-10-17.md) — shared operations, original guarded Git/portable flows and separate provider authority
- [E5 hosted bundle contract](articles-e5-hosted-bundle-contract-2026-10-18.md) — immutable producer parity and bounded inert static export validation, without hosted execution authority
- [E5 hosted palette contract](articles-e5-hosted-palette-contract-2026-10-18.md) — pinned renderer revision 2 with independently readable page/card foregrounds

- [E5 publishing-channel discovery](articles-e5-channel-discovery-2026-10-17.md) — bounded public/repository observations and recommendations, without publishing authority

- [E5 site-recipe authorization](articles-e5-site-recipe-authority-2026-10-17.md) — default-off current target/reservation inputs outside committed v2 recipes

- [E3 original inventory count disclosure](articles-e3-native-inventory-display-2026-10-17.md) — scoped “We found N” observations and immutable producer parity, with no readiness grant

- [F2 bootstrap summary and rendering](articles-f2-bootstrap-summary-2026-10-16.md) — default compact reads, explicit full details, and remaining latency/overlay validation
- [F3 recorded usage and operational alerts](articles-f3-cost-accounting-2026-10-16.md) — original-run accounting, company coverage and durable budget/stall/repair notices
- [G6 preview cleanup deadline](articles-g6-preview-cleanup-2026-10-16.md) — durable 24-hour lifecycle limit and one scoped operational incident

- [Articles B1 failure contract](articles-b1-implementation-2026-10-14.md) — immutable taxonomy, trusted enrollment, emitter coverage and regression/provenance evidence
- [Articles B5 setup delivery](articles-b5-implementation-2026-10-14.md) — durable receipts, pure reconciliation and initial implementation evidence
- [Articles B2 goal runner design](articles-b2-design-2026-10-14.md) — proposed milestone proofs, per-site execution and one backend status for Chat
- [B2 implementation and validation](articles-b2-implementation-2026-10-14.md)
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

- [Website connection lifecycle and repository consent](website-connections.md) — schema, API, revocation, reviewed cleanup, and local verification.
