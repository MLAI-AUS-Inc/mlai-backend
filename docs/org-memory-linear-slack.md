# Organisational memory: Linear and selected Slack

PR14 replaces the metadata-only Linear and Slack connectors with durable adapters over the existing `startup_updates` artifact tables. The adapters do not trust webhook bodies as evidence. Signed events and artifact-save signals only move an approved configuration's next sync earlier; the normal cursor-safe runtime reads the durable artifacts, creates immutable versions, and reconciles access and deletion.

## Safety and authority boundaries

- Linear accepts explicit selected `project` scopes. Projects marked private in their durable artifact payload, plus their issues and updates, are excluded.
- Linear project state and issue status, assignee, priority, and project membership are system-of-record facts. The adapter records those authority fields on each source version.
- Slack accepts explicit selected `channel` scopes. IDs beginning with `D`, provider records marked `is_im`/`is_mpim`, and unverifiable `G` scopes without a channel selection record are rejected or omitted from discovery, so direct and multi-person direct messages cannot enter organisational memory through this adapter.
- Slack threads are informal evidence, not authoritative issue or project state. Each source is labelled for informal context, discussion, and open-loop extraction.
- Slack ingests one source per thread rather than duplicating every message as independent evidence. Message IDs, participants, and start/end times remain in citation locators.
- Webhook receipts store hashes, provider/account/scope IDs, event type, and scheduling counts only. They never store source bodies.
- The existing deployment allowlist, organisation enablement, approved source scopes, classification policy, and runtime state gates still apply. Registering a webhook does not enable ingestion.

## Durable source mapping

| Provider artifact | Memory source type | Stable external ID | Citation unit |
|---|---|---|---|
| `LinearProjectArtifact` | `linear_project` | `linear_project:<project-id>` | project snapshot |
| `LinearIssueArtifact` | `linear_issue` | `linear_issue:<issue-id>` | issue snapshot |
| `LinearProjectUpdateArtifact` | `linear_project_update` | `linear_project_update:<update-id>` | project update |
| `SlackThreadArtifact` | `slack_thread` | `slack_thread:<channel-id>:<thread-ts>` | bounded group of ordered messages |

Version keys include normalized content, adapter schema, durable artifact revision, and the exact ACL snapshot. An unchanged rerun is idempotent. A content or permission change creates a new immutable version, retires old chunks, and activates only chunks whose current ACL remains accessible.

## Operator flow

Use the Admin Roo source-control API:

1. Connect an organisation-owned Linear or Slack `ExternalServiceConnection`.
2. Discover scopes and explicitly select approved Linear projects or Slack channels.
3. Preview and dry-run the existing artifact corpus. Both operations create no active memory.
4. Approve the reviewed preview and request the backfill.
5. Keep the memory scheduler and worker services running. `ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400` provides the daily memory reconciliation fallback.
6. Inspect `GET /api/v1/org-memory/connections/<configuration-id>/health` for selected-scope counts, artifact counts, source lag, and Slack threads still inside the quiet period.
7. Use the existing permission-refresh action after changing provider credentials, classifications, or selected-scope ACL metadata.

The adapter is downstream of the existing Linear/Slack artifact acquisition. Existing connector syncs must continue to refresh `startup_updates` artifacts. Artifact save/delete signals schedule an early memory run automatically; the daily run guarantees reconciliation of the durable state already acquired.

## Signed wake endpoints

Linear:

```text
POST /api/v1/org-memory/webhooks/linear/events
Linear-Signature: <hex HMAC-SHA256 of the raw request body>
```

Configure `ORG_MEMORY_LINEAR_WEBHOOK_SECRET` with the Linear webhook signing secret. The receiver validates the raw body signature and requires `webhookTimestamp` within the configured age window. `webhookId` is used for replay protection when present. Matching project events debounce a selected active configuration by 60 seconds by default.

Slack:

```text
POST /api/v1/org-memory/webhooks/slack/events
X-Slack-Request-Timestamp: <unix seconds>
X-Slack-Signature: v0=<HMAC-SHA256(secret, "v0:<timestamp>:<raw-body>")>
```

Configure `ORG_MEMORY_SLACK_SIGNING_SECRET` with the Slack app signing secret. The endpoint supports Slack URL verification, enforces the request-age window, deduplicates `event_id`, and ignores direct-message and unselected-channel events. Selected channel events move the next run to the end of the quiet period; repeated events extend that period so a thread is captured after conversation settles.

Both endpoints return `401` without scheduling work when verification fails. They return a bounded status response for valid accepted, ignored, or duplicate events. Provider events are wake hints only: artifact state and the daily reconciliation remain authoritative.

## Configuration

```text
ORG_MEMORY_ARTIFACT_PAGE_SIZE=100
ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS=900
ORG_MEMORY_SLACK_CHUNK_TARGET_CHARS=6000
ORG_MEMORY_SLACK_SIGNING_SECRET=
ORG_MEMORY_SLACK_WEBHOOK_MAX_AGE_SECONDS=300
ORG_MEMORY_LINEAR_DEBOUNCE_SECONDS=60
ORG_MEMORY_LINEAR_WEBHOOK_SECRET=
ORG_MEMORY_LINEAR_WEBHOOK_MAX_AGE_SECONDS=60
ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400
```

Secrets are deliberately blank by default, making both public webhook endpoints fail closed until configured.

## Reconciliation behaviour

- An artifact that disappears or leaves the selected/cutoff scope emits a removal hint and tombstones its stable source. Current chunks are deactivated atomically.
- A disconnected connection or inaccessible selected scope captures an inaccessible ACL version and revokes current retrieval. Restored access creates a new ACL-aware version and reactivates only that current version's chunks.
- A Slack thread still inside the quiet window remains part of the expected inventory. It is deferred, never mistaken for a deletion.
- Incremental cursors advance only over eligible durable artifacts. Backfill and permission refresh are paginated independently, and permission refresh cannot advance the content cursor.
- A full daily scan catches missed webhooks, deletes, ACL changes, and service outages. PR18 expands this into provider-wide watch renewal, reporting, and freshness SLO alerting.

## Rollout and rollback

Enable `linear` and `slack` in `ORG_MEMORY_ENABLED_PROVIDERS` only after their organisation enablement, reviewed policies, selected scopes, secrets, artifact acquisition, scheduler, and worker are ready. Start with one Linear project and one low-risk Slack channel, inspect source versions and citations, then expand scope.

Rollback is fail-closed: remove the provider from the deployment allowlist or pause the affected configurations. Remove provider webhook subscriptions or secrets to stop early wakes. Existing immutable evidence remains auditable; use the normal source/configuration deletion path when it must be tombstoned. Do not reverse migration `org_memory.0013` while replay receipts are required for an active endpoint.
