# Reporting canaries and historical replacements — 10 September 2026

Status: local reporting verification passed and release PRs are in progress. No live reporting canary has completed and no historical publication has been replaced.

## Production evidence handling

Keep live financial amounts, source documents, account identifiers and draft identifiers in the private audit record. Public repository documentation records the test procedure and pass/fail outcomes only. Compare each generated Revenue value against an authoritative report with identical dates, currency and calculation basis; accounting entries may change between fetches.

## Changes prepared

- Xero Revenue comes from the exact report total and verified organisation currency. Reports use an explicit accrual basis and cutoff and preserve signed adjustments. One month's fetch failure does not discard successful reports for other months. Partial-period growth is withheld.
- Stripe is freshly synchronised before capture, stays separate from Xero Revenue, and retains its processor-only basis. Failed refreshes carry stale evidence warnings; verified empty periods may be zero.
- Gmail eligibility uses messages inside the reporting window, so later replies cannot hide an earlier thread. The reporting extraction no longer stops after 40 threads. Source receipts retain full in-period text and attachment evidence and identify the extraction contract.
- Notion fetches all pages of children and nested blocks, caches by source version, processes overlapping chunks and flags documents edited after the reporting cutoff. Current document text cannot establish its historical contents.
- Uploaded originals are re-parsed under the new parser version. Word/PowerPoint tables and spreadsheet empty cells survive extraction. Full text is frozen; bounded quote extraction visits the text beyond the old 12,000/40,000-character limits and rejects invented quotations. Image-only PDF pages are explicitly flagged for OCR/review.
- Worker checkpoints preserve backend-owned source receipts, founder choices and snapshot pins. Snapshot capture rejects unfinished staged extraction. Historical metric series are frozen and exclude archived unverified values.
- Narrative sections remain visible alongside charts. Missing values stay unknown rather than becoming zero. Period notices display the startup's timezone and work for narrative-only updates.
- Reporting classification, source extraction, curation, drafting, document quote extraction and groundedness review use the configured Astra models. Worker leases renew during long calls. Completed document chunks, months and revision reviews survive retries; review receives one exact revision and its evidence once.

## Local verification

Completed:

- Valley: 137 tests passed, including complete thread/document coverage, exact source quotations, immutable preparation and retry reuse.
- Backend: 24 pure tests passed without constructing a database, covering revenue/receipt rules, periods, signed report totals, source fingerprinting, February reply exclusion, nested Notion blocks and uploaded table extraction.
- Frontend: 10 tests passed; TypeScript and production build passed. Checks include narrative with charts, missing versus zero, frozen history and timezone cutoffs.
- Django system checks passed. Read-only migration drift check reported no changes.

Additional verification:

- Database regression run completed: 273 tests, OK with three PostgreSQL-only skips. This includes all seven new database canaries, revision/API regressions, connector flows, worker checkpoint tests and founder APIs.
- Full canonical local suite: 2,110 tests completed successfully with 30 platform/feature-specific skips. The final temporal-context changes also passed 45 focused database tests.
- Still pending: live generation, real browser two-tab approval, provider-cohort canaries, historical rebuilds and replacement approvals.

The user approved the specific disposable test-database migrations listed in [reporting-canary-test-database-approval.md](reporting-canary-test-database-approval.md). The reporting code adds no migration. Production's read-only migration plan was empty when inspected.

## Release and canary sequence

1. With test-database approval recorded, run the targeted backend suites on a new disposable database: `startup_updates.tests_reporting_canaries`, `startup_updates.tests_revisions`, `integrations.tests_startup_updates`, `integrations.tests_connectors`, and financial publisher tests. Run the relevant workflow checkpoint/cancellation regressions and canonical CI checks. Fix failures before release.
2. Create the three PRs. Verify current main has not introduced additional migrations before CI. Merge and deploy the backend contract, Valley worker and frontend, pausing incompatible monthly dispatch during the transition. Update the deployed reporting overrides `OPENAI_REASONING_MODEL` and `OPENAI_CLASSIFIER_MODEL` to `gpt-6-astra`; defaults alone do not replace environment overrides. Confirm exact release SHAs, API readiness and worker health, then restore dispatch.
3. Generate August privately. Record run ID, model IDs, stage timings, selected sources, reporting period, source coverage, snapshot ID/hash, draft revision ID/hash and review verdict. Reconcile Revenue to the simultaneously fetched authoritative Xero report on identical dates, AUD and accrual basis. Require exact Decimal equality. Do not add Stripe or bank receipts to Xero Revenue.
4. Generate February privately. Include a February email with a later reply: only in-period messages and attachments may support February. Include a later-edited document with an explicitly dated February fact. Its receipt must identify the current source revision and retrospective limitation; undated later edits cannot become February events. Include an uploaded-only table fact beyond the old text limits and a fact beyond the old thread/block limits.
5. Generate September month-to-date privately. Record the source cutoff and startup timezone. Confirm an incomplete period, unavailable metrics, genuine zero, failed sources and stale values remain distinguishable. Do not show a month-to-date/full-month growth comparison.
6. For each canary, compare every displayed KPI and target chart point to the exact snapshot. Compare all narrative sections, asks, learnings and next steps between edit preview and the saved/published revision. Private publication means `just_me`; public disclosure needs the separately reviewed audience revision. Inspect source provenance for each substantive narrative claim, not merely whether an extraction contains the same claim.
7. In two browser tabs, open revision A in both. Save revision B in the second tab. Attempt to approve A in the first. Require HTTP 409 and no publication change. Reload, review B and verify only B's exact ID/hash can be approved. Repeat with a change of audience/disclosure.
8. Run the cohort matrix: Stripe-only; Xero+Stripe; narrative-only; domainless/non-Australian with a non-USD currency and timezone. Confirm source precedence, absent finance charts, uploaded-only facts, custom metric definitions and no company-registration requirement. Label synthetic ORM/API results separately from live connected-account results.
9. Only after all canaries pass, rebuild February–August in chronological order. Use versioned source receipts from corrected extraction; a legacy processed flag is not a cache hit. Save replacement drafts privately and retain existing published revisions. Record old/new revision hashes, Revenue/cost differences, narrative additions/removals, source changes, limitations and validation verdicts.
10. Review each replacement in full and explicitly approve its exact revision ID/hash and intended audience. Preserve the previous publication until that approval succeeds. Verify the resulting archive, cards, charts and detail view against the replacement snapshot. Save an audit record of every approval and retained old revision.

## Remaining limits to measure

No production speed-up has been measured yet. Record p50/p95 generation times and stage/token/source counts; do not infer a speed-up from model choice. The current review retains full raw evidence for one month and can still become large. The extraction receipts live in durable run JSON, which avoids a new schema migration but may need indexed evidence/chunk tables for substantially larger datasets. Slack/Linear and other connectors need their own complete pagination, temporal and coverage canaries before claiming the same assurance as the Gmail/Notion/upload paths tested here. A health assessment is qualitative until an explicit, validated scoring contract exists; do not substitute a fabricated health score.
