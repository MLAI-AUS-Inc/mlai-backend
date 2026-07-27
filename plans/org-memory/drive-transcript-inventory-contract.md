# Google Drive transcript inventory command contract

This contract defines the read-only inventory prototype that precedes transcript ingestion. It does not authorise content extraction, embedding, or model processing.

## Implemented command

```bash
python manage.py inventory_drive_transcripts \
  --connection-id <uuid> \
  --folder-id <drive-folder-id> \
  --modified-after <YYYY-MM-DD> \
  --output <absolute-json-path> \
  --dry-run
```

Repeat `--folder-id` for each explicitly selected folder or Shared Drive root. The command must refuse an empty scope and must never silently inventory all accessible Drive content.

The exact `organization:<id>`, `connection:<id>`, and every `folder:<drive-id>` must first appear in the Google Drive policy's `source_scope.selectors`. The policy also requires a separate approved metadata-inventory record, named data/security approvers, and a file ceiling. This does not enable content ingestion.

## Required input

- Organisation-bound Google Drive connection ID.
- One or more approved folder or Shared Drive IDs.
- Historical cutoff in Australia/Sydney, normalised to UTC in output.
- Allowed MIME/file types.
- Maximum pages/files and an operator-approved inventory-only cost ceiling.
- Output path containing no OAuth credentials.

## Required output schema

```json
{
  "schema_version": 1,
  "inventory_id": "uuid",
  "organization_id": "opaque-id",
  "connection_id": "opaque-id",
  "started_at": "RFC3339",
  "completed_at": "RFC3339",
  "selected_roots": ["drive-id"],
  "historical_cutoff": "RFC3339",
  "counts": {
    "seen": 0,
    "candidate_transcripts": 0,
    "duplicates": 0,
    "unsupported": 0,
    "inaccessible": 0
  },
  "formats": {"application/vnd.google-apps.document": 0},
  "date_range": {"oldest": null, "newest": null},
  "estimated": {
    "characters": 0,
    "tokens": 0,
    "embedding_cost_aud": null,
    "extraction_cost_aud": null,
    "processing_time_band": "unknown",
    "review_items": null
  },
  "items": [],
  "warnings": []
}
```

Each item contains only inventory metadata: stable Drive ID, selected-root lineage, name, MIME type, size where available, created/modified timestamps, owner/domain summary, permission class summary, checksum/version marker where available, shortcut target, supported status, and exclusion reason. Do not include file bodies in the inventory artifact.

## Behaviour and safety requirements

- Use read-only Drive scopes and provider pagination.
- Traverse only explicit roots; detect shortcuts/cycles and record selected-root lineage.
- Include Google Docs, DOCX, PDF, TXT, Markdown, VTT, and SRT candidates.
- Report scanned PDFs, audio/video without an approved transcript, and unsupported types as visible follow-up work.
- Detect exact version/checksum duplicates and likely meeting duplicates without deleting either record.
- Redact credentials, raw permission email lists, and unnecessary personal data from logs/output.
- Make output deterministic for the same provider snapshot and parameters.
- Stop safely at file/page/time ceilings and mark the result partial.
- Perform no downloads, parsing, embedding, extraction, or writes to the memory corpus in `--dry-run` mode.
- Refuse an existing output path instead of overwriting it.
- Create the metadata output with owner-only filesystem permissions (`0600`).

## Pilot selection record

| Input | Approved value |
|---|---|
| Connection/organisation | TBD |
| Folder or Shared Drive IDs | TBD |
| Historical cutoff | TBD |
| File/page ceiling | TBD |
| Allowed formats | Google Docs, DOCX, PDF, TXT, MD, VTT, SRT (proposed) |
| Inventory operator | TBD |
| Data/privacy approver | TBD |
| Inventory execution window | TBD |

The prototype may be implemented and tested with synthetic fixtures now. It must not be run against MLAI Drive until the selection record is approved.
