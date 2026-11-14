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
`updateId`/`expectedRevision`. With Xero selected the chart is the accounting
chart and Xero must refresh; a Stripe-only startup may select Stripe alone.
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
`pending`, `refreshing`, `ready`, `failed`, or `reconnect_required`. Stripe is
not read once Xero has failed, because no chart can be saved; it stays `pending`
with a `detail` saying so. The two
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

New refreshes pin the owner's selected report account for each provider. A
missing or disconnected explicit choice requires reconnection; it never falls
back to another tenant. Changing the selected account affects new requests only:
an existing request, including retries, keeps its original connection pins.

A bounded two-thread fast path advances saved stages. Each stage claims a
180-second lease and commits only while its token still owns the job. Provider
reads happen outside the job transaction; existing connector credential rotation
retains its own locking. Polling wakes unfinished work after process restart or
capacity exhaustion. Completed stages are retained, transient reads retry at most
three times with provider backoff, and jobs expire after 24 hours. If the user
closes the editor and a process stops, work resumes when the editor polls again.

The accounting source fetches, for the six calendar months ending at the selected
reporting month, the Xero Profit and Loss (accrual) and Bank Summary (cash)
reports: thirteen Xero calls including the base-currency check. Current months
use the pinned local reporting cutoff; prior months use month end. Base currency
must match the startup's reporting currency. Parsing preserves missing values as
gaps and validates report hashes and scope through `financial_history.build_snapshot`.

- Accrual revenue is Total Income plus Total Other Income. Accrual costs are
  revenue minus Net Profit, so every expense section counts and revenue - costs
  always equals Xero's Net Profit.
- Cash in and cash out are the Bank Summary totals across all bank accounts, the
  report behind Xero's dashboard "Cash in and out" chart. Each account must
  reconcile (opening + received - spent = closing) or the month is a gap.
- A connection without `accounting.reports.read` or
  `accounting.reports.banksummary.read` still refreshes accrual and reports
  `cashUnavailable: "scope"`, asking the founder to reconnect for cash.

Stripe reads bounded `/v1/balance_transactions` pages in the same window and
keeps only monthly sales, refunds, fees and payouts in the reporting currency,
plus page hashes and counts. No customer details or raw transactions are kept.
Refreshing does not republish global financial observations or change their cache.

## Cash and accrual bases

Each saved chart stores accrual, cash and Stripe series side by side (snapshot
`schemaVersion: 2`). Readers choose the series when the chart is read, from the
startup's `StartupProfile.progress_configuration["financial_display"]`:
`{"basis": "cash" | "accrual", "stripeInXero": bool}`, defaulting to cash and
`true`. `GET/POST business-health/` reads and writes `financialBasis` and
`stripeInXero`; changing them does not bump the reporting version, so in-flight
refreshes stay valid. Every reader (editor, saved and published updates, the
community feed, the public page and rendered markdown) applies the current
setting, so a change updates every report at once with no provider call.

Stripe merge rule: when Stripe payouts land in a Xero bank account
(`stripeInXero`, the default) Xero already counts that money, so totals are
Xero's and the DTO's per-point `stripe` value shows the Stripe share. Otherwise
Stripe is added once: payouts to cash in, or net sales to revenue and fees to
costs. A startup with no Xero figures charts Stripe alone and never reports
costs from it. The `financialHistory` DTO keeps `schemaVersion: 1` and adds
`basis`, `basisFallback` (`legacy`, `scope` or `unavailable` when cash was asked
for but is missing) and `stripe` (`withinXero`, `added`, `only` or null).

Charts saved before this change are never rewritten (revisions are immutable).
`upgrade_legacy_history` recomputes them at read time from their own source
snapshot, which applies today's accrual definition with no provider call. For
cash, `python manage.py backfill_pulse_cash_basis` (dry run by default; `--apply`,
`--max-calls`, `--pause`) fetches the Bank Summary for each old chart's months
from the same Xero account and stores a content-addressed cash supplement
snapshot that readers merge in. It is an operational command: run it only when
asked, and mind the shared Xero daily API limit.

The candidate owns a separate immutable evidence snapshot and capture timestamp.
`save_revision(financial_history_snapshot=...)` updates only the financial-history
attachment and its `_source` proof. The original narrative evidence and private
`financialSnapshot` remain intact. The normal revision hash covers the resulting
chart. Existing published revisions and approvals remain immutable until the
founder approves the new revision. Startup data deletion scrubs refresh receipts
and fences in-flight workers before deleting candidate evidence.

## Validation

Database-free tests: `startup_updates.tests_financial_refresh_unit`,
`startup_updates.tests_financial_history_unit` and
`startup_updates.tests_financial_basis_unit` through
`scripts/test_without_database.py`. Integration tests cover scoped admission,
deduplication, fresh six-month requests, source errors, leases/retries, deletion
and revocation, candidate expiry, unsaved drafts, real save behavior and frozen
publication. They are assigned to CI.

Local integration testing uses `scripts/test_pulse_financial_refresh_database.py`
only after explicit approval of the exact inventory in
`pulse-financial-refresh-test-migrations.json`. The runner checks that inventory,
blocks network access and production credentials, uses synthetic data in a
temporary SQLite database, and deletes it afterward.
