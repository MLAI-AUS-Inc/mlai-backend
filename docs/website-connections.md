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

Inventory is saved through config PUT as `repository_inventory` with an exact `source_sha`, `discovery_complete`, and evidence. Inventory-only updates preserve targets/templates and never create publishing readiness. Target verification requires `verification.status=passed|verified` and `verification.source_sha` equal to the evidence source SHA. Detection metadata alone is insufficient. The worker is responsible for build/render verification and exact source identity.

Owner routes are `/api/v1/vibe-marketing/website-connection` and `/{pause|disconnect|reconnect|reset|cleanup}`. They preserve existing authenticated company ownership checks. Bootstrap exposes `websiteConnection`, its generation, capabilities, blockers, allowed actions and latest operation.

- Pause advances generation and cancels older repository work, retaining inventory and draft generation while denying publication/setup writes.
- Disconnect/revoke advance generation, disable publication and scheduled automation, cancel active local work, and queue remote cancellation/token revocation. Website files, published content and company data remain intact. Sole-owner company deletion separately purges snapshots, template bodies, targets and ownership evidence immediately; minimal cancellation/provider references remain only until the revocation outbox completes. A departing cofounder revokes only the binding they authorized.
- Disconnect and company deletion do not erase Content Factory's durable run artifacts, checkpoints, generated media or repository copies. Receipts explicitly record those retained categories. Preview shutdown removes supported deployments only when the provider confirms it; there is no general worker-artifact erasure job in this release.
- Reconnect verifies exact GitHub access again and requires a fresh scan/verification. OAuth callbacks and installation-created events cannot reconnect implicitly.
- Reset invalidates repository-derived projections while preserving history. It does not delete remote branches or customer files.
- Cleanup first computes a read-only proposal pinned to repository HEAD and recorded hashes. Only unchanged, exclusively created integration support files with applied receipts whose commits are ancestors of the selected branch are candidates. Intent-only/unmerged receipts cannot authorize removal. Shared/modified/unknown files are retained or reported as conflicts. Published content/dependencies block automated support removal.

Cleanup execution requires a second explicit owner POST with `approve_cleanup:true`, `operation_id`, `source_sha` and `proposal_digest`. It revalidates GitHub identity and file hashes, then opens a dedicated removal PR. It never merges or modifies the default branch. A disconnected website receives no general publication grant; this endpoint authorizes only the reviewed cleanup operation. Retries reconcile the same operation-specific branch/PR.

## Operations and rollout

The existing scheduled-discovery process runs `process_website_connection_operations`. Completed runs can still own previews, so preview shutdown uses a separate `/api/runs/{id}/preview/stop` worker call and retains terminal article history. Only explicit cleanup confirmation completes that part of the receipt; HTTP 409, pending builder cancellation, and unconfirmed hosted deployment removal remain pending. `python manage.py reconcile_website_connections --limit 20` is the equivalent explicit operational command. Both make external cancellation/revocation calls and should only be run in an authorized environment.

Direct article PR merges require an applied ownership receipt for the same publishing run, connection generation, branch and exact live PR head, then send that SHA as GitHub's compare-and-swap condition. Setup merges require the exact verified preview commit. External PR pushes cannot inherit an earlier approval just because checks pass.

Verified GitHub installation deletion/suspension, repository removal, transfer, rename, archive and deletion revoke selected connections. Default-branch pushes retain consent generation, advance configuration version, record observed source identity and revoke publication readiness. Source-bearing config writes and publication authorization reject stale `expected_source_sha`; readiness promotion also verifies the current GitHub branch SHA. Templates are retained. Duplicate push receipts are idempotent. The existing installation reconciliation sweep also revokes after confirmed installation loss; inconclusive API failures do not grant or revive access.

`python manage.py repair_website_connections --domain example.test` is read-only and prints identity/digest findings without template bodies. `--apply` is a separate operational repair: it creates disconnected legacy records and archives invalid template envelopes. It never guesses extracted template content, enables publication or edits GitHub. Use it only after reviewing the dry run and receiving environment-specific approval.

Deploy backend schema/API, worker tuple propagation/guards, and frontend capability handling as one coordinated release. Older repository jobs without tuples intentionally fail closed and need a newly authorized scan. Roll back by disabling repository work; do not bypass generation enforcement to resume legacy jobs.

### Canary control and business-outcome monitoring

Set `WEBSITE_CONNECTION_WRITE_MODE=disabled` during cutover, then `canary` with `WEBSITE_CONNECTION_CANARY_DOMAINS=talathrive.com` for the scoped acceptance test. `enabled` permits ordinary connection checks; it is not an authorization grant. Invalid modes and an empty canary list deny new setup, publication, merge, preview-deployment and reviewed Git cleanup writes. Read/inventory/export and disconnect remain available. Canonical client summaries display the same restriction. Previously accepted provider requests still require reconciliation.

The deployment workflow passes the repository variables `WEBSITE_CONNECTION_WRITE_MODE` (default `disabled`) and `WEBSITE_CONNECTION_CANARY_DOMAINS` (default empty) through validated SSH stdin into the host's `/root/mlai-backend/.env`. Compose supplies them to recreated application processes. Exact lowercase domain lists reject URLs, wildcards, IP addresses and partial matches. Keep writes disabled while backend schema/API and Content Factory are rolled out, then set the reviewed canary variables and rerun deployment. Returning the mode variable to `disabled` and rerunning deployment is the immediate repository-work rollback. Previously accepted provider requests still need reconciliation.

`APPROVED_MIGRATION_PLAN_SHA256` is an optional repository variable containing the lowercase SHA-256 of the reviewed candidate's complete `Planned operations:` block from `python manage.py migrate --plan --noinput`, with trailing newlines removed exactly as shell command substitution does. The validator excludes environment-specific import diagnostics and rejects missing or duplicate headers; every operation line remains in the digest. Deployment installs the approval before checking the plan; only the exact pending plan passes. For this rollout, only `content_factory.0042_website_connection_lifecycle` should be pending. Clear the variable after successful schema/API verification; the next deployment clears the host's old approval too. Keep migration 0042 applied during an application rollback: reversing it deletes lifecycle and audit history.

`python manage.py report_website_connections --domain talathrive.com --hours 24` is a read-only, sanitized JSON report. `--check` exits nonzero when business-level scans failed/blocked, reconciliation has remained pending for more than 15 minutes, or the bounded 2,000-run sample was truncated. It reports connection states, adoption backlog, inventory, quarantined templates, operation age, scan outcomes and mean lifecycle elapsed time. That elapsed time includes queue/processing and reflects durable row timestamps; it is not a CPU/build-duration measure. A read-only shadow comparison counts legacy scaffolded state and canonical current verification without granting authority. The existing admin usage payload includes this report as `websiteConnections`; an operator can wire the command's exit code into existing monitoring. It does not create an external notification subscription.

## Local verification

With explicit approval for migration 0042 and its existing dependency graph, the harness creates fresh synthetic databases, ignores `.env`, denies external network, and removes its database when finished:

```sh
python scripts/test_website_connections_database.py --engine sqlite --replay content_factory.tests_website_connections
python scripts/test_website_connections_database.py --engine postgres --replay content_factory.tests_website_connections
```

PostgreSQL mode creates a temporary socket-only local cluster using `initdb`/`pg_ctl`. It exercises actual row-lock concurrency; SQLite skips that one test. The harness also checks Django system configuration and migration drift.

GitHub repository-ID token scoping follows [the installation token API](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app); deleting and recreating a repository under the same name cannot inherit an old connection grant.

Repository callback follow-ups use durable `worker_followup` operations and are dispatched by the existing website reconciler after callback transactions commit. The original connection generation is retained, and disconnect cancels pending follow-ups. Worker HTTP never runs inside the owner/config authority transaction; local response projection takes a fresh fence. An accepted response that races disconnect is retained as cancelled history with remote cleanup queued.

The native mutation adapter currently accepts only the repository root on GitHub's current default branch. Every setup, publication, merge and preview authority check verifies immutable repository identity and the current default branch with GitHub; identical SHAs on different branches do not bypass the check. Other selections remain available for inventory, with explicit adapter-verification blockers.

Client compatibility: old native builds without the original connection tuple fail closed and must update MLAI before reconnecting. The browser is the initial canary client. Coordinate the compatible TestFlight and desktop releases after backend and worker contract verification; source parity alone does not establish that users have received a compatible binary.

Portable `content_only` generation deliberately dispatches without repository identity or consent. Its durable original mode permits only editorial run snapshots and content/progress/failure callbacks; sender-supplied mode, a publication result, a hosted-preview claim, or an organisation-config write cannot use this exception. A portable draft must start a new explicitly authorised repository workflow to publish. Native clients without reviewed connection fields fail closed and need a compatible update; browser canary verification does not imply a mobile binary has been distributed.

All reconciliation actions lease an operation in a short transaction and release it before remote HTTP. A conditional update checks the original claim timestamp afterward. Concurrent offboarding therefore cannot deadlock with the worker's cancellation callback or have an erasure/retention receipt replaced by stale transport results.
