# Content Factory editorial and onboarding contract

Local implementation: 10 September 2026. This document describes code, not deployed state.

The founder frontend calls the authenticated `/api/v1/vibe-marketing` views. The backend owns organisation access, billing, approved editorial policy and dispatch. Content Factory owns model selection, research, repository changes, previews and release checks.

## Editorial catalog

The service-authenticated `GET /api/content-factory/org/config/?domain=…` response includes:

- `audience_options`: typed audience definitions with status, version and approval provenance;
- `cta_options`: typed offers with audience compatibility, markets, destinations, supported promises and approval provenance;
- `editorial_catalog_version`: the current catalog revision, initially zero.

The matching `PUT` accepts the same audience/offer lists and optional `expected_editorial_catalog_version`. Send the expected revision to reject stale edits. Omitted lists retain their current values. Validation errors return HTTP 400. The individual audience/offer schemas are maintained in [editorial_contract.py](../content_factory/editorial_contract.py).

The reserved `editorial_catalog` envelope lives in `OrganizationContentConfig.pillar_strategy`, using its existing JSON column. Generated pillar updates cannot replace it. Policy updates and legacy GitHub scan saves acquire the same organisation row lock and refresh the stored catalog before writing. This change introduces no migration.

Approved entries require approval provenance; changed entries require a higher version. Approved offers must name known audiences and explicit markets. Do not infer approval or eligibility from generated copy. The worker's catalog API reads the saved value back after a write to detect an incompatible backend deployment.

When a catalog or editorial brief is supplied, the founder article endpoint requires a valid `editorialBrief` (also accepts `editorial_brief`), resolves its approved audience/offer versions and checks compatibility before charging or dispatching. The normalized brief is forwarded to the worker. Catalog-free legacy requests retain their existing contract. A management/brief-selection frontend is still needed before exposing catalog editing to founders; activating a catalog requires a client that can supply its brief.

## Drafting during setup

Inventory scans send `generate_components=false`. Topic discovery can proceed while website setup is pending. An implicit article request in that state resolves to `content_only`, allowing research and drafting to proceed. An explicitly requested `publish_code` delivery still returns the setup readiness block.

Content-only delivery is a draft review surface. Exact website preview and publication retain the worker's integration/build/review checks. Existing callback deduplication, idempotent dispatch, organisation scoping and charging behavior remain in place.

## Verification and rollout

Four pure catalog contract tests and edited-module syntax checks passed. Database-backed integration tests have not been run because the repository requires specific migration approval for test database construction. Deploy the backend, worker and frontend contracts together after end-to-end verification; the local OpenAI key currently returns 401, so live Astra/Fast verification remains outstanding.

See the [cross-repository implementation report](../../content-factory/docs/astra-onboarding-2026-09-10/README.md) for model routing, scaffold proof and remaining work.
