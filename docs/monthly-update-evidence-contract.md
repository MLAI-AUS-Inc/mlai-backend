# Monthly update evidence and revision contract

Implementation: 9 September 2026. The user approved the specific additive migration and disposable local database verification. The migration has been created and exercised locally; production deployment has not been performed.

Monthly updates now have one versioned evidence snapshot, immutable content revisions, and an approval receipt for the exact revision and audience. The active product supports private business health and founder-approved community updates. Investor discovery, fabricated activity, and investor-specific publication choices have been removed. Historical enum values remain readable for compatibility.

## Evidence and revenue

`startup_updates/evidence_contract.py` defines hashes, reporting periods, metric substitution, and deterministic financial guards. `startup_updates/revisions.py` captures startup ID/name, reporting configuration version, period boundaries, metric values/provenance, chart data, and approved narrative evidence. Captured artifacts are copied into the snapshot; they are not rehydrated on a read.

Revenue uses Xero Profit and Loss revenue when available. In the separate financial sync path, an active Xero connection requests accounting reports instead of adding Stripe receipts. The fallback is paid Stripe **invoice sales excluding tax**, with explicit partial-coverage metadata. It does not claim whole-business accounting revenue. Unpaid invoices are excluded; supplier payments never qualify as customer receipts. Ambiguous credit notes, partial payments and missing tax totals remain unknown. Refunds made outside invoice credit notes and non-invoice Stripe payments are not yet reconciled; the UI states this limitation.

Only observations with the accepted calculation basis enter Revenue snapshots. Missing currency-matched observations stay unknown. Last-good Xero observations are retained on failed refreshes. Current-month report requests stop at the reporting cutoff. Stripe paid timestamps are bucketed in the configured timezone. Currency exponent conversion supports zero-, two-, and three-decimal currencies. Stripe pagination continues to completion and rejects a non-advancing cursor.

The former invoice/bill fallback charts and proportional category allocations have been removed. Historical chart gaps are null. New memo KPIs carry numeric values and units from the snapshot; the history serializer does not regex-parse unquantified founder assertions or join different currencies into one series.

Generated financial claims must use `{{metric:revenue}}` style references. The server substitutes the snapshot display value, replaces generated KPI displays with snapshot values, and rejects detected unbound financial amounts. This is a deterministic guard, not a proof that every narrative claim is true. Groundedness review and founder review remain necessary.

## Worker protocol

1. `POST /api/v1/integrations/startup-updates/runs/{run_id}/evidence-snapshot` captures each requested month's evidence once. The run pins `snapshot_id` and the base `expected_revision`.
2. Valley generates a private draft from that frozen payload. Live context and newly fetched memory are not merged back into generation or review.
3. Each draft-result write includes `snapshot_id` and `expected_revision`. Django locks the run and draft, checks startup/month ownership, and creates a numbered revision.
4. The response includes `revisionId`, `revisionHash`, `snapshotId`, and the private evidence payload. Valley stores these exact returned references.
5. Groundedness review submits `revision_id` and `revision_hash` for those saved revisions. Stale writes return HTTP 409. Review attaches validation metadata without rewriting content.
6. A retry after a retryable save failure resubmits the stored generated payload. The backend only acknowledges duplicate generation when its input hash and current revision agree.

Candidate auto-selection is not publication approval. Generated revisions start with pending validation. Pending, failed, and needs-review validation block publication.

## Founder API

`GET /api/v1/vibe-raising/business-health/?company_id=...` reads the evidence snapshot of the latest working revision. It reports observed values, missing evidence, source limitations, and a costs-above-revenue flag only for comparable Xero values. No dashboard score or investor activity is inferred.

`POST /api/v1/vibe-raising/business-health/` accepts `companyId`, `timezone`, `currency`, and optional metric definitions (`key`, `label`, `definition`). The UI can add a definition with `metricLabel` and `metricDefinition`. Changes increment the configuration version; prior snapshots do not change.

`POST /api/v1/vibe-raising/updates/` requires the form's `companyId` and `expectedRevision` for edits. It returns a saved revision receipt. A changed founder value becomes a clearly labelled founder assertion in a new snapshot. Unchanged evidence is copied from the previous snapshot.

`POST /api/v1/vibe-raising/updates/{id}/publish/` requires `companyId`, `revisionId`, `revisionHash`, and `audienceVisibility`. Approval and publication occur in one transaction. Audience must match the saved disclosure choice. A changed revision or disclosure returns 409. Duplicate approval is idempotent.

Published reads use `published_revision`; editing updates `current_revision`. A cancelled worker cannot remove an approved publication or a later founder edit. Source erasure examines historical evidence revisions too; copied Slack evidence is removed even when the current memo no longer cites it. Community output includes selected metric values and safe quality annotations; it omits private evidence and private financial charts. It is the founder-selected disclosure of the same evidence base, not a separate unconstrained LLM draft.

Existing updates without revisions remain labelled `legacy_unverified`; reading does not recalculate their historical values or pretend they were newly approved. They must be saved and reviewed under the new contract before republishing.

## Startup identity and browser behavior

Company ownership and stable organization IDs are authoritative. Domainless companies receive a reserved internal `startup-{company.pk}.invalid` routing alias while their visible website field remains empty. This bridges older domain-keyed integrations without requiring a public website. Existing organization mappings survive company website changes.

Reporting timezone and metric definitions belong to the startup. The monthly-update UI no longer requires Australian business registration. Save and publication forms carry the displayed company ID. Manual source selections are stored per user/company in session storage. Debug logs/caches containing publish form content and local-success publication fallbacks have been removed.

## Approved migration and rollout

Created migration: `startup_updates.0022_reporting_evidence_revisions`, depending on `0021_linear_project_member_artifact` and the user-model dependency.

- Create `MonthlyEvidenceSnapshot`: organization, month, SHA-256 hash, JSON payload, capture timestamp; unique organization/hash.
- Create `MonthlyUpdateRevision`: draft, protected snapshot FK, revision number, audience, SHA-256 hash, structured/rendered content, validation metadata, creation timestamp; unique draft/revision number.
- Create `MonthlyUpdateApproval`: one-to-one revision, nullable approving user, revision hash, audience visibility, approval timestamp.
- Add nullable current/published revision pointers to `MonthlyUpdateDraft`.
- Add `reporting_timezone` (UTC default) and `reporting_config_version` (1 default) to `StartupProfile`. Currency and KPI definitions already exist.

This is an additive schema migration. It does not rewrite existing updates, approve old content, contact providers, or publish anything. Django created and applied these operations successfully in a disposable SQLite test database. Migration consistency checks report no remaining model changes.

Deployment remains a separate action. Release in this order:

1. Pause monthly-update worker dispatch while the incompatible API contract changes.
2. Apply `startup_updates.0022_reporting_evidence_revisions` in the separately authorized deployment environment, then release Django's API.
3. Release the matching Valley worker and frontend together. Older workers cannot submit the required snapshot contract, and old editor clients cannot approve revisions.
4. Resume dispatch and smoke-test a startup's snapshot capture, generation/review, private health, founder edit and community publication using the same receipt.
5. Keep historical updates marked legacy/unverified. Saving an old published update archives its existing public content verbatim, creates a new working revision, and requires approval before replacement.

## Verification

- Django system checks and migration consistency checks pass with environment-file loading disabled. Database integration runs use disposable local SQLite only.
- Pure reporting contract: 14 tests pass (supplier receipts, payment/tax evidence, unknown values, DST, hashes, metric tokens).
- Fourteen new revision/API integration tests cover snapshot pinning, duplicate generation, mutable source observations, wrong currency/tenant, stale approval, disclosure, published-copy preservation, configuration validation and domainless founders. The existing regression suites have been updated for the new contract.
- Backend regression run: **320 tests completed, 317 passed and 3 PostgreSQL-only tests skipped** under SQLite. This includes historical Slack evidence erasure. PostgreSQL concurrency tests remain a deployment-environment check.
- Cross-repository test results are recorded in Valley's dated implementation report.

Stripe field semantics: [Invoice object](https://docs.stripe.com/api/invoices/object).
