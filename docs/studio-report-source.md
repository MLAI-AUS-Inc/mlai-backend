# Private Studio report preparation

`POST /api/v1/integrations/studio-reports/prepare` is disabled by default.
It reloads approved work records, discovers contractor invoices in the treasury
mailbox, and reads client invoices and reconciled receipts from Xero. It never
creates accounting entries or sends mail. OAuth rotation stays backend-owned and
uses a database row lock. No schema migration is needed.

Configure a dedicated key of at least 32 random characters:

```dotenv
STUDIO_REPORT_SOURCE_ENABLED=true
STUDIO_REPORT_SOURCE_KEY=<dedicated-private-service-key>
STUDIO_REPORT_SOURCE_CONFIG=/app/studio-report-source/clients.json
```

The key must differ from `ROO_API_KEY` and `INTERNAL_API_KEY`. Give it only to
the private Studio worker. Keep source files out of Public Roo. Both production
web slots mount `STUDIO_REPORT_SOURCE_DIR` (default `./data/studio-report-source`)
at `/app/studio-report-source`. Keep directories mode 700, files mode 600, and
back up this directory separately. Never commit its contents.

`clients.json` fixes each verified owner's scope. These are placeholders:

```json
{
  "UEXAMPLECLIENT": {
    "team": "TEXAMPLE",
    "organization_id": 123,
    "linear_organization": "verified-linear-organization-uuid",
    "project_ids": ["verified-project-uuid"],
    "xero_connection_id": 456,
    "xero_contact_id": "verified-xero-contact-uuid",
    "legacy_invoice_ids": [],
    "work_manifest": "/app/studio-report-source/work.json",
    "gmail_credentials": "/app/studio-report-source/gmail.json",
    "gmail_query": "reviewed-client-and-supplier-scoped-invoice-query",
    "review_directory": "/app/studio-report-source/review"
  }
}
```

The Gmail query must cover the client's suppliers, revisions and outstanding
invoice history. Validate it against reviewed evidence before activation; a
narrow query can miss invoices. Do not restrict it to unread mail or the last
sync date. Each pass fully pages the bounded query, including spam/trash.
Page limits, time limits and provider errors fail closed.

`gmail.json` contains `email`, `client_id`, `client_secret`, `refresh_token` for
the approved read-only treasury connection. The live Gmail profile must match
`email`. Do not restart the retired reconciliation-agent service. Reauthorise
the mailbox if retained read credentials have been revoked.

The work manifest contains the complete existing Roo finance projection:

```json
{
  "snapshot": {"version": 1, "clients": {"UEXAMPLECLIENT": "full reviewed dataset"}},
  "reviewed_at": "2026-10-23T17:00:00+11:00",
  "hours_complete_through": "2026-10-23T17:00:00+11:00",
  "reviewed_gmail_ids": ["reviewed-message-id"]
}
```

`snapshot` must include the full envelope, evidence and records, exactly one
owner, the correct workspace/Linear organisation and exact project set. Roo
validates provenance, hour conservation and cash allocation before use.
Publish atomically after reviewing submissions and invoice replacements.
`hours_complete_through` attests work coverage across every mapped project,
including explicit confirmation of no submitted work. It cannot exceed
`reviewed_at`. Neither value is a sync timestamp. The collector reloads the file
each attempt; it cannot discover unsubmitted work or approve prose/PDF work
dates and project allocations automatically.

New mailbox IDs block sending and are saved as private review candidates.
Review documents, add supported work quantities with builder/project/dates,
resolve replacements, then add those message IDs to the manifest. Do not clear
a gap merely by advancing a date or adding an ID. Contractor money never
becomes work hours. Multi-project and ambiguous invoices require review.

The request has exactly `owner`, `team`, timezone-aware `cutoff` and random
64-hex `nonce`. It cannot override paths, projects, account or mailbox. Send
`X-Studio-Source-Key` over HTTPS. Response signatures are HMAC-SHA256 over sorted,
compact JSON, binding scope, cutoff, nonce, per-stream checks and status.

`ready` requires work coverage through the cutoff, no unresolved work, no
unreviewed invoices, and complete, stable finance reads. Invoices are read
again after payments to detect concurrent changes. Actual reconciled payment
IDs are used; batch receipts count once. Reviewed external historical invoices
may remain via `legacy_invoice_ids`, but their bank receipts are revalidated
for ownership, amount, date, reconciliation and deletion. Missing invoices,
reversals, credits requiring attribution, changed amounts and ambiguous
cutoff-day payment times never become assumed credits.

`waiting_data` returns safe gap codes and no usable snapshot. Private review
receipts retain candidate data and unreviewed message IDs without logging money,
email bodies or credentials. A finance sync never advances work coverage.
The private worker retries every five minutes and freezes one ready snapshot
for both recipients and all charts at the original Friday cutoff.

Activation order: deploy with the feature disabled, provision/verify private
files and key, exercise the scoped endpoint, then enable Roo's private refresh
flag. Preserve existing grants and schedule IDs. Do not run migrations during
activation. Disable weekly issuance to pause sends. Disabling refresh returns
to the separately documented historical-snapshot behaviour.

Synthetic checks:

```sh
python scripts/test_without_database.py integrations.tests_studio_report_source
```
