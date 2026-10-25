# Startup Progress contract and rollout

Status: implementation behind `STARTUP_PROGRESS_ENABLED=0`. The approved
`0024_startupprofile_progress_configuration` migration and all 346 listed
prerequisites were applied to a disposable local SQLite database on 19 September
2026. All 107 targeted backend tests passed, including database integration tests;
the migration drift check reported no changes. Production has not been migrated
or deployed. This document describes the implemented first release, not a claim
that all proposed connectors exist.

## Scope

Progress is a private, shared company dashboard. It uses existing
`StartupMetricObservation` rows, exact coverage metadata and server-owned chart
snapshots. It does not provide health scores or business advice. Pinning a chart,
selecting a source for AI, and sharing a chart are independent actions.

The first release uses verified Xero P&L, precisely labelled paid Stripe invoice
sales, Luma event counts and GA4 monthly results. Founder-provided nonfinancial
metrics accept up to 24 monthly rows, entered directly or via the frontend CSV
form. Currency measures cannot be created through this form. Sheets OAuth,
Search Console, product analytics, CRM, newsletters and native social APIs remain
separate future integrations. Their frontend catalogue is explicitly labelled.

## Private APIs

All endpoints are under `/api/v1/vibe-raising/progress/`, authenticated, feature
flagged, explicitly company scoped (`company_id` for GET, `companyId` for POST),
and subject to the existing company/domain ownership checks.

- GET `/`: series, saved chart specs (null until chosen; [] explicitly empty),
  preferred range, configuration version, GA properties/events and custom definitions.
- POST `/`: `{expectedVersion, charts, range}`. Charts have `id`, ordered
  `seriesIds` (1–3), `months` (3/6/12/24), `type` (line/bar) and `caption` (280 chars).
  The server resolves all IDs within this organisation and validates compatible
  series. At most 12 charts; last-writer conflicts return 409.
- POST `/custom-metrics/`: `{expectedVersion, key?, label, definition, category,
  unit, aggregation, points:[{date:'YYYY-MM-01', value}]}`. Units are count, people,
  accounts, pilots, subscribers, actions, %, seconds or hours. Definitions cannot
  be changed after creation; use a new metric for a new meaning. Repeated monthly
  values upsert in the founder namespace. Omitted months retain earlier values.
- POST `/google-analytics/`: `{expectedVersion, propertyId, eventName?, eventLabel?}`.
  Only a selected property attached to this organisation is accepted. One refresh
  reads up to 24 months at `yearMonth` grain and discovers observed events in
  bounded pages (2,000 maximum). An action selection adds event occurrences and
  event-filtered distinct users as separate series. Six refresh requests per user
  per hour. Existing observations survive an upstream failure; successful
  replacement is atomic, version checked and limited to the selected scope.

Configuration writes are limited to 60 requests per user per minute. Configuration
lives in `StartupProfile.progress_configuration`, with a monotonically increasing
version and `charts`, `range`, `definitions`, `ga_events`, `ga_mappings` keys.

## Measurement semantics

Each series ID hashes metric, provider, currency/unit, account/property/event,
timezone, definition version and reporting basis. Different scopes stay separate.
Points carry explicit periodStart/periodEnd, observedAt, value (including zero or
null) and partial status. Missing months are gaps; repeated observations use the
latest observation rather than adding totals. Historical partial periods remain
partial. This release supports calendar-month charts only; arbitrary weekly
aggregation and custom funnels require additional provider adapters.

GA users/rates come from monthly provider aggregates. Daily distinct counts and
breakdown percentages are not added. Legacy truncated report breakdowns cannot
become headline totals. Monthly action users indicate an observed action, not a
universal definition of activation or retention. GA can revise recent numbers;
its timestamp and definition remain visible. Provider thresholding/sampling
metadata is retained as limitations when present.

Luma registrations and check-ins count participation across ended events, not
unique people. Missing check-in coverage is not zero attendance. Stripe invoice
sales are not MRR and are not added to Xero income. Unconfirmed Stripe totals are
not chart-ready. Imported observations cannot be overwritten by founder edits.

Only matching income/costs or registration/check-in series can currently share an
axis, and only with matching source scope, coverage and applicable currency.
Rates use a 0–100 axis. Bars include zero; partial line segments are dashed.

## Update revisions and disclosure

The existing save endpoint accepts optional `chartSelections`. Omission preserves
a legacy update or the prior selection. An explicit `[]` shares no charts.
The server ignores browser numerical chart payloads and materializes selections
from scoped observations through the update date. Identical specs preserve the
prior snapshot for text-only edits; the editor's explicit “Use latest source
figures” makes a new chart identity and snapshot. Changing the date to before a
snapshot's cutoff forces rematerialization. Changes must be reviewed before
publishing the exact revision hash.

`structured_memo.progress_charts` contains specs, definitions, selected points and
cutoff, covered by the immutable revision content hash. AI redrafting inherits
these choices. Publication uses the existing atomic revision approval path.
Published API responses strip internal scope/observation identifiers and suppress
legacy datasets when explicit chart selection is present, even when empty. Text
exports also respect that selection. Narrative remains founder-reviewed and may
independently discuss figures. Existing legacy publications are unchanged.

## Rollout and validation

1. `startup_updates.0024_startupprofile_progress_configuration` is created and
   verified locally (one JSON field, no data rewrite). Obtain explicit approval
   for applying the migration in each additional target environment.
2. Run `vibe_raising.tests_progress_contract`, `vibe_raising.tests_progress_api`,
   `startup_updates.tests_revisions` and relevant connector suites against a
   disposable database. **These commands require the repository's specific
   migration approval.** Do not run them against production credentials.
3. Deploy schema/backend first. Enable `STARTUP_PROGRESS_ENABLED=1` only in the
   target environment and set the frontend binding to `true`. Verify source
   reconciliation and founder access with an internal company before broader use.
4. Roll back by disabling both flags. Existing frozen articles remain readable;
   configuration and revisions are retained. The schema is still required by the
   deployed model even when the feature flag is off.

Browser previews use illustrative fixtures and do not validate database or OAuth
behaviour. Test production components on desktop/mobile for pin/reorder/range,
custom metrics, empty states, chart selection, removal, draft recovery and review.

Local verification covered `vibe_raising.tests_progress_contract`,
`vibe_raising.tests_progress_api`, `startup_updates.tests_revisions`,
`integrations.tests_luma_connector`,
`integrations.tests_startup_updates.GoogleAnalyticsServiceUnitTest`,
`integrations.tests_startup_updates.StartupUpdateGoogleAnalyticsPipelineViewTest`
and `integrations.tests_connectors.ConnectorEndpointTests` (107 tests, all passed).
These tests use provider fixtures and mocks; live OAuth/provider access and
production database concurrency have not been exercised.

Primary GA contract: [API schema](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema),
[runReport](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport).

## Pulse financial history — 25 October 2026

Pulse adds a separate, explicit financial-history disclosure. It does not promote
existing `progress_charts` or private dashboard snapshots into a community post.
Existing published revisions have no new chart. This addition requires no schema
change or data rewrite and is independent of `STARTUP_PROGRESS_ENABLED`.

The owner save and generation-start endpoints accept optional
`financialHistorySelection: {enabled: boolean, shareAmounts: boolean, refresh?: boolean}`.
Booleans are strict: strings, numbers, unknown keys and browser-provided chart
values are rejected. `enabled: false` requires `shareAmounts: false` and cannot
refresh. Omitting the selection retains the prior revision's choice and frozen
values. Generation pins the founder's choice on the run; generated prose cannot
introduce financial disclosure choices or numerical history payloads. An inflight
run cannot have its sharing choice changed by a repeated start request.

On first inclusion, the save path materializes exactly six calendar slots ending
at `draft.month` from the server-owned `MonthlyEvidenceSnapshot.payload.charts`.
The report month is independent of the publication/update date. A period's
exclusive cutoff becomes the final covered calendar date in its reporting
timezone. Missing months/series stay null, zero stays zero, and partial periods
retain an explicit inclusive `periodEnd`. Reports after the snapshot's cutoff
cannot enter the chart. Later reads never load current observations or providers.

The initial supported history is Xero accounting revenue and monthly costs on
the accrual basis. Each included total must match the canonical frozen P&L
parser, its source metric, report hash, currency, account, timezone and period.
Direct costs use the same canonical monthly-cost calculation as the dashboard.
Different accounts, currencies, reporting bases or same-month coverage are not
combined. Stripe paid-invoice sales are not substituted for accounting revenue or
combined with Xero costs. Legacy history lacking account provenance remains a gap;
if no compatible values remain, the save returns an actionable evidence-refresh
error rather than creating an empty chart.

`structured_memo.financial_history` stores the selection and complete internal
snapshot, covered by the revision hash. Ordinary writing/cover/audience edits and
the dollar-amount toggle reuse these same frozen underlying values. Explicit
`refresh: true` captures current cached financial evidence into a new immutable
snapshot and revision; it makes no provider request and does not mutate the prior
publication. A source connection must already have been refreshed through its
existing workflow when newer source reports are needed. Disabling/re-enabling
history retains the same frozen figures unless refresh is requested.

Owner DTOs return persisted `financialHistorySelection` without the transient
refresh flag. Owner and approved shared DTOs expose only the allowlisted
`financialHistory` projection (null for disabled/legacy posts):

```json
{
  "schemaVersion": 1,
  "reportingMonth": "2026-08-01",
  "rangeStart": "2026-03-01",
  "rangeEnd": "2026-08-31",
  "cutoff": "2026-08-18",
  "capturedAt": "2026-10-25T03:12:00+00:00",
  "shareAmounts": false,
  "currency": null,
  "points": [
    {"month": "2026-03-01", "revenue": null, "costs": null, "partial": false, "periodEnd": null}
  ]
}
```

The illustrative `points` list above is shortened; valid responses always contain
six ordered calendar slots. Relative mode normalizes all revenue/cost points
together once so the maximum absolute value is 100, preserving signed and zero
values. It exposes no currency, source report, account/observation ID or raw
amount. Only explicit `shareAmounts: true` returns the frozen currency values.
This choice authorizes the chart only; existing shared narrative/metric privacy
filters remain in place. Public HTML and Markdown exports use the same projection.
Clients highlight `reportingMonth`, show a data table and leave visual gaps.

Approval still requires the exact revision ID/hash and audience. Inclusion,
amount-sharing and source-refresh changes therefore each require review of the
new saved revision. No backfill or automatic addition to existing posts occurs.

Database-free coverage lives in `startup_updates.tests_financial_history_unit`,
`startup_updates.tests_draft_disclosure_unit` and
`community_chat.tests.test_startup_publication`. Database integration coverage is
`startup_updates.tests_financial_history_database`; run it only against a fresh
disposable database after approval for the exact current migration inventory.
