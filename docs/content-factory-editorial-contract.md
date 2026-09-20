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

## Generation notifications

Content Factory automation emails are sent only for a completed content draft or an article ready for review. Topic selections and delivery-mode prompts remain available in the application and on opted-in WhatsApp/Slack channels; they do not send email. WhatsApp daily research consent and delivery selection are unchanged.

Generation/research errors update durable run diagnostics and operator logs without notifying customers. A late error callback cannot overwrite a completed automation run or its review link. Actual failures remain visible in the application; suppressing a notification never converts a failed article into a completed draft. This policy is limited to Content Factory automation delivery, not account/security emails.

## Verification and rollout

Four pure catalog contract tests and edited-module syntax checks passed for the 10 September work. Database-backed integration tests have not been run because the repository requires specific migration approval for test database construction. Deploy the backend, worker and frontend contracts together after end-to-end verification; the local OpenAI key currently returns 401, so live Astra/Fast verification remains outstanding.

See the [cross-repository implementation report](../../content-factory/docs/astra-onboarding-2026-09-10/README.md) for model routing, scaffold proof and remaining work.


## Article recovery and publication observations (14 September 2026)

The reliability implementation is additive and uses existing run JSON; it adds no migration. Worker snapshots and callbacks carry `generation`, `state_version`, typed `failure`, and `recovery`. State versions order writes within an execution generation. A newer explicit resume generation supersedes earlier events. The backend validates versions before handlers can mutate state, refund or notify, and serializes callbacks/status sync under the run row lock. Failure to store the event-ID claim defers delivery instead of executing without deduplication. Once a run adopts this protocol, unversioned events cannot overwrite it.

The dashboard receives the same recovery decision used by the worker. A pending automatic retry suppresses manual retry controls. Unknown failure codes preserve their message and action.

Sitemap entries are candidate URLs, not live-content proof. Publication refresh observes the current PR merge commit and compares an exact normalized article body/canonical URL with the saved source package. The `release_observations` namespace retains the HTTP evidence, content hashes, target commit, check time and last verified observation; worker polling cannot replace it. Changed input or target invalidates its current applicability. Historical on-main/publish facts remain intact. The dashboard distinguishes Merged from a recent verified Live observation. Browser/CSS visibility and hosting-provider deployment-job success remain separate from this HTTP check; JavaScript-only bodies stay unverified here.

Pure serializer/ordering/content-identity tests run without database access:

```sh
python -m unittest content_factory.test_reliability_contract -v
```

After explicit approval to apply existing migrations to a temporary test database, validate the real handlers, refunds, snapshot reconciliation and publication observations using Python 3.11:

```sh
APP_ENV=test DATABASE_URL=sqlite:////tmp/mlai-article-reliability-tests.sqlite3 \
  python manage.py test tests.test_content_factory_callback_idempotency \
  tests.test_content_factory_run_sync content_factory.tests_article_publish_status \
  integrations.tests_article --noinput
```

Deploy the backend's additive readers before compatible factory API/worker images and then the dashboard. No production article has been resumed or published as part of this implementation. Validate one controlled authoring and revision recovery at the normal approval boundary, then the plan's representative-run sample before broad rollout.
