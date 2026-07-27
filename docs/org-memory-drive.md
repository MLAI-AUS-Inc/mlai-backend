# Organisational memory: Google Drive control plane

This is the PR8–PR9 operator runbook for discovering, approving, and processing exact Google Drive roots. Inventory and dry-run remain metadata-only. Only an approved backfill or active incremental sync may export/download content and create immutable source versions and citation-ready chunks. Claims and entity extraction remain deferred to PR10.

## Safety boundary

- Use an organisation-bound `ExternalServiceConnection` with the read-only Drive OAuth scope.
- Select explicit `folder` or `shared_drive` IDs. Discovery never selects a root automatically.
- Governance must approve the connection, organisation, and every exact `folder:<id>` or `shared_drive:<id>` selector before inventory runs.
- A complete inventory and dry-run are required before approval. A partial inventory caused by a file, page, or time ceiling is not approval-ready.
- Inventory asks only for bounded metadata. It does not request owner identities, file bodies, exports, downloads, or media.
- Content processing requires `drive.readonly` (metadata-only OAuth is rejected), checks `capabilities.canDownload`, and rejects a file before download if its selected-root IDs are no longer selected.
- Downloads, parsed output, artifact/source versions, ACL snapshots, chunks, and checkpoints are bounded and committed atomically. A failed page cannot advance its cursor.
- Exact and high-confidence near-duplicate copies are linked to a canonical meeting artifact without creating a second evidence source or chunk set, so they cannot count as independent corroboration.
- Webhook notifications can only schedule an earlier sync. The Drive changes feed and the existing daily scheduler remain authoritative.

## Operator flow

Use the source-control API with an Admin Roo service principal and verified actor assertion:

1. `POST /api/v1/org-memory/connectors/google_drive/connect` with the organisation-bound `external_connection_id`.
2. `POST /api/v1/org-memory/connections/<configuration-id>/discover`, following `next_cursor` until it is empty.
3. `PUT /api/v1/org-memory/connections/<configuration-id>/scopes` with the exact folder and Shared Drive IDs to select.
4. `POST /api/v1/org-memory/connections/<configuration-id>/preview` to create an immutable metadata inventory manifest.
5. Review roots, counts, formats, coarse ownership classes, permission classes, inaccessible/unsupported items, cutoff, warnings, and configured cost estimates.
6. `POST /api/v1/org-memory/connections/<configuration-id>/dry-run` and verify `approval_ready=true` and `active_memory_created=false`.
7. `POST /api/v1/org-memory/connections/<configuration-id>/approve` with `{"confirm": true}`.
8. `POST /api/v1/org-memory/connections/<configuration-id>/backfill` with `{"confirm": true}`. The worker processes the approved manifest oldest-first and checkpoints after each bounded page.
9. Review `GET /api/v1/org-memory/connections/<configuration-id>/health` and the `DriveReconciliationReport` counters before expanding the pilot roots.

The standalone safety inventory remains available for pre-approval investigation:

```bash
python manage.py inventory_drive_transcripts \
  --connection-id <external-connection-id> \
  --folder-id <approved-folder-id> \
  --modified-after 2024-01-01 \
  --max-files 10000 \
  --max-pages 1000 \
  --max-seconds 300 \
  --output /absolute/private/path/drive-inventory.json \
  --dry-run
```

The command refuses relative/existing output paths and creates the JSON with mode `0600`.

## Cost configuration

The Drive pipeline never hard-codes model prices. Configure approved internal AUD rates when the extraction and embedding models are selected:

```text
ORG_MEMORY_DRIVE_EMBEDDING_COST_AUD_PER_MILLION_TOKENS
ORG_MEMORY_DRIVE_EXTRACTION_COST_AUD_PER_MILLION_TOKENS
```

Until both are positive, the inventory reports `pricing_configured=false` and null cost values. Google-native document sizes remain unknown before export, so estimates explicitly report that limitation.

## Parsing and chunking

Supported deterministic parsers are:

- Google Docs exported as Markdown to preserve headings;
- DOCX paragraphs, heading styles, and table-row positions;
- PDF pages with a text layer;
- UTF-8 text and Markdown;
- WebVTT and SRT cues with speaker and timestamp ranges.

Google Workspace exports use Drive `files.export`; ordinary files use bounded `files.get` media downloads. The default local processing ceiling is 25 MiB, while Drive itself limits ordinary `files.export` responses to 10 MB. Content is streamed in bounded chunks where the real client supports it.

Conversation cues remain separate chunks. Other paragraphs are grouped without splitting a block until the configured target/max sizes require it. Every chunk carries the Drive file ID, parser, stable meeting identity/title/date/timezone/participants, section/page range, block range, speakers, and timestamps where available. One chunk never mixes source ACLs.

Configure the bounds with:

```text
ORG_MEMORY_DRIVE_PROCESSING_PAGE_SIZE=10
ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES=26214400
ORG_MEMORY_DRIVE_CHUNK_TARGET_CHARS=5000
ORG_MEMORY_DRIVE_CHUNK_MAX_CHARS=8000
ORG_MEMORY_DRIVE_CHUNK_OVERLAP_CHARS=300
```

Shortcuts are not followed. Scanned PDFs become `needs_ocr`; audio/video without an existing transcript becomes `needs_transcription`; download restrictions and unsupported formats remain visible on the artifact. Parser results are immutable per Drive metadata version and parser version, so a parser upgrade safely creates a new source version while an identical rerun performs no download or repeated parsing. Parser failures appear in reconciliation rather than being silently skipped.

Meeting identity uses the strongest deterministic metadata currently available: normalized title, an explicit date from the filename/heading, source-created date fallback, and detected speakers. Exact-content copies and near-duplicate signatures are linked with `copied_from` without creating duplicate sources or chunks; distinct transcript/notes artifacts for the same meeting use `same_meeting_as` and remain separate evidence.

## Change cursor and webhook hints

The connector captures a Drive start-page token before inventory, advances through every `nextPageToken`, and commits only the final `newStartPageToken`. It requests removals and Shared Drive changes. A failed page does not advance the durable cursor because the runtime commits metadata and cursor in one transaction.

To add an optional early wake-up channel, expose the callback over HTTPS and register it:

```bash
python manage.py register_drive_watch \
  <configuration-id> \
  --callback-url https://api.example.com/api/v1/org-memory/webhooks/google-drive/changes \
  --days 6
```

The database stores only the SHA-256 hash of the random channel token. Incoming notifications must match channel ID, resource ID, token hash, expiration, and a strictly increasing message number. Duplicate and initial `sync` notifications do not schedule work. Change notifications set the configuration's next sync time to now; they never carry or activate source data.

Google Drive change watches expire within seven days. Until automatic renewal lands with daily reconciliation in PR18, operations must renew them before expiry. An expired/missing watch does not stop scheduled polling.

## Monitoring and failure handling

`GET /api/v1/org-memory/connections/<configuration-id>/health` reports artifacts, meetings, extracted/duplicate/unsupported/failed totals, cursor presence, watch state, and the latest reconciliation counters. Existing runtime health and dead-letter tooling cover retry exhaustion. Investigate these conditions before resuming:

- `partial=true`: reduce selected scope, raise an approved ceiling, or fix inaccessible folders; do not approve it.
- `access_lost`: the linked source ACL is revoked and current chunks are deactivated.
- `trashed` or `removed`: the linked source is tombstoned and current chunks are deactivated in the same transaction.
- invalid/expired OAuth: reauthorise the organisation-owned connection; refresh failures are surfaced without logging tokens.
- invalid webhook: returns 401 and does not schedule work.
- unsupported format, OCR/transcription work, download restriction, or shortcut: remains a visible artifact and creates no active source; if a formerly supported file changes into this state, its previous chunks are deactivated until a supported version is processed.
- `duplicate_suppressed`: review its canonical link if independent evidence was expected; by default it creates no second active source.

## Rollback

Disable Google Drive in the existing provider-governance manifest to stop new Drive work, pause affected configurations, and let in-flight leased work complete or retry. The webhook endpoint is inert without active channels. Use the existing configuration/source deletion path to tombstone active Drive sources before reversing migrations. Reverse `org_memory.0009`, `0008`, then `0007` only after retaining required reconciliation, meeting-lineage, artifact, source-version, ACL, and chunk evidence; schema reversal deletes PR8–PR9 Drive records. PR9 creates no claims or entities.
