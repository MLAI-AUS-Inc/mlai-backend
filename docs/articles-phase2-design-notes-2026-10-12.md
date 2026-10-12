# Articles adapter: Phase 2 design approvals

12 October 2026. Status: all eight designs approved by the owner in this chat on 12 October 2026.
Implementation still waits for Phase 1 deployment; production actions are not approved.

These notes make each required design decision reviewable while Phase 1 draft
PRs are prepared. Implementation starts only after the Phase 1 merge/deploy gate
in the user runbook. Approval of a note does not authorize customer writes,
production migrations, the production consistency sweep, or publication.

Source: `ARTICLES_ADAPTER_IMPLEMENTATION_PLAN_2026-10-12.md`, supplied by the
owner and available under `mlai-chat/docs/mlai/`. The owner approved all eight individual designs together in this chat on
12 October 2026. All flags below are proposed, default off, and must be documented
in each consuming repository's environment inventory. Backend JSON columns and
CF's durable JSON artifacts provide storage; any newly required migration needs
separate approval before it is created.

| Step | Repositories | Dependencies | Approval |
| --- | --- | --- | --- |
| 2.1 | backend, chat | Phase 1 deployed | Approved 12 Oct |
| 2.2 | CF, backend | 2.1 | Approved 12 Oct |
| 2.3 | CF, backend | 2.2 | Approved 12 Oct |
| 2.4 | CF, backend display | 2.3 | Approved 12 Oct |
| 2.5 | CF, backend | 2.1 | Approved 12 Oct |
| 2.6 | CF, backend guard | Phase 1 deployed, test owner/account | Approved 12 Oct |
| 2.7 | chat, backend | 2.1 | Approved 12 Oct |
| 2.8 | backend, CF | 2.3 | Approved 12 Oct |

## 2.1: one backend status owner and consistency sweep

**Approach.** Add the runbook's `content_factory/website_status.py` projection,
using real prepare/disconnect operations, their child runs and `project_journey`.
Return `version`, `state`, `step`, `title`, `message`, `nextAttemptAt`, `action`,
`evidence`, `operationId`, and `updatedAt` through bootstrap and website connection.
States are exactly `working`, `needs_you`, `done`, and `stopped`. Evidence entries
have stable check IDs, passed/failed/pending results, observed time and bounded
human explanations; they never contain provider response bodies.

Use existing operation due timestamps for `nextAttemptAt`; never invent a retry
in the projection. A working child must have a persisted scheduler/reconciliation
due time. Ensure that timer exists in the operation writer before presenting
`working`. A terminal operation takes precedence over a still-active CF child,
which becomes consistency evidence rather than reopening finished work. Multiple
historical operations cannot override the current selected generation/action.
Use deterministic ordering and one current operation ID.

Add `website_consistency.py` to the existing scheduler inventory in
`core/management/commands/run_scheduled_discovery.py`. Default to bounded dry-run
reporting. An apply command takes a scoped lock, rechecks the same generation and
state, and emits an idempotent repair receipt. It closes backend children of
terminal operations, identifies unverifiable adopted setups, and reports applied
revision batches with no applied revision. CF terminal-record repair remains CF's
responsibility. No refund or Roo balance mutation belongs in this sweep.

**Users.** Chat renders only `websiteStatus` when enabled; legacy fields remain
for one release. One action explains an interrupted preparation. The same status
appears on every article entry point.

**Flag and rollback.** `ARTICLES_WEBSITE_STATUS_V1_ENABLED=false` controls the
new projection/rendering. `ARTICLES_WEBSITE_CONSISTENCY_ENABLED=false` controls
reporting independently. Turning off status rendering restores legacy clients;
turning off the sweep stops future work. Already applied repairs remain audited.
Production `--apply` always requires its separate one-time approval.

**Acceptance.** Real model/projection truth tables for every operation/journey
combination; action and scheduled-time invariants; dry-run writes zero rows;
repeating apply produces no new repairs; stale-generation races cannot repair a
replacement connection; Playwright shows one state. The production dry-run report
is owner-run after deployment, without applying changes.

**Decision.** Approve the proposed names and current-generation precedence.

## 2.2: hosted verification, optional customer CI

**Approach.** Extend existing hosted builder, native execution contracts and
backend website verification. Build the exact selected current source SHA,
using 2.3's reviewed contract once available. Before 2.3, read the current v1
contract without creating a parallel file. The trusted browser controller checks
all listing pages, a preserved real detail, unknown-slug 404, inventory agreement,
and absence of setup examples. Adoption, setup merge and source-change paths use
the same verifier and bounded scheduling budget.

The trusted CF broker sends a versioned receipt containing the runbook fields,
repository identity, operation attempt/deletion watermark, verification checks,
a unique receipt ID and signing key ID. Canonical JSON is signed with HMAC-SHA256.
Backend authenticates the existing service transport and independently checks
signature, complete passing checks, source SHA, target/contract digest, current
connection/generation, operation authority, artifact digest and freshness. Receipt
IDs make repeats idempotent. The proposal is a dedicated 32-byte signing key,
separate from the general service credential, with current/previous key IDs for
rotation. No fallback to an unsigned or mismatched receipt.

**Threat model.** Repository code, build commands and dependencies are untrusted.
They run only in the existing isolated child sandbox with resource/time/output
limits, read-only input archive and narrowly controlled dependency egress. No
Docker socket, customer/GitHub token, backend service credential or signing key
reaches the child. Browser checks run under a trusted controller; content cannot
supply its own success receipt. The broker checks immutable archive/source and
artifact identities before signing. Reject symlink/path escapes, redirect escapes,
private-network destinations, unsigned observations, stale generations and
cross-company artifacts. Missing build secrets or private dependencies become
`needs_you`, with CI/hosted alternatives; credentials are never copied into a
sandbox as a silent repair.

Receipts arrive within 15 minutes of signing, allowing at most 60 seconds of clock
skew. Stored verification remains valid only while the exact source, contract,
target and current authority tuple match. Source changes invalidate it and queue a
new verifier run; current-source inspection failures do not grant publication.
The live deployed-route check remains required.

**Users.** Hosted verification is the default. Workflows write and Checks/statuses
read are required only for sites explicitly selecting in-repository CI. Existing
verified sites retain their proof during the flag rollout if its identity is still
current. The legacy CI path stays available.

**Flag and rollback.** `ARTICLES_HOSTED_VERIFICATION_ENABLED=false` exists in
both services. Enable the receiver before the producer. Turning it off restores
in-repo verification for new work without deleting receipts or opening workflow
PRs automatically. Hosted-only sites require an explicit CI choice to proceed.

**Acceptance.** Wrong SHA, contract, target, generation, key/signature, operation,
freshness, replay identity or artifact digest is refused. Static and Next.js zoo
sites reach Ready with no workflow file/grant; changing source invalidates proof;
CI-only sites still verify. Adversarial child fixtures cannot read broker secrets
or forge receipts. Test the real CF-to-backend receipt chain with network/DB seams.

**Decision.** Approve dedicated signing key, rotation policy, freshness bounds and
sandbox threat model. Provisioning the key is a later owner deployment action.

## 2.3: stable site contract v2

**Approach.** Extend `.mlai/articles-verification.json` in place with a versioned
schema under CF `contracts/`. V2 describes only `store`, `listing`, `detail`,
`existing_content_paths`, `build`, `publish`, and `verification` from the runbook.
Keep commands as bounded argument arrays and paths canonical and relative to the
repository/app root. Validation reuses `native_ci_contract.py` and path guards.
Reject connection/generation/operation identity in v2 rather than silently keeping
it. An explicit adapter normalizes v1 into the existing delivery/execution types.

Connection binding belongs in the backend target row and verification request /
receipt, never in committed site files. Use a canonical digest of the validated
site contract, excluding transient execution facts. Adopt unchanged v1 contracts
on reconnect with fresh external verification; do not open an upgrade-only PR.
Upgrade when another reviewed PR already needs to change the site. Ownership and
operation fences still apply to every request independently of contract version.

**Users.** Reconnecting re-verifies the same site without a new configuration PR.
Publishing still uses existing delivery adapters and reviewed PRs.

**Flag and rollback.** `ARTICLES_SITE_CONTRACT_V2_ENABLED=false` gates v2 writes
and reconnect behavior. Ship dual reading first. Roll back producers; retain the
v2 reader because already committed v2 files must remain readable. Never rewrite
customer contracts to downgrade automatically.

**Acceptance.** Strict schema/path/argv checks; deterministic v1 normalization;
no identity in a v2 file; reconnect/generation bump accepts unchanged site contract
with a new receipt; real json-registry, markdown-dir and component-file publishers
still work. Changed contracts fail old proof without requiring a database change.

**Decision.** Approve in-place dual-read/conditional-upgrade strategy.

## 2.4: existing posts and rendered inventory

**Approach.** Implement CF's existing dated article-registry design as written.
Capture read-only `source: existing`, `managed: false` records from an immutable
archive and reviewed route, with repository/source/card provenance and stable
repository+route IDs. No live crawl becomes a replacement source of truth.
Static and import-graph adapters index existing content without rewriting its
native detail/card markup. Generated records cannot take over existing routes.
Examples are excluded using 1.5's ownership decision; unresolved ownership fails
for exact owner action.

Generalize the public-output gate under the 2.2 verifier. Traverse every listing
pagination route, extract canonical article URLs, compare them with the sitemap's
article subset and registry, reject duplicates/hidden entries/examples and follow
same-origin bounds. Unavailable or incomplete inventory is failed/pending evidence,
never a verified empty set. Shared gate results retain route sets, counts and
source/artifact digests; clients receive only the count and safe evidence.

**Users.** Display `We found N existing articles` from a verified capture receipt.
Existing posts are usable for topic dedupe/internal links but remain owner content.

**Flag and rollback.** `ARTICLES_EXISTING_POST_CAPTURE_ENABLED=false` gates capture
and count exposure. `ARTICLES_RENDERED_INVENTORY_ENABLED=false` gates new hosted
verification evidence. Rollback stops new capture; keep read-only records and
adapter safeguards so records already captured never become writable.

**Acceptance.** The five acceptance checks in CF's existing design: 35 static
stories, five Next.js posts, repeated capture, unchanged detail/card hashes after
publishing, topic dedupe/internal-link use, and two complete disconnect/adopt cycles.
Add Astro or Hugo through the same real output gate; empty, paginated, missing,
duplicate, stale and example inventories must fail with useful evidence.

**Decision.** Approve the existing capture design plus framework-independent gate.

## 2.5: refresh a stale proposal without carrying approval

**Approach.** Extend `mutation_ownership.require_expected_head` callers. A moved
base creates one successor proposal on a fresh owned branch from the selected
current source. A durable refresh key includes previous proposal, new base,
connection generation and contract digest. Replaying the observation returns that
successor; repeated source movement supersedes it again within a bounded budget.

Recompute deterministic marker/contract/example changes from the archive. Model
scaffold changes use the old proposal as reference and run normal bounded checks.
Only persist `superseded_by` after the successor receipt exists. Backend marks the
old approval superseded and projects `needs_you: review refreshed changes`. Approval
is always for an exact proposal/base/head; no source movement inherits consent.
No main-branch write occurs in this refresh path.

**Users.** See one refreshed proposal with a short changed-since-approval summary.
Failures stop with one action rather than reopening old approvals indefinitely.

**Flag and rollback.** `ARTICLES_PROPOSAL_REFRESH_ENABLED=false` in both services.
Disable new refreshes to restore explicit retry behavior. Already superseded
approvals remain invalid, and owned successor branches remain tracked for cleanup.

**Acceptance.** Real moved-base chain with only GitHub/DB replaced; deterministic
refresh equals a fresh computation; duplicate observations create exactly one
successor; failed successor creation cannot erase the old evidence; stale approval
cannot merge either old or refreshed proposal; owner source remains unchanged.

**Decision.** Approve explicit re-review and durable successor identity.

## 2.6: nightly test zoo and orchestration-test guard

**Approach.** Extend `scripts/framework_fixtures.py`, `scripts/e2e_one_click.py`
and the existing nightly workflow; do not add a competing lifecycle harness.
Twelve frameworks each get five-existing-post and no-blog cases. Add 35-story
Tala-shaped and Vyavos-shaped cases, monorepo/selected-branch blockers until 3.5,
and a private-dependency case expecting the 2.2 owner-action result. Save immutable
fixture, source, feature-flag and tool/runtime versions alongside each result.
Use explicit expected terminal categories for negative fixtures.

Use only a dedicated approved GitHub owner and test account/wallet. The harness's
existing allowlist forbids customer repository/domain mutations. Validate exact
wallet identity, company and repo bindings before any lifecycle action. Local
fixture generation is separate from authenticated repo provisioning. The owner
supplies account/owner and secrets through GitHub environment settings.

Generate supported-framework documentation from two consecutive distinct scheduled
night runs of the complete required variant set on the same relevant version set.
Partial, skipped, missing, stale or manually replayed results cannot claim support.
A failure revokes support until two qualifying nights pass again. Retain redacted
raw results as Actions artifacts and use the existing configured Slack webhook
only when the owner enables the nightly runner.

Add an AST-based orchestration-test guard to backend and CF CI. New tests that
patch the named orchestrators must pair with a marked real-chain test. The real
marker is verified by checking forbidden patches in that test, with permitted
network/DB seams documented. Baseline legacy tests are inventoried explicitly;
new bypasses fail. Avoid rewriting or weakening old tests wholesale.

**Flag and rollback.** The existing empty `ONE_CLICK_SANDBOX_CONFIG` keeps nightly
execution off. Proposed reporting flag `ARTICLES_ZOO_SUPPORT_REPORT_ENABLED=false`
keeps generated support claims off until accepted. Disable the workflow/config to
stop mutations; preserve results and report why support is unknown. CI test guard
can ship independently after the marked baseline passes.

**Acceptance.** Generate all case shapes locally; refused customer configs and
wrong wallet identity; real connect/prepare/publish/disconnect sandbox chain; two
consecutive night support calculation and revocation; guard rejects a new stub-only
or falsely marked test. No supported-framework claim before actual night results.

**Decision.** Approve guard/reporting convention, then supply dedicated GitHub
owner and test account. Account creation, wallet allocation and secret provisioning
remain owner actions.

## 2.7: doctor panel and redacted diagnostics

**Approach.** Chat renders `websiteStatus.evidence` under `What we checked`, with
passed/failed/pending per stable check ID. Copy diagnostics uses a backend-owned
allowlist matching `inspect_website_prepare`, scoped to the authenticated selected
company/current generation. Prefer allowlisting safe identity/state/code/timestamp
fields to stripping arbitrary stored provider dictionaries. Mask emails at the
source and omit tokens, headers, repository content, URLs with credentials/query
secrets and raw errors. Shared redaction serves the command and API.

**Users.** Copy one bounded JSON receipt explaining the visible status; disabled
clipboard permissions produce a clear local message. No cross-company evidence
appears after community/company switching.

**Flag and rollback.** `ARTICLES_DOCTOR_ENABLED=false` gates the API/display;
turning it off removes the panel. Legacy operation diagnostics remain available.

**Acceptance.** Nested sentinel token/email/content tests; URLs/header/error
redaction; exact selected-company authorization; missing/pending evidence; real
Playwright panel + clipboard with the same status the backend returned. Document
read-only inspect usage in `docs/website-connections.md`.

**Decision.** Approve allowlist-based diagnostics and masked identity fields.

## 2.8: disconnect cleanup with bounded owned resources

**Approach.** Extend existing website disconnect and CF #971 cleanup receipts to
all ledger-owned branches, local previews and hosted deployments. Preserve current
revocation/cancellation fences before cleanup. Cleanup handles stable resource
identity plus original generation and recorded ownership; missing resources are
idempotent successes, owner-modified/unbound resources are retained with an exact
reason. Count every provider failure against a bounded attempt budget and then
stop with actionable evidence. Do not retry detached repositories forever.

Disconnect-keep preserves published pages. Disconnect-remove uses only the
reviewed ownership restoration plan; drift requires owner review. Reconnect uses
2.2/2.3 and never revives old approvals or cleanup authority. Future CMS/hosted
channels implement their own credential/export/unpublish actions through 3.2.

**Users.** One disconnect action stops work and shows remaining cleanup evidence.
Retained owner content is visible as retained; terminal cleanup cannot look queued.

**Flag and rollback.** `ARTICLES_DISCONNECT_CLEANUP_V2_ENABLED=false` gates the new
cleanup expansion. Turn it off to stop expanded scheduling while keeping existing
revocation and fences. Never restore revoked credentials as rollback.

**Acceptance.** Two zoo keep/remove cycles; customer edits remain unchanged;
missing resources replay safely; detached repository and provider outage exhaust
budget; stale-generation worker cannot cancel a replacement preview; every owned
resource is either removed or has a durable retained reason.

**Decision.** Approve ownership-only bounded cleanup and retained-resource receipts.

## Phase 3 decisions queued after these foundations

The channel classifier/interface must preserve the existing Git and portable
paths and put choice in existing organisation JSON. CMS connectors need their own
approved design and current primary API documentation before code. WordPress comes
first, then Ghost; later connectors follow customer demand. Hosted articles need
the owner's provider and default-domain choice before implementation. Monorepo and
non-default-branch gates remain until exact combinations pass the zoo.

The Phase 2 design approval does not choose a Phase 3 provider/domain or authorize
production operations. The
implementation report will track owner-approved design IDs, feature flags,
acceptance evidence and remaining deployment dependencies separately.
