# Agent runtime initial schema proposal

Proposed on 5 October 2026. **No migration has been created or applied.**
Approval requested for one additive `agent_runtime.0001_initial` migration
containing the models below, including the OAuth records defined in
`agent_runtime/oauth_models.py`. It must not alter existing tables or backfill
existing accounts, tokens, grants, or messages. Features remain disabled.

All UUID primary keys are server generated. User and organization references
use existing identities; no administrative capability is inherited. Timestamps
are timezone aware. Bearer secrets and authorization codes are stored as
one-way hashes. Optional webhook signing secrets are authenticated-encrypted
inside the existing subscription delivery JSON; they are never plaintext.

| New model/table | Fields and purpose |
| --- | --- |
| `AgentInstallation` | UUID id; owner user FK; nullable organization FK; name; runtime (`hermes`/`external`); nullable unique Nostr public key; active flag; created/updated timestamps. |
| `AgentGrant` | UUID id; installation FK; exact scope/resource JSON lists; positive generation; expiry/revocation; concurrency, daily task, daily cost and task-duration limits; created/updated timestamps. |
| `AgentResponsibility` | UUID id; grant FK and captured generation; name/instruction/resource; active flag; nullable interval seconds (null is one-shot); next/last scheduled times; timezone and optional quiet-hour times; limits/delivery JSON; created/updated timestamps. |
| `AgentTask` | UUID id; grant FK and captured generation; optional responsibility FK; idempotency key and canonical input hash; instruction/resource/input references/limits/delivery; lifecycle status; due/deadline; lease UUID/epoch/expiry/worker; attempts/max attempts; reserved/actual cost, model-call/token counters; result/error; created/updated/finished timestamps. |
| `AgentCredential` | UUID id; grant FK; kind (`access`, `refresh`, `task_tool`, `task_model`); unique SHA-256 token hash; captured generation; audience; scope subset; expiry/revocation; optional task FK/lease epoch; optional refresh-family UUID/replacement credential FK; created timestamp. |
| `AgentEvent` | Incrementing internal id; source/source-event identity; resource/type; bounded payload and canonical hash; actor kind, chain UUID/depth; occurrence/creation timestamps. External cursors encrypt and bind the internal position to a grant generation and resource. |
| `AgentSubscription` | UUID id; grant FK/captured generation; exact resource/event-type list; optional task instruction; durable event cursor; active and agent-event flags; bounded chain depth; limits/delivery; created/updated timestamps. |
| `AgentArtifact` | UUID id; task FK; name; unique private storage key; media type; SHA-256; byte length; creation/optional expiry. Storage data stays outside the database. |
| `AgentActionProposal` | UUID id; task FK/captured grant generation; action kind/resource/payload/hash/idempotency key; lifecycle status; approving user/time/exact approval hash; expiry; delivery lease UUID/expiry; receipt/error; created/updated timestamps. |
| `AgentAuditEvent` | Incrementing id; optional grant/task FKs; event type; bounded non-secret metadata; creation timestamp. |

Core constraints: grant generation >=1; unique task `(grant,idempotency_key)`;
unique source event `(source,source_event_id)`; unique action
`(task,idempotency_key)`; unique credential hashes, installation public keys and
artifact storage keys. Due-task, task/action lease and event-resource indexes
support bounded worker scans. Owning core foreign keys use PROTECT; replacement
credential references use SET_NULL. The OAuth binding uses CASCADE as specified
below.

OAuth code/binding records are included only with the exact schema in
`agent_runtime/oauth_models.py`; review its fields with the generated migration
before application. No existing migration should be modified. No automatic
startup migration or production application is included in this approval.

Validation proposed after separate explicit authorization: generate and review
the one additive migration; create a disposable local database with synthetic
settings and the enumerated dependency migration closure; test grants,
concurrent claims, expired leases, revocation, OAuth rotation and outbox
ambiguity. Never point this runner at an existing or production database.

Until authorized, use only database-free unit tests and model checks. Adding the
app to settings is source registration, not permission to run migrations.
The empty `agent_runtime/migrations/__init__.py` marks the app as migration
managed, preventing Django's unmigrated-app `syncdb` path from creating these
tables implicitly. It contains no schema operations or initial migration.

Implementation review before schema approval: database-free tests cover
scope/resource/current-membership intersections, stale leases and revocation,
exact completion retries, quiet hours, scoped prior-result context, action
approval hashes, ambiguous delivery, event cursor isolation, webhook callback
verification and bounded egress, and model-token/cost reservations. A read-only
model check found exactly 12 models and four explicit constraints with no model
warnings; the existing migration graph can be inspected without a connection.
These checks do not establish PostgreSQL concurrency behavior or live provider
interoperability. Both still require the approved disposable database and an
explicitly authorized isolated pilot.

Hermes shell permission requests currently stop the task with
`error_code=approval_required`. Bounded, redacted command details are stored for
the owner to review as data. Approving and resuming a shell command is not
enabled. This is distinct from the implemented exact-message approval outbox,
which has its own disabled-by-default write gate and current-member checks.

# OAuth schema addition (5 October 2026)

Two additional tables are part of the proposed initial `agent_runtime` migration:

- `AgentOAuthCode`: UUID primary key; unique SHA-256 `code_hash` (64 characters);
  protected foreign key to `AgentGrant`; captured positive `generation`;
  `client_id` (255), exact `redirect_uri` (2000), S256 `code_challenge` (43),
  `audience` (500), JSON `scopes`, required `expires_at`, nullable `consumed_at`,
  and automatic `created_at`. A code is consumed once inside the owner/grant
  revocation fence. No raw authorization code is persisted.
- `AgentOAuthBinding`: one-to-one `credential` primary key referencing
  `AgentCredential` with cascade deletion; `client_id` (255); automatic
  `created_at`. Every OAuth access and refresh credential is bound to its
  preregistered client. Rotation detects replay and revokes that refresh family.

OAuth clients are explicitly registered in configuration; there is no dynamic
registration or client table in this pilot. Optional MCP webhook subscriptions
reuse `AgentSubscription.delivery` JSON for encrypted callback configuration,
expiry and bounded retry state; no additional table or column is proposed.
Delivery verification and egress protection are required before enabling the
separate webhook feature flag. Portable authenticated event cursors remain
available independently. This addition creates no migration file and does not
authorize applying any migration.
