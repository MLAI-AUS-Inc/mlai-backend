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
