# Organisational memory: selected-root Notion

PR15 replaces the shallow, run-scoped Notion page bundles used by startup updates with an organisational-memory-owned inventory. Notion discovery remains metadata-only. Content is fetched only after an administrator explicitly selects a `page_root` or `data_source`, approves the reviewed preview, and requests backfill.

## Durable source boundary

- A selected page is traversed recursively through its block children, child pages, child databases, and their 2026 API data sources.
- A selected data source is queried page by page. Every returned page is then retrieved through the same page/block path.
- `NotionPageArtifact` stores normalized current page metadata, visible property text, cleaned content, selected-root ancestry, lifecycle, provider revision, and the scan generation in which access was confirmed.
- `NotionBlockArtifact` stores normalized block text, provider block ID, parent block ID, global ordinal, depth, heading path, timestamps, and content hash.
- One `notion_page:<page-id>` memory source is created per durable page. Chunks contain `page_id` and exact start/end block IDs and ordinals so evidence can be cited back to the provider object.
- OAuth tokens remain in the encrypted `ExternalServiceConnection`. They are used only in outbound authorization headers and never enter artifacts, cursors, queue payloads, webhook receipts, or source metadata.
- The adapter does not read or depend on `ExternalServiceConnection.sync_cursor["startup_update_runs"]`. Those legacy run bundles may continue to support the startup-update feature, but they are not organisational-memory evidence.

Notion webhook events are change hints rather than source content. The adapter always retrieves current state from the Notion API before creating a memory version.

## Operator flow

1. Connect the organisation-owned Notion workspace using the existing Notion OAuth integration.
2. Enable `notion` in the deployment provider allowlist and approve the organisation/provider enablement. Registration alone does not activate ingestion.
3. Discover scopes. The adapter calls Notion search and returns page/data-source names, IDs, URLs, and object types only.
4. Explicitly select the approved page roots or data sources. Apply the correct classification/policy to each root; avoid overlapping roots with different classifications.
5. Preview and dry-run. These operations report existing durable inventory metadata and create no active memory.
6. Approve the reviewed preview and request backfill. Keep the memory worker and scheduler running until all resumable pages complete.
7. Inspect connection health plus the read-only Notion page/block admin tables. Sample current memory sources and verify page/block citation locators before expanding scope.
8. Keep `ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400` (or a stricter reviewed value). The daily full selected-root scan is the correctness fallback for missed, delayed, retried, or out-of-order webhooks.

The cursor contains only bounded IDs, traversal ancestry, provider pagination cursors, and a scan UUID. It contains no source bodies or credentials. Backfill checkpoints and incremental cursors are idempotent: a retried page upserts the same durable page/block identities and the evidence kernel deduplicates an unchanged version key.

## Access and deletion reconciliation

- A page confirmed under a selected root is marked active for the current scan generation.
- A page returned with `in_trash` or legacy `archived` state captures an inaccessible ACL version. Current chunks are deactivated, while immutable history remains auditable.
- A page returning `403`/`404` is treated as access loss, not proof of permanent deletion. Its last durable body is retained but retrieval access is revoked.
- An active artifact not seen anywhere in the completed selected-root scan is marked `access_lost` and emits an access-revocation removal. This covers unsharing, moving outside the selected subtree, and root deselection.
- If a previously lost or trashed page becomes visible again, a fresh current API capture restores access through a new ACL-aware version.
- Hard tombstoning remains reserved for the normal explicit deletion/reconciliation path when the durable object identity itself is removed. Soft deletion and permission uncertainty never destroy evidence automatically.

## Signed webhook wake endpoint

```text
POST /api/v1/org-memory/webhooks/notion/events
X-Notion-Signature: sha256=<HMAC-SHA256(raw-body, verification-token)>
```

Notion first sends an unsigned JSON object containing `verification_token`. The endpoint acknowledges that one-time request without storing, logging, or echoing the token and without scheduling ingestion. Capture the token through the controlled subscription-verification procedure, set it as `ORG_MEMORY_NOTION_WEBHOOK_VERIFICATION_TOKEN`, restart the web service, and complete verification in Notion. If a token is already configured, a mismatched verification request fails closed.

Subsequent events require the exact raw-body HMAC, an accepted event timestamp, and a configured token. Replay-safe receipts retain only the event hash, event ID derivation, workspace ID, entity ID/type, event type, and scheduling count. Because an event entity may be a descendant rather than a selected root, a valid event wakes every active configuration for the connected workspace; the adapter then enforces selected-root scope during traversal. Invalid requests return `401` and schedule nothing.

Relevant provider behavior is documented in Notion's [webhook verification guide](https://developers.notion.com/reference/webhooks), [event delivery reference](https://developers.notion.com/reference/webhooks-events-delivery), [search reference](https://developers.notion.com/reference/post-search), and [data-source query reference](https://developers.notion.com/reference/query-a-data-source).

## Configuration

```text
NOTION_API_VERSION=2026-03-11
ORG_MEMORY_NOTION_DISCOVERY_PAGE_SIZE=100
ORG_MEMORY_NOTION_SCAN_PAGE_BUDGET=10
ORG_MEMORY_NOTION_SCAN_MAX_PAGES=1000
ORG_MEMORY_NOTION_MAX_BLOCKS_PER_PAGE=2000
ORG_MEMORY_NOTION_MAX_DEPTH=16
ORG_MEMORY_NOTION_CHUNK_TARGET_CHARS=6000
ORG_MEMORY_NOTION_HTTP_READ_SECONDS=20
ORG_MEMORY_NOTION_WEBHOOK_VERIFICATION_TOKEN=
ORG_MEMORY_NOTION_WEBHOOK_MAX_AGE_SECONDS=90000
ORG_MEMORY_NOTION_DEBOUNCE_SECONDS=60
ORG_MEMORY_SYNC_INTERVAL_SECONDS=86400
```

Limits are fail-closed. Increase traversal/block limits only after measuring the selected workspace and worker/database load. The webhook secret is deliberately blank by default, so signed event ingestion cannot be spoofed before operator configuration.

## Rollout and rollback

Start with one low-risk page root, verify recursive coverage, classification, citations, access revocation, and daily freshness, then expand. Do not choose a workspace-wide root until the source policy and classification boundary are reviewed.

Rollback is fail-closed: pause the affected configuration or remove `notion` from `ORG_MEMORY_ENABLED_PROVIDERS`, then remove the Notion subscription or signing token to stop early wakes. Existing immutable evidence remains auditable and inaccessible according to its ACL. Use the normal connection deletion flow when evidence must be tombstoned. Do not reverse `org_memory.0014` while a Notion configuration is active.
