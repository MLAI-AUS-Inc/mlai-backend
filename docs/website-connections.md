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

## Local verification

With explicit approval for migration 0042 and its existing dependency graph, the harness creates fresh synthetic databases, ignores `.env`, denies external network, and removes its database when finished:

```sh
python scripts/test_website_connections_database.py --engine sqlite --replay content_factory.tests_website_connections
python scripts/test_website_connections_database.py --engine postgres --replay content_factory.tests_website_connections
```

PostgreSQL mode creates a temporary socket-only local cluster using `initdb`/`pg_ctl`. It exercises actual row-lock concurrency; SQLite skips that one test. The harness also checks Django system configuration and migration drift.

GitHub repository-ID token scoping follows [the installation token API](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app); deleting and recreating a repository under the same name cannot inherit an old connection grant.
