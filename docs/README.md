# Backend documentation index

Start with the repository [`README`](../README.md) and
[`ARCHITECTURE`](../ARCHITECTURE.md). Use this index to find current
subsystem-specific contracts and runbooks.

## Community chat APIs

- [`community-chat-home.md`](community-chat-home.md)

## Inactive Buzz / MLAI Chat experiment

These documents describe integration work for the inactive experiment in
deploying the open-source Buzz platform. Retain them as historical technical
context; they are not current production runbooks or evidence of an active MLAI
Chat service.

- [`mlai-chat-bridge-contract.md`](mlai-chat-bridge-contract.md)
- [`mlai-chat-bridge-staging.md`](mlai-chat-bridge-staging.md)
- [`mlai-chat-membership-bootstrap.md`](mlai-chat-membership-bootstrap.md)
- [`mlai-chat-release-runbook.md`](mlai-chat-release-runbook.md)

## Organisational memory

The `org-memory-*.md` documents describe governance, providers, ingestion,
retrieval, review, publication, runtime behavior, and rollout evidence. Begin
with [`org-memory-runtime.md`](org-memory-runtime.md) for runtime boundaries and
[`org-memory-pilot-rollout.md`](org-memory-pilot-rollout.md) for the controlled
rollout sequence.

## Reconciliation and scheduled work

- [`stripe-xero-reconciliation.md`](stripe-xero-reconciliation.md)
- [`humanitix-xero-reconciliation.md`](humanitix-xero-reconciliation.md)
- [`reconciliation-knowledge-export.md`](reconciliation-knowledge-export.md)
- [`xero-statement-reconciliation.md`](xero-statement-reconciliation.md)
- [`monthly-update-reminders.md`](monthly-update-reminders.md)

## HealthHack

- [`healthhack-scoring-data.md`](healthhack-scoring-data.md) documents the
  private scoring-data boundary and runtime provisioning contract.

## Roo

- [`coworking-booking.md`](coworking-booking.md)
- [`office-manager.md`](office-manager.md) includes the backend-first rollout,
  scheduler health chain, rollback/drain procedure, and the mandatory
  read-only audit for historical Roo migration identities `0029`–`0036`.
- [`roo-linear-channel-issues.md`](roo-linear-channel-issues.md)

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
