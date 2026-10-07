# Articles activation and Pulse source selection

Local implementation, 4 October 2026. This describes code and test contracts;
it does not establish production deployment or live OAuth/worker behavior.
No new model, migration or production data repair is included.

## One capability contract

The existing authenticated Vibe Marketing bootstrap includes
`articleCapabilities` version 1. Dashboard, Articles and website setup consume
that projection. Missing capabilities must disable article creation until the
client obtains current state.

| Field | Meaning |
| --- | --- |
| `canResearch` | A startup website/domain exists; GitHub/setup is unnecessary. |
| `canGenerateArticle`, `canPublishArticle` | Current repository write access and verified integration both pass. |
| `stage` | `github`, `repository`, `integration`, `verifying`, `unavailable`, or `ready`. |
| `reasonCode`, `reason` | Stable blocker and concise next action. |
| `githubConnected`, `accountStatus` | Saved account authorization, distinct from actual repository access. |
| `repositorySelected`, `repositoryAccessVerified` | A selected repo versus a current GitHub write-access check. |
| `integrationVerified`, `verifiedAt` | Fresh proof for the selected repository/default branch/head. |
| `routePath`, `surfaceLabel` | The actual content surface, including `/stories` / `Stories`. |
| `nextStep` | Existing website setup step `repository` or `articles`. |
| `prices` | Integer Roo quotes: `article`, `researchTopic`, `research`, `islandResearch`, `automationResearch`. |

Saved expired OAuth credentials with a refresh token or App installation show
`checking`; they are not presented as never connected. Expired credentials with
no recovery path show `needs_action`. Founder ownership is required to use an
organisation's saved credentials. The account resolver is shared with the
Connections website-source cards. It does not treat token presence, refresh
presence or an installation record as proof of repository access.

The repository probe resolves the existing App/OAuth token, checks repository
identity, write permission, current default branch and commit SHA. Bootstrap
may reuse a successful check for 60 seconds (failed checks for 15); producing
mutations force a fresh check. Access revocation and GitHub outage are distinct
blockers. Tokens are neither returned nor stored in the capability cache.

## Integration evidence and admission

A completed scan must carry an explicit ready verdict, the selected repository,
its default branch/head, a timestamp no older than seven days, and the same
safe direct publish target currently selected in configuration. The scan's
own verdict/targets are used together; another scan or raw persisted readiness
cannot fill missing proof. The GitHub probe must still match its branch/head.
Pending setup must be merged and published before verification can grant access.
Reset/disconnect markers, provisional/structural-only targets and bundle-only
fallbacks block production.

Article history, a public Stories listing, `articles_scaffolded`, and a merged
setup PR alone cannot grant permission. Old stored scans lacking this proof
require a new verification. A changed repository invalidates old drafts;
a changed default branch/head requires another verification.

The backend checks this before charging or dispatching new articles, restart,
resume, AI comment revisions, image regeneration, preview-quality retry,
bundle promotion and publication controls. Older authenticated and Roo service
entry points use the same resolver. Poll-driven auto-merge uses it too. The
legacy combined `verify-merged-setup` worker action can resume historical
parents, so it is suppressed until strict readiness passes; use the existing
inventory/rescan verification path first. Blockers return 409 with
`code: article_system_setup_blocked`, `reasonCode`, `articleCapabilities` and the
next setup step. Saving/viewing existing drafts remains available.

Integrated Content Factory workers fetch admission immediately before article
producing steps, retries/resume, image generation and publication. The existing
service-authenticated `GET /api/content-factory/org/config` accepts
`article_admission=1`, exact `domain`, `github_repo` and
`requested_by_slack_user_id` (legacy `slack_user_id`). It requires an existing
owned configuration and resolves the actor to its account. Only fresh ready
capabilities produce 200 `{domain, github_repo, articleCapabilities}`; missing
scope is 400, invalid actor 403, changed repository or blocked readiness 409.
The worker requires version 1, matching scope and every ready capability;
cached org snapshots and fuzzy-domain lookup cannot supply admission.

Release this backend branch and the coordinated Content Factory worker changes
together. An older backend deliberately blocks integrated workers. Standalone
workers without an MLAI backend keep their existing policy.

## Research billing

Topic discovery is available before publishing setup. Each requested topic
costs one Roo Point, bounded to 1–8 topics; the default four-topic batch costs
four. Brief-led island exploration costs one point. Scheduled and manual
notification research creates three topics and costs three points per run.
`prices.automationResearch` advertises that quote. Articles cost six. The
existing `mlai.au` free-domain exception remains. Bootstrap advertises the
same values used by charging.

`expectedCostPoints` (or legacy `expected_cost_points`) is optional for older
clients. New clients submit their confirmed integer quote. A changed/invalid
quote returns 409 `roo_points_quote_changed` with the current `costPoints`
before any debit. Research keys are bound to payer/startup; ledger and remote
dispatch share the same key. Retry reuses a durable run. An ambiguous queue
response holds the debit until key lookup establishes whether the worker
accepted it; confirmed rejection uses the existing idempotent refund path.
Brief-led terminal failure or empty results refund the recorded payer once,
including through durable snapshot callbacks when the browser has closed.
Paid notification research uses the same ledger/remote key and queue recovery.
Run-now accepts `companyId`, `expectedCostPoints`, and `idempotencyKey` or
`clientRequestId`; its scoped key reuses even a terminal run on transport retry.
Enabling reminders checks the same quote before saving the schedule. Empty
topic-selection callbacks and terminal research failures refund research cost,
independently of later article confirmation.

The existing island-research/adoption endpoints already used by clients are
ported into this checkout. Adoption uses measured proposals stored on an
owner-scoped completed research run and is free. No alternate transport or
schema is introduced.

## Pulse and Connections

The Chat facade adds GitHub and Search Console to the existing Connections
source response as website capabilities. They are not selectable Pulse inputs.
Website OAuth initiation uses the existing signed browser ticket/session and
company ownership checks. The browser returns to
`/my-startup/connections?company_id=…&connected=…`; this plain marker is not an
authorization receipt. Clients confirm fresh account/company/provider state
before continuing a stored draft.

Native OAuth accepts signed `returnTo: mobile` on connect initiation and returns
only `mlaichat://connections?company_id=<uuid>&provider=<key>`. The native return
URI allowlist rejects arbitrary hosts, paths, fragments and additional query
data. Callback context revalidates the revocable Chat session and original
company/account/organization; revoked sessions and transferred startups cannot
finish pending consent.

Chat routes now support `POST sources/<provider>/` with boolean `enabled` for
default inclusion, and `DELETE sources/<provider>/` to disconnect that user's
selected startup connection. Luma/Humanitix `POST connect/<provider>/` with
`apiKey` uses existing provider services and returns `connected: true` only
after the resulting source status is confirmed connected.

Inclusion preferences use reserved `chat_source_preferences` in existing
StartupProfile.progress_configuration JSON, scoped by company and owner account. Writes
lock and reread the organization and profile. No credentials or schema changes are required.
Worker/public pillar strategy also strips legacy reserved preference metadata.
Never-connected sources default off; existing connections default on. Explicit
saved preferences survive revocation and remain independent of data readiness.
Google Drive authorization is visible but update imports are unavailable in
this backend, so it cannot become a selected generated-update input.

Pulse generation validates its effective selected sources before shared
billing/dispatch. A selected disconnected or syncing account, an unavailable capability,
or a missing source selection returns 409 `startup_update_sources_unavailable` with `unavailableSources`.
The user can reconnect, or explicitly remove that source and retain notes or
documents. Notes-only generation does not require an OAuth account. Default inclusion preferences (`enabled`) do not remove a usable source
explicitly selected in a saved draft. Google Analytics remains an accepted reporting source rather than being silently
removed by the founder facade.

## Verification boundary

Website setup recovery keeps terminal runs and their operation ledger consistent.
A cancelled or denied dispatch response cannot reopen an operation. Starting a
new reviewed setup reconciles a stale active operation only when its saved run
matches the company, connection generation and operation attempt, then retains
the new request identity. Duplicate requests for an in-flight setup retain the
original identity.

The durable run PUT accepts cancellation receipts for the exact saved workflow
attempt, including an already cancelled run. That receipt can update run history
and operation status, but cannot write website configuration, revive work or
change a completed operation. Current setup preview failure callbacks remain
valid after the corresponding terminal snapshot; old attempts and generations
are rejected. An explicit `retryable: false` failure or a completed, cancelled or
denied lifecycle suppresses Resume and Retry controls in run serialization.

The recovery regressions run without a database or network:
`python scripts/test_without_database.py
content_factory.tests_website_operation_recovery_unit
content_factory.test_reliability_contract`.

Run `.venv/bin/python scripts/test_without_database.py
content_factory.tests_activation_unit content_factory.tests_island_research_unit
content_factory.tests_editorial_dispatch_unit
content_factory.tests_editorial_revision_unit
content_factory.tests_editorial_snapshot_unit
community_chat.tests.test_startup_connections_unit
content_factory.tests_research_automation_billing_unit`.

These suites exercise real DRF parsing/dispatch and implementation functions
with synthetic ownership, ledger, HTTP and worker seams. The runner forbids
both database and network access. They verify admission order, evidence scope,
expired/revoked credentials, outages, quote changes, request-key scope,
callback preservation and refunds. They do not prove SQL locking, persistence,
real session/OAuth authentication or production worker behavior. No trusted
already-migrated disposable database was found locally, so no database-backed
runner or migration was invoked.

Pulse resource scope is automatic for the reporting month. Legacy manual `selected` flags are not admission requirements; current status, availability and explicit data usability determine readiness.
