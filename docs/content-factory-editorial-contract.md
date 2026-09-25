# Content Factory editorial and onboarding contract

Local implementation: 10–11 September 2026. This document describes code, not deployed state.

The founder frontend calls the authenticated `/api/v1/vibe-marketing` views. The backend owns organisation access, billing, approved editorial policy and dispatch. Content Factory owns model selection, research, repository changes, previews and release checks.

## Editorial catalog

The service-authenticated `GET /api/content-factory/org/config/?domain=…` response includes:

- `audience_options`: typed audience definitions with status, version and approval provenance;
- `cta_options`: typed offers with audience compatibility, markets, destinations, supported promises and approval provenance;
- `editorial_catalog_version`: the current catalog revision, initially zero.

The matching `PUT` accepts audience/offer lists and **requires** `expected_editorial_catalog_version` as a nonnegative JSON integer (not a boolean, float or string). Missing/invalid versions return 400; stale catalog or entry versions return 409. Omitted lists retain their current values; null/dictionary values are not shorthand for clearing a list. The individual audience/offer schemas are maintained in [editorial_contract.py](../content_factory/editorial_contract.py).

Catalogue mutations must be separate from general organisation settings: only `domain`, the expected version, and audience/offer lists are accepted by this service write path. It requires an existing organisation, locks and rereads its current strategy, validates the entire operation, then writes only the strategy field. A rejected edit cannot first create an organisation or change its name/keywords. General configuration and scan updates preserve the reserved catalogue.

The reserved `editorial_catalog` envelope lives in `OrganizationContentConfig.pillar_strategy`, using its existing JSON column. Generated pillar updates cannot replace it. Policy updates and legacy GitHub scan saves acquire the same organisation row lock and refresh the stored catalog before writing. This change introduces no migration.

Ordinary writes cannot create approval, including by copying `approved_by`/`approved_at`. Changed entries require a higher version, draft/retired status and cleared approval fields. Unchanged approved entries can remain approved. Retire entries instead of deleting them so old identifiers/versions cannot be recycled. Changing or retiring an audience also requires explicitly drafting/retiring each dependent approved offer at a higher version in the same save; do not silently carry compatibility approval to a different reader task.

The worker's catalogue and older CTA-only APIs both require the caller's expected revision and confirm content/revision by read-back. They expose backend 400/404/409 responses without fetching a fresh version and retrying a stale request. Failed or uncertain read-back means reload before retrying; it is not a promise that an already accepted write was rolled back.

## Owner approval API

`GET/PUT /api/v1/vibe-marketing/editorial-catalog/` and `POST /api/v1/vibe-marketing/editorial-catalog/approve/` inherit the product's authenticated JWT and cookie-Origin rules and require a founder-owned **explicit `company_id`**. Both trailing-slash forms are routed. A service API key is not a human approval identity. Domain or actor fields cannot select the approval tenant or approver.

1. GET with `?company_id=…`. Read the full audience/offer entries, their status and `editorial_catalog_version`. `review_entries` supplies each entry's `kind` (`audience` or `offer`), `id`, `version` and `content_sha256`.
2. PUT draft edits using that expected catalogue revision. Lists supplied are full replacements for that kind; omit the other list to preserve it. Existing changed entries need higher versions and cleared approval fields. Include dependent offer invalidation when changing an approved audience.
3. GET/review the saved content. POST only `expected_editorial_catalog_version` and an `entries` array containing the exact selected `review_entries` objects. Do not send an approver or timestamp. Audiences and offers may be approved together, regardless of selection order.
4. After the initial explicit-company lookup, the server locks the organisation, then the founder profile and company, inside the catalogue transaction. It rereads the authenticated user's profile identity/role and company ownership/organisation link before reading policy. A missing or no-longer-founder profile returns 403; a missing/transferred company returns 404; a changed organisation link or domain returns 409 and requires reload. Locks remain held through the write. It then verifies selected versions/hashes and offer dependencies/markets, and records `user:<authenticated-user-pk>` with an aware UTC server timestamp. Each receipt binds exact content/version; offer receipts also bind approved audience content. Approval history preserves the approved record snapshots and provenance. A stale selection returns 409; retired entries must be revised before approval.

An exact retry with the **current** catalogue revision is a no-op and does not rewrite the original approver or timestamp. An old revision is rejected even if the caller believes it is retrying the same operation. Existing approval history and receipts survive scans and draft edits.

The same locked ownership checks apply to draft saves and exact no-op retries. Service catalogue edits remain an independently authenticated draft-only path; the shared helper cannot approve without an owner context. These are catalogue-write guards, not automatic retirement of approvals previously granted by a person whose role later changes. Earlier approval history remains intact; retiring or revising an offer is an explicit policy mutation. GET retains the existing product-context resolution, and this change does not make all onboarding/offboarding operations atomic with catalogue mutations.

### Explicit legacy review requirement

Catalogue envelope schema 2 adds receipts/history inside the existing JSON column; there is **no database migration**. Reading a legacy `status=approved` entry without a matching server receipt returns it as a draft with cleared current approval fields. A read does not rewrite stored history. Changed/mismatched receipts also fail closed, and an unapproved audience invalidates dependent offer eligibility. Legacy records need explicit owner review/approval, not fabricated backfilled identities or timestamps.

Deploy this behavior only as a coordinated backend/worker/editor release. The website now contains a catalogue-management UI and article-brief selectors across all three start screens, with controlled local action tests. Real authenticated persistence, worker integration and explicit business approval still need verification before activation. No catalogue has been activated, reapproved or changed in production by this implementation.

When a catalog or editorial brief is supplied, the founder article endpoint requires a valid `editorialBrief` (also accepts `editorial_brief`), resolves its approved audience/offer versions and checks compatibility before charging or dispatching. The normalized brief is forwarded to the worker. Catalog-free legacy requests retain their existing contract. The website supplies a fresh company-scoped brief, but checking one earlier snapshot is not an atomic lease through later billing, dispatch or publication. Activating a catalog requires coordinated clients and worker paths that can supply and revalidate its brief.

## Article starts, restarts and dispatch retries

The coordinated worker main-start API now accepts the backend's normalised
`editorial_brief` instead of discarding it. It preserves the decision in queued,
awaiting-delivery-choice and setup-blocked requests and repair reconstruction.
Fresh worker admission reads use the existing `pillar_strategy` envelope,
receipt-backed lists and catalogue revision in the org-config response. Empty
configured policy requires a brief; missing response fields or lookup failure
cannot become legacy permission. No backend schema/migration was added for this.

The worker checks before its billing-authorisation verification and again before
durable article creation, rejects changed selected records during admission and
prevents different briefs being deduplicated by topic or substituted behind a
known key. This does not establish a real upstream charge/refund, a persisted
admission lease, every scheduled/resume path or real database/worker behavior.
The [article-start implementation record](../../mlai-au/docs/article-audit-2026-09-10/article-start-editorial-review.md)
documents 351 worker regressions and a controlled JSON interchange from the actual
backend pure catalogue helper, with synthetic approval and no database or HTTP.

`article_brief_for_catalog` in the pure catalogue module resolves both request spellings against current content-bound approval receipts. Conflicting spellings, explicit null/empty/invalid briefs and an empty configured catalogue cannot silently become legacy permission. Only an absent catalogue envelope **and** absent brief retain the catalogue-free legacy path. No-offer decisions stay explicit and require the current audience permission.

The founder article POST uses this resolver for its initial validation. The billing helper then rereads the organisation's current strategy before charging. Missing configuration or a database read failure returns 503 rather than creating/falling back to an empty catalogue; invalid policy returns 400 with `field: editorialBrief`. This is a fresh read, not a transaction held across the subsequent charge.

Restarts now preserve the original `run_request` brief, including reader task, contribution, acceptance criteria, ICP/offer versions, country and no-offer decision. They do not infer a replacement from generated result/package metadata. A missing/stale/invalid stored decision for a configured catalogue returns 409 before analytics provisioning or billing reuse. The current strategy is reread again before reusing payment; a failure there returns the same 400/503 policy response as above. The original request is not overwritten with a new inferred brief.

The shared queue rereads policy before **each** `endpoint=article` POST, including a second attempt after a transport error/5xx. Policy rejection stops new POSTs and creates the existing blocked local result with policy details in diagnostics. Other endpoints, such as discovery, do not acquire an article-brief requirement.

Refund and idempotency rules remain conservative:

- If policy changes after a POST may have reached the worker, stop retries but keep the same key and pending-resolution/refund metadata. Do not mistake a policy error for proof that the earlier POST failed.
- A charged request rejected before its first POST in the current call may still reuse a key from an earlier call. Look up that key. If a worker run already exists, acknowledge that existing run, record `diagnostics.editorial_policy_recheck` and make no new POST or refund. This acknowledgement is not new editorial approval.
- An unknown or immediately absent lookup retains pending resolution. The existing poll-time resolver may bind a late run or, after its 180-second grace and confirmed absence, invoke refund processing. This implementation does not establish that a real refund has occurred, or add a scheduler guaranteeing reconciliation when no client polls.
- Definitive worker rejection and unchanged-policy retries retain the existing queue/refund behavior. No changes were made to the Roo ledger implementation.

These checks do not close the read-to-charge/read-to-POST race or establish all-path enforcement. Verify the immutable brief survives actual worker callbacks and persistence, then cover direct/Roo/scheduled starts, revisions, resume, promotion and CMS paths. A worker acknowledgement does not establish an article's final source, resource, ICP or CTA quality.

## Durable worker snapshots and dispatch binding

The snapshot writer now preserves a known `run_request` editorial brief and its
`client_request_id` when an incoming worker observation omits them. Omitted or null
`run_request` objects are sparse observations, not instructions to clear the brief.
An explicitly null/changed brief, conflicting alias, changed dispatch key, changed
organisation domain or non-article workflow is rejected before run/step writes.
The embedded request domain must agree with the run. Article workflow aliases remain
compatible. Both brief spellings are accepted when identical and stored canonically
as `editorial_brief`; explicit no-offer decisions retain their reason and versions.

`PUT /api/content-factory/runs/<run_id>/` returns **409** with
`error: editorial_run_conflict`, `detail` and `run_id` for these conflicts. It does
not retry them as SQLite lock errors, and validation details do not expose raw
brief inputs. Existing cancelled and ignored-terminal responses remain unchanged.
Existing active-state blocker cleanup and Django-owned result fields are preserved.
An exact repeated sparse observation remains a no-op after brief reconciliation.

For a new ID, the writer validates before insertion, uses the locking queryset's
`get_or_create`, then reconciles again against the returned row before updating it.
This closes the control-flow gap where an intervening creation could otherwise be
overwritten using defaults validated against no existing brief. Created/updated
response semantics remain distinct. Real database races still require verification.

Dispatch binding locks the provisional and existing remote records. When either
contains an editorial decision, it checks the same identity contract before merging
token-only context with worker fields. A nonempty partial remote request no longer
causes the original brief to be discarded. Conflicts leave both records intact;
the existing best-effort binding wrapper returns None, without deleting the token
or rebinding billing. This is not a new transactional refusal of every caller's
remaining callback work. Rename-only binding retains the original request/history.

These checks preserve observations, not issue approval. Catalogue-free/null legacy
observations remain compatible. A structurally valid first-seen brief is not proof
of the human-selected dispatch or current eligibility. Do not manufacture missing
history, silently repair a malformed known brief or retire an old decision because
its offer was withdrawn. Failure/status history for withdrawn offers remains
recordable; current-policy validation belongs before generation/publication effects.

## Component-feedback revisions

The revision submission view rechecks the recovered source's organisation and
domain, extracts only its saved original brief and resolves it against current
receipt-backed policy. Missing/cross-organisation failed-source recovery is a 404;
malformed/conflicting known source decisions require repair. A failed child's
explicit known brief must agree with the recovered source. Generated result fields
and client replacement briefs cannot silently supply the decision.

The original decision is reread against policy before billing reuse, after billing
reuse before comment submission, and immediately before the component-revision POST.
The canonical `editorial_brief` and source domain accompany the worker request and
local child record. The worker's typed request must agree with its own saved source
request/context; it freshly checks selected catalogue records before billing
verification and again before child initialization/queueing. Source identity drift
and existing child ID/batch/comment/source conflicts return 409.

A policy block after submission retains the existing batch as `submitted`, records
`policyBlocked` and its error, and does not create a local running child. An explicit
permitted retry keeps that batch/key. Worker `editorial_*` and
`saved_editorial_policy_invalid` conflicts remain actionable 409/503 responses.
Transport uncertainty retains the existing retry path; neither a policy error nor
an absent immediate child proves a previous request failed or authorises a refund.

This requires coordinated backend/worker deployment: a new worker rejects missing
backend briefs for known catalogue-backed sources. Legacy worker sources without
any saved brief/options remain outside coverage, not inferred approval. Current
reads are not an atomic lease, and child lookup/creation/queueing is not a distributed
exactly-once claim. Verify real callback-before-local-creation, billing, persistence,
concurrency and final content separately. No production activation occurred.

## Non-terminal retained-task admission notices

The service-key-authenticated callback route accepts `article_admission_attention`
from retained direct/confirmed article-start tasks. Its `admission_notice` object
contains schema `2026-09-11.1`, an allowlisted `error_code`, `task_id` and
`brief_sha256` (explicit null only for an actually unbriefed original). The outer
run/job ID, domain, known repository and article workflow must match an existing
run; callback timestamps order observations, not run execution.

The handler locks/rereads that row and stores only a sanitized
`result.article_admission_notice`. It does not change the original request, run or
job/step status, event watermark, resume availability, billing or scheduled work.
No automatic retry, dispatch-token binding or refund is performed. Missing runs
return 409 so the worker outbox can retry after local persistence; conflicting
identity also returns 409 and needs explicit reconciliation, not inferred repair.
No new run or job is created from this observation. PUT and dashboard remote-refresh
merges preserve the notice; compact polling exposes it as historical information.

The matching website shows fixed recovery guidance and a read-only status refresh
with company-scoped catalogue settings. The new worker event needs a coordinated
receiver-first release. Local verification totals 136 no-database backend tests,
783 worker tests and 192 website tests; actual SQL/queue/payment/authenticated
delivery and browser behavior remain unproven. No migration or production effect
occurred. See the [callback implementation record](../../mlai-au/docs/article-audit-2026-09-10/article-admission-callback-review.md).

## Drafting during setup

Inventory scans send `generate_components=false`. Topic discovery can proceed while website setup is pending. An implicit article request in that state resolves to `content_only`, allowing research and drafting to proceed. An explicitly requested `publish_code` delivery still returns the setup readiness block.

Content-only delivery is a draft review surface. Exact website preview and publication retain the worker's integration/build/review checks. Existing callback deduplication, idempotent dispatch, organisation scoping and charging behavior remain in place.

### Article publish approval and retry

For founder Vibe Marketing runs, `POST /api/v1/vibe-marketing/runs/{run_id}/approve` is the initial article approval action. The backend rejects an older source run if a newer review-ready revision exists, including one reached through a failed intermediate revision. New article approvals require an exact hosted render, preview URL and valid commit SHA in `livePreview.proof`; a top-level commit field is insufficient. The passed, passed-without-baseline, or advisory hosted quality result must name the same preview URL and resume generation and include its input SHA-256 hash. After Content Factory accepts the approval, the backend stores a receipt in the source run's `run_request` with that exact review identity, authenticated founder actor, and server approval time. It also binds the top-level Content Factory run generation when provided; this is separate from the hosted preview's resume generation. A failed approval does not create or replace a receipt. Existing publish-child approval remains a separate PR evidence step and does not require article preview proof.

`promote-bundle` and `publish-pr` are retry actions. Before contacting Content Factory, they require the selected newest revision's matching receipt, approved state, exact hosted render, and current hosted quality status of `passed`, `passed_no_baseline`, or `advisory_findings`. Before a new source approval is sent to Content Factory, the backend saves a receipt-required marker. If the reviewed identity changes before the postapproval receipt is saved, the approve action returns a conflict; the marker prevents a recorded child from entering the older receipt-free retry path. An older approved run with an already recorded publish child and no marker may retry without a local receipt for compatibility; that path cannot initiate a fresh child. A changed run, run generation, preview URL, commit SHA, preview resume generation or quality input hash, or a regressed quality status, invalidates receipt-based promotion and requires a fresh review and approval. Content Factory still checks its hosted review evidence and source bundle at its own boundary. The frontend compares the visible review identity before it sends `approve`; the backend receipt records the server's saved review identity and explicit approve action, not a claim that the browser rendered the page.

`POST /api/v1/vibe-marketing/runs/<revisionRunId>/comments/accept-revision`
accepts feedback on a completed `article_revision` run. The request includes
the feedback `batchId`, `sourceRunId`, and the exact revision the founder
reviewed: `reviewedRunId`, `reviewedPreviewUrl`, and
`reviewedPreviewRevision` (the hosted render's commit SHA). The run must have
an exact hosted render and a current quality result of `passed`,
`passed_no_baseline`, or `advisory_findings` for the same preview URL and
preview-attempt generation. The quality input digest must be present. Missing,
pending, blocking, or stale quality and changed review identity return 409
before comments or learned preferences are promoted. Accepting revision
feedback is separate from approving the article for publication.

## Verification and rollout

On 11 September, **68 no-database tests pass**: 17 existing pure catalogue tests, 18 owner/service API tests and 33 policy/dispatch/CI tests. The latter execute the actual article-start body, restart, charge, queue, result mapping and poll-time reconciliation functions with controlled ORM/HTTP/billing seams. They cover all four ICPs plus explicit OUTSIDE, changed or unavailable policy, lost responses, same-key retries, deferred refunds and late existing-run binding. They do not prove JWT authentication, real ownership queries, SQL locking, ledger effects, rollback or persistence. The website's three focused editorial files pass 164 tests/888 assertions; typecheck/internal links and backend syntax/whitespace checks pass. The worker's earlier three-module 163-test result is historical and was not rerun in this dispatch pass. No production credentials, requests, migrations or deployments were used.

Run the backend's bounded no-database checks from the repository root:

```sh
.venv/bin/python -m unittest content_factory.tests_editorial_catalog_unit content_factory.tests_editorial_catalog_api_unit content_factory.tests_editorial_dispatch_unit content_factory.tests_editorial_snapshot_unit content_factory.tests_editorial_revision_unit
```

The backend validation workflow now includes this no-database unittest suite. Existing migration/deployment steps are unchanged; editing the workflow did not trigger it. CI execution remains unverified until an approved release workflow runs.

The subsequent snapshot/binding pass verifies **98 no-database tests**, adding 30
pure/in-memory-seam checks to the earlier 68. The new suite executes the actual PUT,
snapshot and binding function bodies and the real DRF sync serializer, covering
sparse/alias/no-offer/conflicting inputs, new-row creation ordering, a simulated
creation race, no-op callbacks, active cleanup, terminal behavior and SQLite retries.
The initial five persistence-seam tests failed before integration. Website editorial
tests still pass 164 tests/888 assertions, and typecheck/internal links and backend
syntax/whitespace checks pass. These are not real SQL, authentication, rollback,
ledger or distributed callback tests; no migrations or production operations ran.

The subsequent component-revision pass verifies **117 no-database backend tests**,
including 19 revision control-flow tests, and **250 worker API/artifact/catalogue
tests** with synthetic billing, queue and policy seams. Website editorial tests
remain 164 pass/888 assertions, with typecheck/internal links passing. Known source
and child conflicts, policy withdrawal before/after billing, exact retries and
actionable error mapping are covered. This does not prove real database, billing,
distributed queue or final article behavior. The revision module is included in
the same CI step without triggering it. See the [revision implementation and
verification record](../../mlai-au/docs/article-audit-2026-09-10/backend-editorial-revision-review.md).

Next verify actual PostgreSQL create/update/rollback and lock ordering, including
callback-before-binding and conflicting dispatch identities, with specific migration
approval. Complete the remaining direct/Roo/scheduled/revision/resume/promotion/CMS
entry paths and independent article/offer/pilot review. A passed mocked persistence
test does not close these requirements or prove that live articles improved.

Do not substitute `manage.py test`: database-backed integration requires specific migration approval. Use an approved isolated PostgreSQL transaction test to establish real row-lock behavior; SQLite does not implement `select_for_update()` ([Django reference](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update)). Remaining verification includes actual authenticated save/read-back, role/company changes while waiting for locks, rollback, cross-path lock ordering with domain/setup/offboarding operations, concurrent policy edit versus scan, exact content-bound approval, legacy review and every article dispatch/promotion/CMS path. Local model-provider credentials were not rechecked in this pass; earlier failed model tests do not establish current provider state.

See the [cross-repository implementation report](../../content-factory/docs/astra-onboarding-2026-09-10/README.md) for model routing, scaffold proof and remaining work.


## Article recovery and publication observations (14 September 2026)

Article serialization reports `liveVerification.state: unverified` with reason
`source_run_unavailable` when its writing run is absent or belongs to another
organisation. Missing historical evidence must not crash the article list or be
treated as verified publication.

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

## Customer profiles and historical attribution (16 September 2026)

The enriched catalogue record format uses `catalog_schema_version: 2`. Profiles add a required human-readable `name` and `description`, optional `pain_points`, `desired_outcomes`, and `knowledge_level`. Actions add `action_type` and `action_description`. Legacy records keep their original exact serialization; empty new defaults must not enter historical approval hashes. Admissions with enriched selected records use `schema_version: "2026-09-16.1"`; readers still accept `2026-09-11.1` admissions containing legacy records.

Owner catalogue GET accepts `include_suggestions=1` to retrieve `research_suggestions` from the startup's own saved `startup_autofill` run. Suggestions live outside `editorial_catalog` and never activate approval requirements merely by existing. An owner draft PUT may attach `suggestion_reference` (`research_run_id`, `suggestion_id`, `kind`, `entry_id`, `entry_version`). The locked write verifies the source run, domain and saved draft, then retains the original research evidence in `pillar_strategy.editorial_suggestion_reviews`, outside catalogue record hashes. Re-scans only propose additions/revisions.

Owner endpoints (explicit `company_id`, same founder authorization as catalogue):

- `POST editorial-catalog/suggest-brief/`: `topic`, `country`, optional `audience_id`, and `expected_editorial_catalog_version`. Returns a reviewable brief or a reason there is no fit. The worker rechecks the current catalogue after the model call. No approval or article dispatch occurs. Throttled to 12 requests/hour/user.
- `GET editorial-catalog/articles/`: paginated saved articles (25/page), `offset`, `q` title/keyword search, `audience_id` and `offer_id` filters. `audience_id=__unknown__` finds unrecorded profiles; `offer_id=__none__` finds intentional no-offer articles. Full snapshots remain owner-scoped.
- Discovery can accept optional `preferredAudienceId` and `expectedEditorialCatalogVersion`. The owner API resolves an approved profile; the worker verifies the same definition when research starts. This guides topic research without approving an article brief or altering keyword-level metrics.

WrittenArticle attribution is resolved from the exact tenant-owned saved writing run, never the current catalogue. Direct article sync now supplies `source_run_id`, stable `analytics_id` and `editorial_admission`. The same transactional service handles run materialization and direct sync, preserving the first snapshot, rejecting unrelated identity collisions, retaining writing identity through publishing children, and ignoring stale writing callbacks. Source discovery runs are not mistaken for writing parents. Sparse legacy observers cannot erase known attribution. A new catalogue-backed article with missing saved admission fails with a retryable conflict until the writing run is synchronized.

**Schema and rollout:** [`0041_writtenarticle_editorial_attribution`](../content_factory/migrations/0041_writtenarticle_editorial_attribution.py) follows the committed `0038_delete_seo_topicmap_researchsession` migration. It adds eight article fields and two organisation-scoped indexes; it does not infer or rewrite historical attribution. Apply the compatible schema before serving these model/API changes. Backend merges to main deploy automatically and run migrations, so production application requires explicit approval beyond the earlier disposable-local-only approval.

**Verification (17 September 2026):** PostgreSQL 17.11 replay from 0038 to 0041 passed, as did 88 targeted database tests (including three real row-lock/concurrency cases), 153 no-database unit tests, system checks, and migration drift checks. The disposable cluster has been removed; no persistent, staging or production database was changed during local validation. Reproduce the isolated integration run with `scripts/test_customer_profiles_database.py --engine postgres --replay` and the customer-profile, article-publish, article-discard, and workflow-run test modules.

`backfill_article_editorial --organization-id <id>` is dry-run by default; `--apply` writes only snapshots evidenced by each article's exact source writing run. A brief without admission remains `partial`; absent history remains `unknown`. Never use this command to classify from today's profile definitions. No backfill has been executed as part of this change.

## Article run workflow progress

`workflowProgress.currentStepId` on a specific article run points to that run's current generation, review, revision, or publication action. An incomplete organization baseline remains `ready` in `steps` and in the organization setup wizard; it does not replace the article action in the run-page header. A packaged draft with `article_preview_quality.status=blocking_findings` has a blocked Publish step linked to review. While that quality check is queued, running, or retrying transient findings, Publish is locked. Passed or advisory quality findings retain the normal publishing path. Existing PR or publication evidence remains authoritative for a publish run already in progress or complete.

Once an approved article's publish child is running or publication evidence exists, the Review/Revise stage reports complete and has no old acceptance action, even if the selected revision has no feedback batch of its own. The Publish stage continues to report its actual running, ready, blocked or completed state.

Full and compact article run responses include a bounded `hostedQualityIssues` list for claim-specific hosted quality errors. Each item has a claim ID, a short reason, and an optional component ID. Image findings use the captured image index only when the reviewed image count and signed component inventory agree. Resource and disclosure findings have no attested component location in the current report and remain in the list without a page anchor. The current visible reviewer permits no article repair and records exactly one attempt; multi-attempt reports fail closed until their final claim identity has an explicit contract. The projection requires the exact available render, preview URL, preview attempt generation, and a review time after preview startup. It supplies no section removal authority. The persisted quality report and approval gate remain authoritative.

## Vibe Marketing response size

The bootstrap response keeps complete recent runs in `latestRuns`. Its
`latestRunsByWorkflow` field is a small index of the newest run per workflow,
carrying identifiers, status, approval and publish control state. Clients that
need the run's result or review artifacts use `latestRuns` or the individual
run endpoint; the index does not repeat those large values.

An individual full article run exposes `reviewDraftHtml`, `componentManifest`,
`contentPackage`, `sectionIssues`, artifacts and diagnostics in their dedicated
fields. Its `result` still carries workflow and publish control values. The
response omits only identical redundant copies; `contentPackage` is metadata,
so a raw delivery package with article content remains available. Distinct
nested review HTML and component manifests also remain available. This
projection does not change the saved run or the article approval checks.
Compact run status responses continue to omit article HTML. Nested worker
artifacts, diagnostics and section issues are projected to the dedicated fields
when they are the only available source; a different nested value stays in
`result`.
