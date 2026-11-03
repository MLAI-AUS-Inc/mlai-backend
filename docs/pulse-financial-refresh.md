# Pulse financial chart refresh

Pulse's financial refresh collects a new six-month accounting chart without
generating prose, editing a draft, publishing an update, or changing existing
dashboard metric observations. Saving the editor binds the exact preview to a
new working revision. Publishing still requires normal review of that revision.

## Owner API

The same routes exist under `/api/v1/vibe-raising/` for website authentication
and `/api/v1/community-chat/startups/` for revocable Chat account sessions.
Every call pins the owned company with `?company_id=<uuid>`; another tab's
active-company selection cannot change that target. Responses use
`Cache-Control: private, no-store`.

`POST financial-refresh/` accepts:

```json
{
  "requestId": "client-generated-uuid",
  "targetMonth": "2026-10-01",
  "updateId": 123,
  "expectedRevision": 456,
  "inputSources": ["stripe", "xero"]
}
```

An unsaved editor supplies its stable normal-save `creationKey` UUID instead of
`updateId`/`expectedRevision`. Xero must be selected for the accounting chart.
The endpoint performs no provider reads and returns HTTP 202 with:

```text
{
  refreshId, targetMonth,
  status: queued | running | ready | partial | failed,
  sources: [{ provider, status, lastRefreshedAt?, detail? }],
  candidate: null | { id, expiresAt, ownerPreview, audiencePreview },
  detail?, retryAfterSeconds?
}
```

`GET financial-refresh/<refreshId>/` returns the same shape. Source states are
`pending`, `refreshing`, `ready`, `failed`, or `reconnect_required`. The two
previews use the existing `financialHistory` DTO: `ownerPreview` contains dollar
amounts; `audiencePreview` always contains normalized relative values. A partial
result has a valid Xero chart plus an unsuccessful optional Stripe refresh.
Failure never replaces an existing chart with zeros or a different source.

Normal `POST updates/` accepts `financialCandidateId` alongside the current
editor fields, `expectedRevision`, `inputSources` and the explicit
`financialHistorySelection: { enabled, shareAmounts }`. The candidate may be
retained with `enabled: false` for private owner review. Neither downloading a
candidate nor applying it opts into inclusion or dollar disclosure. Saved
owner responses include `financialHistoryOwnerPreview` and
`financialHistoryAudiencePreview` independently of inclusion; shared DTOs omit
both and serialize only the approved `financialHistory` disclosure.

Candidates expire after 24 hours. An unauthorized ID returns 404. Changed
revision, company/update identity, reporting settings, selected financial
sources, deleted source data, changed connection authority, or expired evidence
returns 409. Save resolves and hashes server evidence; browser-supplied amounts
are never authoritative. Older clients retain the existing cache-only
`financialHistorySelection.refresh` behavior.

## Collection and persistence

`startup_updates.financial_refresh` uses the existing `ContentFactoryRun` model
with workflow `pulse_financial_refresh`, plus immutable `MonthlyEvidenceSnapshot`
rows. No migration or new worker service is required. Admission locks the company
and coalesces equivalent in-flight requests with the same owner, update identity,
revision, sources, connection identities and reporting settings. A repeated
request ID with changed inputs fails rather than mutating an earlier job.

A bounded two-thread fast path advances saved stages. Each stage claims a
180-second lease and commits only while its token still owns the job. Provider
reads happen outside the job transaction; existing connector credential rotation
retains its own locking. Polling wakes unfinished work after process restart or
capacity exhaustion. Completed stages are retained, transient reads retry at most
three times with provider backoff, and jobs expire after 24 hours. If the user
closes the editor and a process stops, work resumes when the editor polls again.

The accounting source fetches the exact six calendar-month Xero Profit and Loss
reports ending at the selected reporting month, using accrual accounting. Current
months use the pinned local reporting cutoff; prior months use month end. Base
currency must match the startup's reporting currency. Canonical parsing includes
direct costs, preserves missing values as gaps, and validates report hashes and
scope through `financial_history.build_snapshot`.

Stripe reads bounded invoice pages created within the same window. The job keeps
page hashes/counts and status, without customer details or raw invoices. This is
separate payment-source evidence, not complete company revenue or cash collected.
Stripe amounts are never added to or substituted for Xero accounting totals.
Refreshing does not republish global financial observations or change their cache.

The candidate owns a separate immutable evidence snapshot and capture timestamp.
`save_revision(financial_history_snapshot=...)` updates only the financial-history
attachment and its `_source` proof. The original narrative evidence and private
`financialSnapshot` remain intact. The normal revision hash covers the resulting
chart. Existing published revisions and approvals remain immutable until the
founder approves the new revision. Startup data deletion scrubs refresh receipts
and fences in-flight workers before deleting candidate evidence.

## Validation

Database-free tests: `startup_updates.tests_financial_refresh_unit` and
`startup_updates.tests_financial_history_unit` through
`scripts/test_without_database.py`. Integration tests cover scoped admission,
deduplication, fresh six-month requests, source errors, leases/retries, deletion
and revocation, candidate expiry, unsaved drafts, real save behavior and frozen
publication. They are assigned to CI.

Local integration testing uses `scripts/test_pulse_financial_refresh_database.py`
only after explicit approval of the exact inventory in
`pulse-financial-refresh-test-migrations.json`. The runner checks that inventory,
blocks network access and production credentials, uses synthetic data in a
temporary SQLite database, and deletes it afterward.
