# D2: journal source refresh before reconciling or dispatching

The new internal `website_article_refresh_intent.reserve` prepares one durable
proposal identity for an unchanged paid article on a newly verified source. Its
independent `ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED` flag defaults off, and it
also requires the existing article recovery, goal, failure-policy, consent and
budget gates. No public request fields select it.

The function runs under the existing authority transaction. Canonical
`for_generation` verifies the current owner, company, payer, original spend and
absence of a refund; `article_run` selects the exact original paid run and
operation. It reads a complete trusted source-only revision chain from backend
rows and retains the original branch, app root and repository. The original
article must have a terminal, uncancelled, typed generation/state version. A
cancelled article cannot be reinterpreted as permission to refresh.

The prepare lease CAS journals a versioned `pending_reconciliation` intent with
current scope/source, original run/operation/version, payment identity and a new
proposed run UUID. Replays return the same saved identity. The original charge
and held receipt stay unchanged; no generation, workflow reservation, worker
call, branch, approval or publication occurs. Stale article approval/live/resume
fields are cleared when the intent is first recorded. The original evidence
remains on its historical run.

Unknown journal shapes, changed original article/payment/goal, incomplete source
edges, stale leases and foreign or duplicate history fail closed. Four distinct
source intents are the fixed journal limit; later work must apply the shared
B4 action budget and repeated-failure strategy policy before creating any
replacement operation. Unknown usage or an exhausted budget stops before a new
intent. A proposed run UUID and the journal checksum grant no effect authority.

## Remaining integration

This is a foundation with no runtime caller yet. The D2 coordinator must first
observe/reconcile the owned old PR, merge and live effects, then fence old write
authority and reserve a new current operation/stream. It must pair the worker's
immutable Git/article inputs with this intent and recheck owner/source/consent,
lease, budget and original payment immediately before every effect. Lost replies
must reuse the same intent and operation. Current source movement needs a fresh
trusted chain and must never alter an old approval or sealed snapshot.

The worker must create a new owned proposal, preserve the same original article,
prefer deterministic/native reconstruction, rerun exact build/render gates and
honor review consent. Model-written changes require fresh review. The controller,
publication-child selection and live proof readers still need the replacement
lineage. Article strategy escalation remains pending. Existing source guards,
component-revision comments and promotion snapshot checks remain intact.

Source wakeups currently cancel active children before rechecking the site.
Integrating refresh for those children requires a separately recorded,
backend-owned source fence; do not accept arbitrary cancelled runs or undo user
cancellation. Already-merged/already-applied articles must reconcile live evidence
instead of creating a duplicate publication.

No owner input is needed to finish this code path. Hosting and actual zoo
operational inputs remain separate. All rollout flags stay off; this foundation
performs no production/customer, paid model, hosting or deployment operation.

## Validation

186 related strict database-free tests pass, including 13 new intent tests using
the real controller, billing, source transition and journal code with persistence
seams. Replays retain one original debit; malformed/foreign history, source
movement, missing revision evidence, root cancellation, changed owner/company/
payment, unknown usage and stale leases are covered. The strict runner forbids
network and database connections. Model checks pass with no changes detected.
The new tests are in an existing selected module. No migration is introduced.
