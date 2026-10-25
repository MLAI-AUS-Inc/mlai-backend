# Website connection lifecycle

The website connection is the authority for repository work. GitHub installation access is independently reusable across companies; authorizing GitHub does not revive a disconnected website. The additive `content_factory.0042_website_connection_lifecycle` migration adds six tables and a nullable selection on `OrganizationContentConfig`. Existing rows receive no implicit authorization. Migration application and production repair require their own explicit approval.

## Stored ownership

| Record | Purpose | Lifecycle |
| --- | --- | --- |
| `WebsiteConnection` | Company, immutable GitHub repository ID, installation, app root, branch, site, state, monotonic generation | Disconnect keeps identity and history; reconnect verifies GitHub and advances generation |
| `WebsiteConnectionTarget` | Adapter contract and exact-commit verification | Available only in the generation that verified it |
| `WebsiteScanSnapshot` | Inventory evidence and detector/source identity | Immutable and independent of optional model enrichment |
| `WebsiteTemplateRevision` | Template digest, body, provenance and validation | Legacy prompt envelopes are quarantined; archived versions remain recoverable |
| `WebsiteRepositoryMutation` | Operation identity, commit identities, file ownership/hashes, branch and PR receipts | No credentials or source bytes; retries cannot replace a different patch |
| `WebsiteConnectionOperation` | Idempotent lifecycle receipt, cancellation/revocation outbox and cleanup proposal | Retried with bounded backoff; no claim that remote actions were undone |

Company facts, editorial policy, generated articles and history remain company-owned. Repository-derived mutable paths, templates, targets, components and design projections are invalidated on rebinding/reset/reconnect. They are not copied into another repository. Template versions are archived first.

## Contract

Every repository request, saved run, child run, token request, callback and repository-derived config update carries `website_connection_id` and `connection_generation`. `repository_id` verifies GitHub's immutable identity. `connection_target_id` identifies a verified target for publishing; when omitted the selected default must be verified. Worker retry `generation` is a separate counter.

`GET /api/content-factory/connections/authorize` accepts that tuple, `domain`, `github_repo`, and `action=read|scan|config_write|preview|setup|publish|merge|cleanup`. It uses existing internal API-key authentication. Missing, stale, revoked, cross-company and mismatched repository authority returns a stable 409 error. No legacy OAuth fallback overrides denial. A successful check is not a lease: workers recheck immediately before each external mutation. The backend serializes its writes with lifecycle changes using organization/connection row locks.

The separate `action=portable` validates the current reviewed tuple and any supplied observed source SHA, including for a disconnected connection. It does not contact GitHub or grant repository access: its receipt contains `permission_mode=none`, empty repository capabilities and the checked `expected_source_sha`. Token endpoints reject this action. Backend portable dispatch drops the reviewed repository tuple and starts a new unbound draft.

`GET /api/content-factory/org/config?article_admission=1` resolves the founder's persisted ownership of the exact startup domain. Repository admission also requires the reviewed connection tuple and fresh current-head readiness. Explicit `delivery_mode=content_only&delivery_mode_confirmed=true` admission permits a portable draft without a repository; if a reviewed tuple is supplied it must still be current. Responses echo that tuple and source SHA. `articleCapabilities.canGeneratePortableDraft` is separate from `canGenerateArticle` and `canPublishArticle`, which require current-generation durable target proof and fresh GitHub access. Historical scan flags cannot replace proof after a durable connection exists. Original repository-bound runs cannot acquire the portable callback or resume exception by changing their delivery mode.

`GET /api/content-factory/token` uses the same tuple. Default permission is read; write requires `permission_mode=write` plus an explicit mutation action. Tokens are ephemeral, scoped to the selected immutable repository ID and tracked only by expiring cache references for revocation. Durable snapshots strip credential keys. Already-issued external writes can finish during revocation; receipts and reconciliation account for this boundary.

The token endpoint's closed `permission_profile` is `repository` by default. `workflow_files` adds only `workflows:write` and requires explicit write mode plus a real setup, approved cleanup or restoration operation; read, publication and merge cannot request it. App and installation grants are read outside authority locks, then the original connection and operation are checked again. The profile and immutable repository ID isolate credential caches. `preflight=1` with this profile checks grants and fences without minting a token or creating credential references. It returns actual grant permissions with `permissions_source=installation_grant`; an issued token returns its actual narrowed permissions. Missing Workflows approval returns nonretryable HTTP 409 `github_workflow_permission_required`, with `required_permission=workflows:write`. The App owner must review Workflows read/write and the repository owner must approve the installation update; this API never changes either grant or falls back to OAuth/PAT access. Workflow-producing setup must run this preflight before model work or branch writes. A pending or missing native CI contract never certifies publication readiness.

Inventory is saved through config PUT as `repository_inventory` with an exact `source_sha`, `discovery_complete`, and evidence. Inventory-only updates preserve targets/templates and never create publishing readiness. Target verification requires `verification.status=passed|verified` and `verification.source_sha` equal to the evidence source SHA. Detection metadata alone is insufficient. The worker is responsible for build/render verification and exact source identity.

Once a target has accepted verification at the current source, worker scan/config
writes may replay that exact stored contract but cannot change its publishing
inputs or proof metadata. A copied or newer `checked_at` value is not new proof.
Conflicting scan inputs stay in `WebsiteScanSnapshot`; provider attestation uses
its separate source/operation-checked promotion path. Worker proposals also
cannot switch an already certified selected target, even to a different previously
certified target. A changed source still requires reverification; preserving a
contract never grants readiness at that new revision.

Provider CI promotion records preview capability on the target. Same-source
repair scans retain that capability and the already accepted connection-level
preview fact for older targets whose CI proof omitted `preview_capable`.
Different-source scans and native-verification refusals do not inherit it.
This guards stored acceptance and its readers; expanding the native contract
digest to seal every execution/listing input remains separate work.

Private repository provider proof uses the separate `ci_evidence` permission profile with explicit `permission_mode=read` and `action=read`, the original operation tuple and immutable repository. Only canonical CI/status verification consumers request it; model and source readers retain `repository`. This profile narrows existing App and installation grants to Contents, Pull requests, Checks and Commit statuses read access, permits only incidental Metadata read, and never includes Actions or Workflows. Fresh grant evidence is checked before cached credentials are used; missing CI read grants return nonretryable `github_ci_evidence_permission_required` without issuing credentials or changing permissions. Provider proof still requires the exact source and operation, with final consent/cancellation fencing and ephemeral credential revocation.

Provider check responses validate documented states, scalar metadata and inventory counts before inspection. Native verification may find its exact provider-sealed attestation on the first bounded page of 100 checks; that attestation does not imply that every repository check passed. Cleanup verification requires the complete inventory and rejects paginated or unfinished checks. Malformed provider evidence returns retryable unavailability, never a missing-permission diagnosis or successful verification.

Owner routes are `/api/v1/vibe-marketing/website-connection` and the equivalent `/api/v1/my-startup/vibe-marketing/website-connection` facade, with slash and no-slash forms. Actions include prepare, pause, disconnect, reconnect, reset, cleanup, purge, cancel-operation, reconcile, verify-access, verify, verify-cleanup, custom-contract, ci-attestation and github-revoke. They preserve existing authenticated company ownership checks. Bootstrap exposes `websiteConnection`, its generation, capabilities, blockers, allowed actions and latest operation.

Browser lifecycle and setup requests use `Idempotency-Key` and `X-Request-ID`; their CORS preflights allow both alongside Django's default headers. The existing exact browser origin and credential policies still apply. Native Tauri requests additionally allow `Idempotency-Key` only under `/api/v1/my-startup/`, without cookie credentials; community-chat's native header policy is unchanged. Connection and generation fences remain in the request body and are checked by the authenticated owner routes.

- Pause advances generation and cancels older repository work, retaining inventory and draft generation while denying publication/setup writes.
- Disconnect/revoke advance generation, disable publication and scheduled automation, cancel active local work, and queue remote cancellation/token revocation. Website files, published content and company data remain intact. Sole-owner company deletion separately purges snapshots, template bodies, targets and ownership evidence immediately; minimal cancellation/provider references remain only until the revocation outbox completes. A departing cofounder revokes only the binding they authorized.
- Disconnect retains worker artifacts and records their retention. Explicit purge installs a durable deletion watermark, clears backend repository-derived evidence and asks the worker to plan/apply its scoped artifact erasure. Original run IDs survive partial retries. Remote deployments and unconfirmed cleanup remain pending in category receipts; published articles and company facts remain intact.
- Reconnect verifies exact GitHub access again and requires a fresh scan/verification. OAuth callbacks and installation-created events cannot reconnect implicitly.
- Reset invalidates repository-derived projections while preserving history. It does not delete remote branches or customer files.
- Current setup summaries select scans and builds from the selected connection generation. Retained runs from earlier generations remain historical records and cannot restore a scan/setup link after reset, even if later callbacks update their timestamps.
- Cleanup first computes a read-only proposal pinned to repository HEAD and recorded hashes. Only unchanged, exclusively created integration support files with applied receipts whose commits are ancestors of the selected branch are candidates. Intent-only/unmerged receipts cannot authorize removal. Shared/modified/unknown files are retained or reported as conflicts. Published content/dependencies block automated support removal.

Cleanup execution requires a second explicit owner POST with `approve_cleanup:true`, `operation_id`, `source_sha` and `proposal_digest`. It revalidates GitHub identity and file hashes, then opens a dedicated removal PR. It never merges or modifies the default branch. A disconnected website receives no general publication grant; this endpoint authorizes only the reviewed cleanup operation. Retries reconcile the same operation-specific branch/PR.

## One-click preparation, disconnection and billing

The default-off `ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED` adds
`websiteStatus.existingArticles` and one capture evidence message from a validated
summary on the preparation's canonical setup child. The observed original SHA
is retained separately from the current branch. Missing/foreign/conflicting
evidence is omitted; zero requires a valid empty observation. This display has
no effect on goal milestones, capabilities, approval or publishing. See the
[E3 disclosure contract](articles-e3-native-inventory-display-2026-10-17.md).

`python manage.py inspect_website_prepare --company-id <uuid>` (or `--domain example.test`)
prints bounded local preparation diagnostics: connection identity and capabilities,
the last five operation receipts, the saved setup choice, current inventory resolution,
and current-generation targets. PostgreSQL enforces `SET TRANSACTION READ ONLY` as
the transaction's first statement. `--journey` additionally projects prerequisites,
the next action, and allowed actions using read-only GitHub checks. It makes no
worker requests and does not print credentials or repository content. Prepare passes
log only identifiers, step/state/reason code, attempt, and next attempt time; scheduler
summaries include up to twenty operation IDs, actions and outcomes.

Preparation uses the scan's listing route (`existing` mode), or creates `/articles`
in `none` mode only when inventory confirms no listing. A saved owner route takes
precedence. Several ambiguous routes return `needs_user` with
`user_action.id=choose_articles_page`; unchanged facts after a successful step stop
with `prepare_no_progress`. Deterministic child failures link to the failed run,
while explicitly retryable worker failures retain bounded retries. Owners may cancel
an active or waiting prepare operation and explicitly reopen it with another Prepare
click; cancelling fences its child and queues the ordinary run cancellation path.
Choosing an ambiguous page through the existing setup form saves and attaches that
direct setup child to the waiting prepare. Its saved company/domain, setup workflow,
route and original connection tuple must match; its own workflow operation remains
unchanged. A callback that wakes preparation before the handoff is saved adopts the
same saved child, and bounded waiting while dispatch is in flight prevents a second
setup request. A directly blocked child retains its actual failure prompt.

The `permissions` prerequisite sits between inventory and integration. Read-only
App JWT grant checks for `workflow_files` and `ci_evidence` cache results for sixty
seconds without minting a token. Missing grants return `needs_user` with
`user_action={id:review_github_permissions,label,url}` pointing to that installation's
personal or organization settings. Permission lookup outages remain retryable.
GitHub's authenticated `installation.new_permissions_accepted` webhook clears this
cache and wakes current waiting prepares. GitHub documents that Apps receive
[installation events by default](https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation)
and documents [new_permissions_accepted](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app#allow-time-for-users-to-accept-new-permissions)
for permission approval. An owner's Check again click also reopens the operation.

`POST .../website-connection/prepare` takes `company_id`, the reviewed connection/generation tuple and `configuration_revision`. It reserves one `prepare` operation per consent generation and persists its requesting founder, child runs, completed steps, failure counts and next attempt. The scheduled-discovery loop checks current founder membership and consent before resuming access checks, inventory, setup, exact preview proof, CI attestation and live route verification. Successful scan/configuration, CI and deployment callbacks wake the operation after commit. Concurrent requests and late callbacks cannot overwrite a newer operation lease or a disconnect fence.

Preparation, scans and article setup cost zero Roo points. Failure policy reads the child’s explicit `failure_class`, with compatibility mappings for known deterministic and transient codes. Transient failures back off for up to 24 hours and resume the same child through the existing workflow-attempt fence; exhausted repairs and strategy failures stop at `needs_attention` without dispatching another full setup. Code-only review asks for `review_setup`, manual merge asks for `merge_pr`, and unsupported work records one reason with hosted/portable channel options. Missing GitHub authorization produces one `needs_user` receipt. An owner Try again starts a fresh retry epoch and durably cancels a stalled active child; a lost-response replay reuses its epoch and dispatch key. Check again preserves the existing child and proofs for recorded authorization, permission, review, merge, page-choice and deployment actions, including when permission approval was requested by the setup step. Explicit failed/stalled work still starts a fresh attempt. Deployment waits use two, five, ten, twenty, forty, sixty and 120 minutes, then two-hour checks, for a 24-hour window. After that, one Deploy your site action remains visible while a quiet two-hour observer resumes when the marker appears. New setup previews must pass every exact-commit quality/task/resume-generation requirement before automatic approval. Adopted verification kits additionally require actual kit-commit build/browser proof. Setup auto-merge uses the [shared setup merge policy](articles-setup-merge-policy-2026-10-13.md), respects `requires_review`, and includes the exact PR head SHA. A classic protection 403/404 is unknown policy; GitHub enforces its actual restrictions at the merge endpoint. Pending checks and provider outages wait, deterministic failed checks require repair, and required review produces a merge action. Bootstrap/status GETs only observe merges.

`POST .../website-connection/disconnect` takes `{remove_setup:false}` by default. It commits the generation fence immediately, cancels local work and then resumes refunds, at most twenty ledger-owned references per pass, remote cancellation/preview shutdown and token revocation. `remove_setup:true` explicitly authorizes opening the conservative cleanup PR; it does not authorize merging it. The receipt includes `keep_pages`, `remove_setup`, completed steps, refund-pending IDs, closed PRs, deleted branches, skipped customer-modified references and `cleanup_pr_url`. Five cleanup passes without progress stop at `needs_attention`; an explicit retry resumes only missing work. Reconnect performs the existing fresh GitHub binding and starts preparation, which adopts installed pages rather than recreating them.

Read composite receipts at `GET .../website-connection/operations/{operation_id}`. Cleanup retries send the current connection tuple plus that `operation_id`, reuse the same receipt and retain its original `remove_setup` choice. Reconnect/new generation supersedes the retry. `cleanup_status:"no_unchanged_owned_files"` and `cleanup_required:false` explicitly mean that no removal PR is needed; conflicts and retained files remain visible. `GET .../company-billing?company_id=...` returns a fresh integer point balance, `setupPoints:0`, `articlePoints`, masked `signedInAccount`/`billingAccount`, `billingFounderIsYou`, `companyBillingEnabled`, `canGenerate` and `otherFounderHasPoints`. The last field is a boolean and exposes no other founder's identity or balance. A founder opts in only their own account with `POST .../company-billing` and `{company_id,opt_in:true}`; the same founder opts out with `false`. A different founder cannot replace an existing opted-in payer. Each charge/refund uses the actual recorded payer while preserving the requesting founder's audit identity.

The provisional article workflow operation, debit and `ContentFactoryDispatchOutbox` reservation share one transaction. A rejected payment rolls back the provisional operation, so the same client request can retry after a top-up. Provider-backed admission still runs outside SQL locks against the original pinned website/source/target snapshot; those bindings are checked again inside the charge transaction. Server-generated article analytics identifiers are stable per organization/request key. Existing outboxes retain their original tracking metadata and still-valid internal actor identity only after matching the original organization and requesting account; article inputs, consent, cancellation and payer checks remain strict.

Article debit and `ContentFactoryDispatchOutbox` reservation share one transaction. Provider-backed admission happens before the transaction; selected configuration, operation and generation are locked and checked again before spending. The outbox retains the same client request identity and original billing envelope across retries, refreshes editorial policy and activation before a new POST, and first checks whether the worker already owns that key. Accepted delivery rebinds the provisional local run and workflow operation. Delivery retries use one, two, five and ten minutes, with at most five POST attempts. Exhaustion reports/refunds `dispatch_never_received` only when authenticated key lookup proves absence; an ambiguous outcome enters `awaiting_resolution` and continues read-only lookup every fifteen minutes. It withholds refunds until worker ownership can be proven. Explicit worker rejection fails and refunds once; refund failure remains `refund_pending` until the stable spend settles.

A repeated article key cannot move its original spend to another billing founder or price. Such a retry returns HTTP 409 `content_factory_billing_key_conflict` and retains the original payer, requester audit and delivery outbox. Transient deferred-refund failures keep status polling readable, persist the original spend identity and retry through the scheduled sweep; settling the refund preserves newer worker receipts and cancellation state.

The reconciliation sweep probes article work stale for ten minutes, adopts terminal worker state, asks a heartbeat-orphaned worker to `/recover` once, and fails/refunds it if it remains orphaned five minutes later. The worker must explicitly acknowledge a real requeue; HTTP success alone is not recovery proof. Orphan dispatch and original restart receipts also use authenticated key lookup before settling. Failed restarts release only their provisional reused authorization and create no new charge or refund. A small `REDRIVE_AFTER` map permits one resume only after `/health` confirms the deployed worker version contains the named fix; ordinary consent checks still apply.

`GET .../website-metrics?company_id=...` returns `{websiteHealth:{windowHours:168,asOf,oneClick}}` for the selected founder company. Samples are bounded to 2,000 rows; truncation and missing action telemetry remain visible. Outcomes group paid article attempts by their saved spend/lineage, carry manual intervention counts, and report first-click setups, review completion, duration percentiles, review calls, refusals and sweep repairs. Bootstrap timing logs and `X-MLAI-Payload-Bytes` measure canonical compact DRF JSON bytes. Optional `CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID` enables a sanitized ops alert after more than three refusals in ten minutes; empty disables external delivery. Saved alert leases, a deterministic Slack message identity and three delivery attempts prevent unlimited retries.

## Proposed schema and deployment order

The implementation adds `organizations.0003_organization_billing_user` (one nullable `SET_NULL` user foreign key) and `content_factory.0045_content_dispatch_outbox` (one idempotent delivery table and due-state index). On 11 October 2026 the user explicitly approved applying those two migrations, including existing dependencies, in disposable CI/test databases. Their unchanged 363-migration graph has been applied in isolated SQLite and temporary PostgreSQL tests. This approval excludes production schema application and deployment. Database-free contract tests and migration drift inspection do not grant either approval.

Final disposable validation passed 346 SQLite tests (four PostgreSQL-only skips) and all 346 PostgreSQL tests. Twenty-two new database regressions cover real spend/outbox rollback, original-payer and reused-lineage refunds, delayed terminal credit retries, missing payment proof, lost-response adoption, same-key replay and transaction races with Disconnect and parallel delivery. The ten real loopback HTTP/PostgreSQL callback tests also passed, including cancellation during offboarding without deadlock or replacing erasure receipts. Both engines verified the unchanged 363-migration graph, outbox table, billing-user column and approved migration hashes. Test environments excluded dotenv and external network; PostgreSQL used a private Unix socket with TCP disabled, and clusters/databases were removed. Callback validation allowed only 127.0.0.1 HTTP. The 1,284 database-free test executions, model drift and CI assignment checks also passed. Full remote CI results remain separate receipts on [backend #983](https://github.com/MLAI-AUS-Inc/mlai-backend/pull/983).

After separately approved production schema application, deploy the backend receiver APIs for observations/current bindings. Then deploy Content Factory 2.1.0 or newer and confirm its authenticated runtime health version before enabling the recovery ladder, followed by the Chat one-click UI. The sweep stays passive for heartbeat-orphaned jobs until the worker confirms that minimum version; unavailable health or older workers cannot trigger recovery exhaustion/refunds. The new backend requires the outbox table and billing column; do not release its code ahead of approved schema. Run fixture-only public API acceptance with a dedicated test wallet before any separately approved real-site canary or production repair. No canary, production migration, Slack message or deployment is implied by local implementation validation.

## Operations and rollout

The existing scheduled-discovery process runs `process_website_connection_operations`. Completed runs can still own previews, so preview shutdown uses a separate `/api/runs/{id}/preview/stop` worker call and retains terminal article history. Only explicit cleanup confirmation completes that part of the receipt; HTTP 409, pending builder cancellation, and unconfirmed hosted deployment removal remain pending. `python manage.py reconcile_website_connections --limit 20` is the equivalent explicit operational command. Both make external cancellation/revocation calls and should only be run in an authorized environment.

Direct article PR merges require an applied ownership receipt for the same publishing run, connection generation, branch and exact live PR head, then send that SHA as GitHub's compare-and-swap condition. Setup merges require the exact verified preview commit. External PR pushes cannot inherit an earlier approval just because checks pass.

Verified GitHub installation deletion/suspension, repository removal, transfer, rename, archive and deletion revoke selected connections. Default-branch pushes retain consent generation, advance configuration version and record observed source identity. Authenticated owned merges retain existing capabilities while current-source verification runs; other pushes revoke publication readiness and require a rescan. Source-bearing config writes and publication authorization reject stale `expected_source_sha`; readiness promotion also verifies the current GitHub branch SHA. Templates are retained. Current-head re-verification never attests a stale verified SHA. Setup and publication receipts retain a protected `merge_sha`; guarded PR observations retain the same merge identity. A current-head preview proof may clear the source-change blocker for setup while remaining insufficient for publication. Source-reverify work is marked managed by prepare only after a prepare actually reopens, with a fresh dispatch epoch. While that re-check is unsettled (a preparation reopened for the source, or a pending `source-reverify` check that is under 24 hours old and has not failed without retry), the owner's journey (`journey_for_context(..., owner_view=True)`, used by the bootstrap and website-connection responses) reports the inventory, verification and deployment facts and the scan/preview/activation/articles steps it renews as `status="running"` with "Checking your latest website changes." (or "Checking your website again." for an unchanged source) and no `nextAction`. Reason codes and capabilities are unchanged. Writes paused, an open setup pull request or a missing integration stay actionable, and preparation itself reads the unmasked journey to choose its step. Duplicate push receipts are idempotent. The existing installation reconciliation sweep also revokes after confirmed installation loss; inconclusive API failures do not grant or revive access.

`python manage.py repair_website_connections --domain example.test` is read-only and prints identity/digest findings without template bodies. `--apply` is a separate operational repair: it creates disconnected legacy records and archives invalid template envelopes. It never guesses extracted template content, enables publication or edits GitHub. Use it only after reviewing the dry run and receiving environment-specific approval.

Deploy backend schema/API, worker tuple propagation/guards, and frontend capability handling as one coordinated release. Older repository jobs without tuples intentionally fail closed and need a newly authorized scan. Roll back by disabling repository work; do not bypass generation enforcement to resume legacy jobs.

### Canary control and business-outcome monitoring

Set `WEBSITE_CONNECTION_WRITE_MODE=disabled` during cutover, then `canary` with `WEBSITE_CONNECTION_CANARY_DOMAINS=talathrive.com` for the scoped acceptance test. `enabled` permits ordinary connection checks; it is not an authorization grant. Invalid modes and an empty canary list deny new setup, publication, merge and preview-deployment writes. Reviewed inverse cleanup remains available under a separate durable operation scope; it grants no ordinary publication authority. Read/inventory/export and disconnect remain available. Canonical client summaries display the same restriction. Previously accepted provider requests still require reconciliation.

The deployment workflow passes the repository variables `WEBSITE_CONNECTION_WRITE_MODE` (default `disabled`) and `WEBSITE_CONNECTION_CANARY_DOMAINS` (default empty) through validated SSH stdin into the host's `/root/mlai-backend/.env`. Compose supplies them to recreated application processes. Exact lowercase domain lists reject URLs, wildcards, IP addresses and partial matches. Keep writes disabled while backend schema/API and Content Factory are rolled out, then set the reviewed canary variables and rerun deployment. Returning the mode variable to `disabled` and rerunning deployment is the immediate repository-work rollback. Previously accepted provider requests still need reconciliation.

`APPROVED_MIGRATION_PLAN_SHA256` is an optional repository variable containing the lowercase SHA-256 of the reviewed candidate's complete `Planned operations:` block from `python manage.py migrate --plan --noinput`, with trailing newlines removed exactly as shell command substitution does. The validator excludes environment-specific import diagnostics and rejects missing or duplicate headers; every operation line remains in the digest. Deployment installs the approval before checking the plan; only the exact pending plan passes. Inspect the actual candidate plan: current main merges the historical website and credential branches through the no-op `0043_merge_credentials_website`. The disposable lifecycle replay retains credential leaf 0040 and verifies only 0042 plus no-op 0043 as the forward replay plan. Production pending nodes depend on its existing migration state; the local test approval grants no production migration authority. Clear the variable after successful schema/API verification; the next deployment clears the host's old approval too. Keep migration 0042 applied during an application rollback: reversing it deletes lifecycle and audit history.

`python manage.py report_website_connections --domain talathrive.com --hours 24` is a read-only, sanitized JSON report. `--check` exits nonzero when business-level scans failed/blocked, reconciliation has remained pending for more than 15 minutes, or the bounded 2,000-run sample was truncated. It reports connection states, adoption backlog, inventory, quarantined templates, operation age, scan outcomes and mean lifecycle elapsed time. That elapsed time includes queue/processing and reflects durable row timestamps; it is not a CPU/build-duration measure. A read-only shadow comparison counts legacy scaffolded state and canonical current verification without granting authority. The existing admin usage payload includes this report as `websiteConnections`; an operator can wire the command's exit code into existing monitoring. It does not create an external notification subscription.

## Local verification

With explicit approval for migration 0042 and its existing dependency graph, the harness creates fresh synthetic databases, ignores `.env`, denies external network, and removes its database when finished:

```sh
python scripts/test_website_connections_database.py --engine sqlite --replay content_factory.tests_website_connections content_factory.tests_website_articles_reliability_database
python scripts/test_website_connections_database.py --engine postgres --replay content_factory.tests_website_connections content_factory.tests_website_articles_reliability_database
```

PostgreSQL mode creates a temporary socket-only local cluster using `initdb`/`pg_ctl`. It exercises actual row-lock concurrency; SQLite skips that one test. The harness also checks Django system configuration and migration drift.

GitHub repository-ID token scoping follows [the installation token API](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app); deleting and recreating a repository under the same name cannot inherit an old connection grant.

Repository callback follow-ups use durable `worker_followup` operations and are dispatched by the existing website reconciler after callback transactions commit. The original connection generation is retained, and disconnect cancels pending follow-ups. Worker HTTP never runs inside the owner/config authority transaction; local response projection takes a fresh fence. An accepted response that races disconnect is retained as cancelled history with remote cleanup queued.

The automatic native mutation adapter currently accepts only the repository root on GitHub's current default branch; an owner-reviewed custom target can certify an alternate application root with its exact committed build/browser proof. Every setup, publication, merge and preview authority check verifies immutable repository identity and the current default branch with GitHub; identical SHAs on different branches do not bypass the check. Other selections remain available for inventory, with explicit adapter-verification blockers.

Client compatibility: old native builds without the original connection tuple fail closed and must update MLAI before reconnecting. The browser is the initial canary client. Coordinate the compatible TestFlight and desktop releases after backend and worker contract verification; source parity alone does not establish that users have received a compatible binary.

Portable `content_only` generation deliberately dispatches without repository identity or consent. Its durable original mode permits editorial run snapshots, content/progress/failure callbacks and editorial controls (resume, cancel, deny, revise and image generation). Both control dispatch and status polling use the original unbound intent and reject payloads claiming website authority. Sender-supplied mode, a publication result, a hosted-preview claim, or an organisation-config write cannot use this exception. A portable draft must start a new explicitly authorised repository workflow to publish. Rejected controls preserve the saved run and return their actual error instead of being labelled queued. Native clients without reviewed connection fields fail closed and need a compatible update; browser canary verification does not imply a mobile binary has been distributed.

Worker mirrors may include `run_request.editorial_admission.audience.status=approved` and the corresponding offer status. These are catalog observations, not publication approval. The portable guard accepts these exact paths only after the existing editorial snapshot contract validates the admission schema, selection hash, brief, tenant and immutable saved admission. All other approval and repository-authority checks still apply, including inside the admission itself.

Status mirrors omit the backend's `roo_points_*` request fields. Run reconciliation retains this backend-owned billing history, rejects attempts to change recorded values, and drops newly claimed billing fields. Worker observations cannot create authorisation or erase a charge reference. The existing ledger and job billing paths remain authoritative.

Observation-only mirrors also retain the original delivery mode and publication
resolution. A cancellation receipt requires its operation proof even while a
disconnect transaction is still committing; a missing operation or attempted
configuration write cannot fall back to an accepted progress observation.
Terminal paid failures persist refund retry receipts before accessing the payer
or credit service. The scheduled sweep resolves only verified original-company
payment lineage, credits the original payer once, and merges the fresh result
without changing newer cancellation or cleanup history. Missing payment proof
keeps the known paid obligation pending.

An article blocked on a linked automatic repository scan or setup repair retains
an active workflow operation while that repair is queued, running or awaiting
review/merge. The article itself remains blocked. Its children keep the original
connection, attempt and removal fences; their checkpoints do not replace the
parent's operation receipt. A failed or unavailable repair makes the operation
terminal. This projection cannot reopen an already terminal operation, and
cancelling pending repair work fences both the parent and its known children.

Setup approval that reports `setup_pr_create_failed` remains blocked with its
error and approved preview evidence retained. The browser receives a conflict
response instead of a successful queued acknowledgement. A status poll also
normalizes that failure over stale nested `preview_ready` metadata; neither path
creates a PR or reopens the website operation.

Older approved runs whose recorded PR failure was masked by preview polling
receive the same canonical failure in their response projection. This read does
not rewrite their history, and a newer control receipt or an existing PR keeps
its current state. Recovery still starts through the reviewed setup boundary.

A current-generation setup child blocked (or marked failed by an older generic
failure callback) with `SETUP_EXAMPLE_CLEANUP_REQUIRED`
and a validated `remove_setup_example` file/record action leaves preparation in
`needs_user`. The public receipt names the exact relative file and article
record, with a repository-derived GitHub file link. The blocked child is
terminal: callbacks, reads and scheduler wakeups do not retry it. After the
owner removes the named record, an explicit prepare `retry=true` clears only
that preparation's old scan/setup child references, increments its attempt,
probes the current repository HEAD and starts fresh work. Old preview approvals
and the pending setup's old source-scan hint are not reused. Other owner actions
(permissions, route selection, review and merge) retain their existing retry
behavior. Missing/malformed actions or stale operation, tenant, repository,
generation, attempt or deletion-watermark proof keep the generic blocked path.
CF's wire status `needs_user` normalizes to durable `blocked` through the shared
workflow status mapper for HTTP polling and reconciliation. Its root and nested
cleanup action remain available to preparation. A same-payload callback replay
uses that same terminal-state alias while retaining every original operation
fence, so it can acknowledge an already processed manual cleanup event. An
explicit callback `resume_available=false` clears an older true Resume flag.

Status polling retains the worker's authoritative execution generation and
state version, so refreshing an approved article cannot erase its exact-preview
approval fence. Stale snapshots remain rejected and a new generation needs a
new approval.

Pull reconciliation preserves the domain, repository and actor recorded by the
original dispatch when a worker status response omits those model fields. It
can restore a previously cleared repository column from that saved request
after the original authority guard passes. Conflicting incoming identities and
stale snapshots cannot replace the saved identity.

Recovery through a dispatch key acknowledges the existing run identity only.
Its minimal receipt cannot replace saved worker status, repair lineage, evidence
or errors. If callbacks have not yet created the local run, recovery creates a
queued projection until a full authenticated worker snapshot arrives. Billing
still binds to the original dispatch; existing terminal runs remain terminal.

All reconciliation actions lease an operation in a short transaction and release it before remote HTTP. A conditional update checks the original claim timestamp afterward. Concurrent offboarding therefore cannot deadlock with the worker's cancellation callback or have an erasure/retention receipt replaced by stale transport results.


## Version 2 journey and resumable operations

`websiteJourney.version=2` is the authoritative client projection. `prerequisites` independently reports account health, repository reads, inventory, integration, build/render proof and live deployment. `policy` explains a write pause without erasing completed proof facts. `capabilities.canOpenPublicationPr` is distinct from `canPublishArticle`; live publication needs an internally retained provider/route receipt. The checked-in [synthetic fixture](../content_factory/fixtures/website_journey_v2.json) is generated by the actual pure projector and checked for parity in tests. `sourceRuns` lists only current-generation original owned scan/setup runs with durable operation fences, never a cancellation receipt or custom-verification child. `ciSourceRuns` additionally exposes current custom verifier children so their sealed CI identity stays attached to the correct run. Both include operation ID/attempt, deletion epoch and repository identity. CI child selection additionally verifies the persisted operation is `native_verification` with the matching run, attempt and deletion watermark; ordinary article/revision children cannot appear. Inventory completed at an earlier source SHA becomes `stale` after source advancement or a newer provider-observed HEAD. Installed integration remains factual when build proof expires, but build verification becomes `stale` after seven days or a source/branch change and offers scan/CI refresh rather than live verification. Write pauses preserve complete build/deployment facts while denying publication.

GitHub account liveness probes do not require a selected repository or optional `/user` endpoint. They have bounded cache TTLs and a short probe lease. Repository inventory uses read tokens; setup and publication separately require write permission. Completed readiness cannot bypass that distinction. Ephemeral probe and verification tokens are revoked after use.

The journey's GitHub permissions step is action-scoped. Workflows write is
required only while the current-source integration needs verification-file
installation or updates. Checks and Commit statuses read are required only
while authenticated in-repository CI proof is missing or stale. Fresh selected
target proof reuses its backend-validated `ci-verify` receipt only when the
current source, target/contract, original operation/attempt, consent generation
and deletion watermark match. Live verification then checks the repository HEAD
and public listing/detail/404 artifact evidence without reading CI again.
Fresh preview-only build metadata cannot substitute for that authenticated
receipt; missing, mismatched or expired CI receipts expose the permission
requirement before dispatch and still require reattestation. Once integration and
build proof are current and the deployment is verified, publication does not
require either optional profile. Permission receipts from earlier setup work
cannot downgrade that publication readiness. Scans, article generation,
previews and disconnect do not require these grants.

Bootstrap `articleSetupState.setupMerged` and its snake-case alias retain the
server-observed merge fact when source verification expires or access changes.
They do not grant generation or publication permission: `generationReady`,
`scaffoldConnected`, `published` and the journey capabilities are recomputed
from current authority and proof on every response. A missing merge fact stays
false even when generation is allowed. The fresh reviewed setup action uses the
merge fact together with current write policy, inventory and operation fences.

Native article preview commits request a fresh repository token with `permission_mode=write`, `action=preview`, the original `run_id` and an immutable `expected_source_sha`. Preview authorization rechecks current repository identity, source, consent and operation fences; it does not require or establish a live publication receipt. The worker limits this temporary scope to Git objects and that run's owned review branch, then restores the original read scope. Preview tokens cannot request the workflow-files or CI-evidence profiles, and publication/merge still require their independent verified target and live deployment receipt.

Mutation ledger `status=proposed|applied` records a Git effect and never transitions
the originating workflow operation. A preview retry can reconcile the same owned
patch after a failed build without reviving that workflow. Current consent, source,
operation attempt and revocation fences still apply; workflow callbacks continue
to validate their own terminal status and event allow-list.
An owner-forced preview retry advances a failed or blocked original operation
attempt before dispatch. The worker authenticates the new attempt against the
backend's retained original request; it preserves source, consent and charge
identity. Completed, cancelled or removed workflows cannot use this to resume.

`websiteJourney.repository.framework` reports the selected contract or discovery
framework only when its evidence belongs to the current source. Unknown values
remain null. `proofContract={version:1,requirements:[...]}` supplies the handoff
acceptance requirements: `pinned_source_sha`, `safe_publish_target`, `build_proof`,
`browser_proof`, `workflows_permission`, `ci_artifact_digest`, and
`live_verification`. Inventory alone never satisfies this contract.

Website capabilities expose `templatesValid` separately from accepted native
generation and publication proof. Native article generation needs current target
build/browser proof and read authority; publication additionally needs the exact
live marker receipt. `generationReady` remains a compatibility projection of
native readiness. The legacy `articles_scaffolded` database flag grants no
readiness and is not set by completion/scan callbacks. Slack's compatibility
fields are derived from accepted verification evidence.

Every new workflow reserves a durable operation before dispatch. Worker authority includes `operation_id`, `operation_attempt`, `deletion_epoch` and the connection tuple. A repeated active logical scan/setup restores the persisted dispatch identity even after a client reload; the same request key with different inputs returns `operation_key_conflict`. Cancellation fences exactly the reviewed attempt before transport and retains known or uncertain remote effects. It never claims an accepted Git mutation was undone. Purge retains its tombstone across reconnect/reset/readiness updates so late callbacks cannot restore deleted evidence.

Resume reserves a new operation attempt before transport. Repeating an ambiguous
resume retains that attempt; accepted worker observation closes the reservation
so a later failed generation can reserve another. Terminal operations accept
only matching terminal observations and bounded receipt/read operations. Website
lifecycle changes cancel every nonterminal operation, including applying,
verifying, source reverification and review/merge/deployment waits. A denied
terminal callback for a still-current attempt records `result.failure` with the
original typed code or authority denial and disables Resume; it cannot change
configuration, restore a completed run, or revive an obsolete generation.

Default-branch pushes matching a recorded owned merge retain certified readiness
and queue durable `source-reverify` work. Before the merge request, the backend
saves the approved PR number, repository, connection generation, base branch and
exact owned head commit in protected `publish_merge_intent`. A webhook arriving
before the merge response is saved may use an authenticated read of that same
merged PR; its merge SHA, repository IDs, branches and head must match. Reads
occur outside authority locks and the original connection is fenced again before
persistence. The reconciler checks for a late merge receipt before dispatching
an inventory scan. The owner may instead merge an approved setup's own pull
request on GitHub. That push is owned only when GitHub reports the pull request
merged into the selected branch as the pushed commit, from the selected
repository at the exact head the setup previewed, and the pushed commit has that
head's tree. Under the same connection lock the setup then records it as its own
merge (`merge_sha`, `merged_at`, `merge_response.source=github_owner_merge`),
unless the run changed or already records a merge commit. Setup settlement and
CI lineage accept that merge as they accept MLAI's Publish; without a
`publish_merge_intent` it is never a goal `backend_merge`. Other pushes report
`repository_source_changed` and additionally queue a fresh inventory scan. The
reconciler authenticates current-source provider CI and observes listing/detail/
unknown-slug deployment routes before enabling the new source. A failed or
missing proof remains pending with its typed reason; inventory timestamps cannot
refresh accepted verification time.

Provider verification is two-phase: snapshot scope, release database locks, read GitHub or the worker, then lock and compare the original scope before persistence. Cleanup applies only a reviewed inverse or unchanged exclusively owned historical files. Original source bytes remain in the worker until restoration is reviewed. Shared/customer-edited files and retained published-content dependencies prevent unsafe restoration. Opening a removal PR yields `awaiting_merge`; observed merge yields `awaiting_deployment`. `verify-cleanup` completes only when the current selected branch equals the recorded merge SHA, GitHub Actions checks succeeded, and reviewed `verification_routes` prove exclusive routes return 404 or restored routes carry the original artifact digest. Historical integrations without such provenance remain pending with an explicit reason.

## Owner custom integration and CI path

Owner POST `website-connection/custom-contract` requires a current original `run_id`, connection tuple, `configuration_revision`, idempotency key and reviewed version 1 contract. Its executable fields are `runtime_family`, `runtime_version`, `app_root`, `package_manager`, `lockfiles`, argument-array `install_command`, `build_command`, `preview_command`, `content_path_pattern` containing `{slug}`, `route_template`, `listing_route`, `environment_names` and `dependency_policy`, plus optional SHA256 `artifact_digest` (default null). Only environment names are accepted; secret values are rejected. `phase=configure` stores the reviewed worker contract. `phase=verify` adds `verification={source_sha,branch,target_id,seed_slug,expected_h1,file_overrides}` and reserves an immutable child operation. Verification runs isolated baseline/patched builds and listing/detail/unknown-slug/canonical/assets/browser/narrow/wide checks without model or credit work. Its typed queued/running outcome is polled by reconciliation; exact proof promotes only that reviewed source/contract. Missing environment prerequisites return `requires_ci`, never a ready flag.

Generated certified routes include `.mlai/articles-verification.py`, reviewed `.mlai/articles-verification.json` and `.github/workflows/mlai-articles-verification.yml`. Review and merge these artifacts with the integration. The workflow runs the real build/browser verifier and emits the GitHub-owned check `MLAI articles native adapter verification`, with `MLAI_ARTICLES_ATTESTATION:<base64 JSON>` in check output. The checks-write token is confined to the sealing step. Rerun through `workflow_dispatch` after deployment/source changes; no backend operator key is required for the owner path.

Owner POST `website-connection/ci-attestation` supplies the current original `run_id`, connection tuple and exact `evidence` from that check. Backend reads the provider check at `source_sha` and compares repository ID, consent generation, original operation/attempt, deletion watermark, target and persisted contract digest. Full evidence includes baseline/patched build statuses, all browser proofs, adapter/version, source-tree SHA, environment fingerprint, lockfile digests (empty is valid for static projects) and a canonical evidence digest. Supplied ready flags grant nothing. A completed successful operation may attest CI in the same active generation; cancelled/deleted work cannot. The worker receives only a previously authenticated receipt. A reviewed merge may advance original scanned source `a` to CI-tested source `b` without changing the original run or consent generation. Automatic attestation requires applied owned mutation lineage and provider ancestry; the owner path explicitly reviews current `b` and its GitHub-sealed proof. `ciSourceRuns` selects the child identity when required environment names move verification into customer CI.

Owner `verify` derives the current retained original workflow CI identity, ignoring an unrelated latest lifecycle receipt ID supplied by a generic client, and reads the exact selected source and same-origin listing, the exact sealed article detail path, and the sealed unknown-slug path. Listing/detail must return 200 with the same CI-tested `artifact_digest`; unknown slug must return 404. Article generation may use current native build proof, but article publication and merge authority additionally require a fresh live receipt for the exact source, target, contract and marker. Approving a persisted scan/setup workflow uses setup authority, and linking an already verified target uses configuration authority; these onboarding steps can precede live deployment. Article workflows cannot acquire that exception from sender-supplied workflow labels. Configuration-only snapshots cannot replace the complete repository inventory in the journey. All paths must match the reviewed target route mapping. The route must carry the reviewed `<meta name="mlai-artifact-digest" content="…">` or equivalent header. HTTPS observation pins a validated global address while retaining original TLS SNI/Host, rejects redirects/private destinations/proxies and reads a bounded body. Missing CI, marker or deploy evidence produces an actionable denial rather than silently refreshing account access.

After current custom build/CI proof, `phase=configure` may add `generation={reviewed:true,article_template,design_guide,resource_prompt?,live_marker}`. Required template bodies are nonempty strings of at most 500,000 characters. `live_marker={kind:"artifact_digest",schema_version:1,meta_name:"mlai-artifact-digest",value:<64 lowercase hex>}` must equal the marker tested in the retained build/CI proof. This local path uses the exact current certified target even when CI advanced the scan baseline, preserves the original run binding, and stores template changes plus retry receipt atomically. A later source promotion preserves configured generation only if the same contract and new exact proof test the same reviewed marker; otherwise it clears generation readiness and requests reconfiguration. Custom contracts certify execution and rendering, not content authoring. The current worker has no `custom_contract_v1` publishing adapter, so even a certified custom target with reviewed templates and a live marker cannot enable repository article generation or publication. Owner projections and worker authority return `publishing_adapter_required`; portable drafts remain available. An implemented native or hook delivery adapter is required before repository writing.

## Portable saved article export

Owner GET `runs/{run_id}/article-review` returns `articleExport={version:1,status:"ready"|"unavailable",runId,revision,format:"markdown",markdown,metadata,media,reasonCode?}`. Export comes from the current canonical saved article snapshot, including protected direct edits; internal run results/prompts are never a fallback. The facade requires the owned run, exact saved revision and no pending preview or newer ready revision. Unknown or missing versions fail closed. Metadata is limited to `title`, `slug`, `description`, and credential-free HTTPS `canonical_url`; media contains only credential-free HTTPS `url`, `alt`, `caption`. The read response is private and not cached. Export is read-only and does not grant repository publication or require a connected website.

POST on that same owner endpoint accepts atomic text saves:

```json
{"action":"editTextBatch","expectedRevision":"<saved revision>","operationId":"<stable operation>","edits":[{"fieldId":"<binding>","value":"Exact replacement copy"}]}
```

The facade forwards field IDs, revision and operation identity without changing
them. Worker denials retain `code`, `error_code`, `reasonCode`, `retryable`,
`next_action`, `failure`, `errors` and `revision` when supplied.

GET also returns `imageRegenerationCostPoints`: an integer including zero, or
null when paid image pricing is unavailable. Paid pricing uses
`CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS`; no default paid amount is
invented. The existing MLAI free-domain policy yields zero. Clients must display
and submit that exact quote before POST
`{action:"regenerateImage",fieldId,instruction,expectedRevision,operationId,expectedCostPoints}`.
Quote drift or absence returns typed 409 `roo_points_quote_changed`; unavailable
pricing returns `image_regeneration_quote_unavailable`. One exact company/run/
operation consumes one existing Roo ledger charge, bound to its original payer.
Another member replaying that operation gets `image_operation_owner_conflict`
before spending. Changed inputs under the same identity return
`image_operation_conflict`. Definite rejection refunds the
original debit; ambiguous transport acceptance retains a pending debit. Owner
GET reconciles the exact asynchronous candidate: a failed candidate refunds its
original payer exactly once, while queued/running candidates remain pending.
A refunded receipt cannot be changed back to accepted and cannot be reused.

Original confirmed unbound content-only drafts, their comment revisions,
reconciliation and signed private-preview leases need no repository credentials.
They retain review/export links on failure and offer no setup or publication
action. Publishing children require the current verified repository, generation,
source SHA and the exact saved slug/route contract. Older repository-bound drafts
remain reviewable but return `website_repository_changed`,
`website_source_changed` or `capture_target_mismatch` before a publishing child
is bound. A refunded original draft payment also denies regeneration/restart;
no-delivery internal, budget, editorial or source-drift failures queue settlement
of the recorded charge after the worker has proved it stopped without an article
effect or a delivered review/package.

`github-revoke` has an account-wide reviewed flow. `phase=plan` returns `githubRevocation.planDigest`, affected owned companies and provider instructions. `phase=apply` requires that exact `plan_digest`, `approved=true` and an idempotency key. It fences all reviewed owned bindings, cancels their work/schedules and clears only this user's saved installation/OAuth credentials. Other users sharing an installation ID are preserved. Local completion does not claim provider uninstall; the receipt retains `provider_cleanup_pending` and GitHub installation/OAuth settings links. Website disconnect remains company-scoped and preserves global account access.

## Recovery evidence and release gates

Use owner `reconcile` to retry current scoped outbox work and poll custom verification. GET `website-connection/operations/{id}` returns only the selected company's receipt. The read-only `report_website_connections --check` identifies overdue operations and failed scans; cancelled/denied/deleted rows are terminal, and awaiting merge/deploy remains visible. `repair_website_connections` is read-only unless explicitly approved with `--apply`. No repair command silently revives consent.

Release gates are: matching backend/worker/client contract tests; disposable PostgreSQL concurrency and exact historical migration replay; reviewed automatic CI artifacts and route marker; source/build/browser checks; current public-route deployment receipt; then a separately approved production rollout/canary. Local migration approval, source tests and an opened cleanup PR do not satisfy production migration/deployment or verified removal. The specifically approved `0044_website_target_generation_key` and its model/upserts have been created; its disposable tests were authorized on 11 October 2026 as part of the `0045` dependency graph, while production application remains a separate approval gate. See the [generation-key proposal](website-target-generation-migration-proposal-2026-10-07.md) for exact scope and rollback considerations.

Status-only worker PUT cancellation receipts require an existing run and its exact
saved domain, including portable runs and prior connection generations. They record
cancellation metadata without changing saved repository/request identity, targets,
configuration, proof, credentials or billing. Unknown runs return 404 and scope
mismatches return 409. Terminal setup revisions project their recorded failure over
stale preview-building labels in browser and worker responses; the read projection
does not rewrite historical run evidence.


### Article cancellation and original-charge settlement

An article's setup PR does not constitute article publication. The worker emits
`publication_evidence_v1` with `purpose=setup|article`, run/goal identity and the
original present-only scope: connection/generation/target, repository, source SHA,
operation/attempt/deletion epoch, client request ID and optional branch/app root.
A setup receipt explains only its matching PR/branch. Unexplained legacy links,
pending publication or an article effect retain their evidence and require
reconciliation. Later sparse snapshots cannot erase that publication evidence or
change the original scope, billing key, goal or initial delivery consent. Explicit
backend resume changes the execution attempt; it preserves omitted scope fields.

The worker's `article_cancellation_v1` binds that scope to a nonnegative execution
generation, monotonically checked state version, cancellation watermark,
`outcome=no_article_effect|article_effect|unknown` and nullable `delivered_value`.
Its `receipt_id` is SHA-256 over canonical JSON of every other field. Status-only
cancellation keeps existing artifact/review/proof and payment records. An article
with possible external effects can stop locally and remain pending reconciliation;
HTTP success, elapsed time and a legacy `refundable` flag are never no-value proof.

Credit requires a validated `no_article_effect`/`delivered_value=false` stop and
no contradictory saved publication or delivered value. A completed package counts
as portable delivery only for an originally confirmed `content_only` request;
the internal package created before repository rendering is not that consent.
Verified review previews retain their charge. A terminal no-value candidate with
no stop proof records `stop_required`; reconciliation obtains the worker receipt
outside database locks, with at most three attempts before `needs_reconciliation`.
Explicitly exhausted blocked runs use the same path. So do paid runs denied by a
no-value authority refusal such as `website_source_changed` (a merge moved the
verified source mid-run) with no recorded effect or delivery: reconciliation
reserves their obligation even when the worker's terminal callback was superseded.
Content Factory currently keeps a denied run denied and returns no stop receipt,
so these reach `needs_reconciliation` until the worker can seal one.
Owner review/merge/deploy waits and children whose active first-article goal owns
the spend are excluded.

`article_terminal_refund` and `reconciliation_refund_pending` are backend-owned
outbox fields. Settlement locks the organization and fresh run, rechecks proof and
holds those locks through database-only credit against the original spend, payer,
amount and idempotency key. Missing ledger identity, transport uncertainty and
credit errors retain the obligation. Duplicate cancellation cannot erase a pending
or completed credit; contradictory later evidence quarantines an uncredited
obligation. A pre-existing child refund obligation is marked `deferred_to_goal`
and retained for its original first-article goal owner; the child cannot refund
the shared spend after another repair child delivers. Confirmed dispatch rejection retains its separate existing refund path.
Disconnect preserves queued goal-refund operations. An ordinary source wake,
including the first article's own merge push, cannot create another paid successor
after a terminal first-article goal, even when the goal-runner flag is rolled back.

The paired worker fixtures cover setup-only cancellation, a trusted resumed
attempt and a publisher exception with pending effects. Local tests deny database
and network access; disposable database tests exercise original-ledger settlement.
These source checks do not claim deployment or resolution of historical balances.

First-article goals settle that shared payment through their existing `goal-refund`
outbox. A terminal parent, failed child or missing child row does not authorize a
credit. Settlement locks organization, connection, parent, dispatch and child
records, and requires each original-payment-owned article child to have a valid
stop receipt proving no article effect and no delivered value. A verified draft
retains its payment; pending publication and uncertain dispatch remain unresolved.
Children and rejected-dispatch handlers defer to this parent even after it stops.

Admission writes the generation dispatch in the same transaction that validates
the active parent. After provider admission, it rechecks the stored integration
proof under the organization and connection locks before spending or consuming a
reservation. Admission captures the selected target and exact verified source
before reserving the workflow; supplied identities cannot be replaced. The locked
check rejects a changed selection or source even if the new one has valid proof.
A pending sealed proof therefore blocks new dispatch even when it
appears after the provider read; the original goal hold remains unchanged. Exact
hosted proof can resume that hold. Verification readiness is not refund evidence:
settlement still requires each child's independent stop and delivered-value proof.
Legacy reserved workflow rows whose original digest lacks target/source binding
are not upgraded to today's selection. Replaying the same client identity fails
closed without another spend or dispatch. Before rollout, reconcile these rows
through a separately reviewed recovery using their actual saved request, outbox
and payment evidence; missing history is not permission to invent a binding.
Its absent row can prove pre-dispatch cancellation only while
that parent lock excludes new admission and no prior child/dispatch evidence
exists. A dispatch with zero attempts can already have a synchronous POST in
flight; it remains unknown. Stops run outside the billing transaction, at most
three times, before the outbox requires reconciliation. No timeout credits money.

Erasure retains a bounded `first_article_settlement_v1` financial record: original
payment binding, child identifiers, worker stop receipt digests and versions, and
the unresolved or proven outcome. It contains no article body, provider response
or credential. An unexplained missing child, corrupted summary or unknown effect
keeps the obligation. Positive recorded stop proof survives removal of content;
the same original ledger idempotency key still ensures one credit.

A fresh, scoped no-effect child stop receipt or confirmed dispatch resolution wakes
an existing quarantined parent refund after its source transaction commits. This
preserves lock order and the existing cancellation-attempt count; duplicate proof
cannot restart the budget. The awakened worker repeats the complete original-ledger
and all-child evidence checks before crediting. Unknown effects remain retained.

Cancellation follow-ups also have their own persisted three-attempt, 24-hour
budget, including operations with no preview IDs. A typed article cancellation
receipt retires repeated stop requests while preserving uncertain publication,
refund and cleanup obligations. Generic HTTP success, 202 or 404 is not article
absence evidence. A known non-article acknowledgement must match its original
job or fenced operation/attempt. Unknown or exhausted cancellation is retained
for reconciliation; preview teardown and token revocation continue independently.
No cancellation deadline authorizes a credit or remote resource deletion.

New cancellation intents freeze a digest of each run's original organization,
repository, domain, workflow, goal and exact execution scope before remote work.
A resumed attempt, changed deletion epoch or newly added run ID cannot reuse the
old cancellation. Historical follow-ups without that binding retain an unknown
obligation and retire repeated requests; the consumer never guesses their scope.

Automatic article stops from lifecycle cleanup, heartbeat recovery, exhausted-run
settlement and first-article parent settlement send the same captured
`expected_cancellation_v1` envelope. Content Factory must validate it inside its
run/lifecycle lock before stopping work. A stale caller cannot cancel an attempt
that resumed after the backend read. A scoped 409 retains the original obligation;
it does not borrow the new attempt's identity. Deploy the companion scoped-cancel
endpoint before these senders, since older endpoints ignore the JSON envelope.
Explicit owner cancellation remains compatible with the legacy empty-body route.

Draft group deletion retains its own PR close-or-observe cleanup. A closed or
inaccessible PR, preview deletion, or refused worker cancellation does not prove
that article work stopped or delivered no value. The existing worker receipt
guard still protects run state and original payment evidence; typed unknown
stops retain reconciliation obligations after PR cleanup. Setup-only typed stops
can finish cancellation without closing the website's setup PR.
