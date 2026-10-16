# F3 recorded usage and operational alerts

## Existing evidence

B4's `website_goal_budget.account_usage` accepts cumulative original-run reports,
orders them by generation/state version and refuses contradictory or backwards
counters in legacy reports. It records model calls, tokens, provider/active seconds, model USD,
builds and previews. Unknown values remain null. `website_doctor.project_doctor`
already exposes all seven budget counters; F3 should reuse that contract.

CF's current `goal_usage.report` is a budget report: token totals retain a
reservation when provider usage is unavailable, USD uses a configured maximum
token rate, and build/preview totals count matching step attempts. These are not
all exact resource measurements. Preserve B4's conservative policy stream and
add explicit accounting quality/basis. Report actual tokens only when every
contributing call supplied valid usage, label configured USD bounds as bounds,
and instrument admitted build/preview invocations for their actual attempt
counts. Keep legacy step estimates distinguishable from that new meter.

The source audit reproduced a B4 protocol bug: the worker can report an admitted
model reservation and later settle it to smaller provider-reported usage. The
backend interpreted that legitimate decrease as invalid usage and stopped the
goal. New producer reports label `counter_semantics` as
`reservation_upper_bounds_v1`. When both snapshots carry that label, token,
provider/active-time and USD bounds may settle downwards. The goal budget keeps
each original run's highest observed bound, separately from its latest raw
snapshot. Unknown values remain unknown, with a sparse peak retained for later
known reports. Calls/builds/previews stay monotonic, including across null gaps;
legacy reports retain strict monotonic validation. Snapshot identities cannot
be replayed with changed semantics or counters. Enable the consumer before
enrolling workers in goal metering. Existing default-off B4 flags govern this
correctness fix; the separate accounting-quality stream remains future work.

`website_goal_runner.stop_goal` reserves one pending `budget_alert` identity.
The current owner-notification outbox covers genuine owner actions and proven
completion. Budget, stall and repair alerts need an operational channel rather
than another owner action. `WebsiteConnectionOperation` provides durable JSON
records and a globally unique idempotency key without a schema change.

F2's complete 24-run endpoint fixture is 245,471 bytes, 702.88 ms cold and
272.63–401.60 ms warm at backend `b8b923ce` (CI run 38811772034, all gates green).
Company accounting must be loaded separately, preserving this bootstrap path.

## Usage ledger

Use one internal completed operation per original worker run, with a dedicated
action and deterministic idempotency key. Store only the original organization,
company/connection/goal attribution, validated counter snapshot, snapshot
identity/digest and server observation time. Read the original scoped run and
accepted B4 report at the ingestion boundary; request bodies and model text
cannot supply accounting authority. Reused runs contribute once to company
totals. Preserve their original attribution rather than claiming reused work as
new spending by a later goal. B4's execution-budget policy remains separate.

Under a row lock, a newer monotonic snapshot replaces the same run's counters;
a replay makes no write. Conflicting, backwards, foreign or malformed reports
produce explicit incomplete accounting. Keep null distinct from measured zero.
Accounting observations never establish current repository or publication
authority, including after a reconnect or source change.

The initial ledger implementation uses `website_cost_ledger` and the pure
`website_cost_usage` validator/reducer. It reads the original goal named by the
worker's immutable `goal_budget`, checks its scope digest and saved repository,
generation, branch, app root and deletion watermark, then locks the globally
unique original-run ledger row. It does not acquire another prepare-goal lock
while a goal is advancing. Replays avoid writes; settlement is validated using
the B4 bound protocol. Contradictory snapshots preserve prior evidence and
mark accounting incomplete. Missing/invalid reports also receive a coverage
row. Accounting errors log a fixed code and cannot stop setup.

`ARTICLES_COST_ACCOUNTING_ENABLED=false` gates observation and reads. The
existing budget-enrolled goal runner observes scoped children, including
missing meters. `GET /api/v1/vibe-marketing/website-connection/usage/` and its
`/api/v1/my-startup/` facade require the selected company; optional canonical
`goal_id` filters remain within that company. The response is private/no-store,
reads at most 2,000 ledger rows plus a truncation sentinel, and reports
`observed_goal_meter_runs` coverage. Truncated history cannot produce a complete
total. Unattributed legacy work is not represented as zero or claimed as an
all-time company ledger. Internal usage/alert rows are hidden from the latest
lifecycle projection and generic operation receipt reads.

Precise quality ingestion matches CF's optional version-1 accounting object:
tokens/provider time expose known subtotals and call coverage; USD remains a
configured bound. Legacy token reservations never become exact accounting.
The optional isolated-verifier admission meter, lazy client, durable
observation recovery and operational alerts are described below. Compiler
subprocess invocation counts remain unavailable.

The admission-meter follow-up accepts CF's explicit
`isolated_verifier_admissions` basis for build/preview counters, independently
of workflow-step attempts. These are admitted isolated verification jobs,
including failed or uncertain executions, rather than compiler subprocesses or
hosted deployments. The company/goal detail read preserves that basis. Legacy
step estimates and incomplete admission coverage remain null; known zero stays
zero. Original-run admission peaks survive unknown gaps, and a later lower
counter produces incomplete accounting instead of reducing the total. Report
identity ordering, deduplication, erasure fences and B4 budget arithmetic remain
unchanged. The matching lazy client must accept/display this basis before the
default-off accounting rollout is enabled. Dedicated directory-verifier Celery
handoffs retain an explicit coverage gap until their original-run receipt
protocol is implemented; they cannot appear as measured zero.

The follow-up recovery path records `cost_pending_run_ids` with the accepted
goal transition. `_persist_prepare` reserves one `usage-refresh` operation in
the same transaction as its successful compare-and-set. A lost prepare claim
cannot reserve a retry. Original-run observations remain globally deduplicated;
recovery calls only stored-run/ledger reads and writes, never the worker or a
repository provider. A two-minute lease, the existing capped retry backoff and
B4's transient-attempt cap also bound crashes/reclaims. Replacing a pending
batch fences the old worker's completion. Exhaustion is internal attention and
does not stop the site goal or notify its owner.

The operation reconciler processes these records under the same default-off
accounting policy. Disconnect retains this read-only historical recovery;
purge/organization erasure removes its rows and cannot recreate them. While a
retry is pending, running or exhausted, the separate company read includes its
bounded source references as unknown observations (without exposing run IDs).
An older ledger value cannot become a complete current total during recovery.
Both ledger and recovery reads are bounded, and no accounting scan enters
bootstrap. Chat's optional lazy view is MLAI-AUS-Inc/mlai-chat#505. The
admission meter and budget/stall/repair outbox use the separate rollout paths
below. Compiler subprocess counts remain unavailable.

Ledger writes lock the connection and compare its retained deletion watermark
to the original scope. A logical website purge removes internal accounting
rows, and organization offboarding advances that watermark before erasing
them, including when financial proof retains an old goal row. In-flight
recovery cannot recreate erased observations. Retry reservations keep
connection-before-parent lock order; recovery claims lock only their own row
and release that claim transaction before reading original reports.

Exclude internal accounting/alert operations from lifecycle/latest-operation
projections. Respect existing disconnect, purge and organization erasure rules.
The projection describes retained recorded usage and its coverage, with an
observation time; it cannot claim erased or unmetered history is a zero sample.

## Company and goal projection

Add an authenticated, explicit-company read on the existing website facade or
doctor surface. Aggregate only ledger rows owned by that company/organization,
without loading raw run results or whole goal receipts. Expose the seven
counters plus report/unknown coverage. If any contributing sample lacks a
counter, its total remains unknown; a separately named known subtotal can still
be useful. Preserve measurement quality through aggregation; a reserved token
bound cannot become an exact token count. An empty ledger is unmeasured. USD is
operational model usage or a labelled configured upper bound, not a
new customer charge or a provider invoice reconciliation.

The detailed doctor UI can load this summary on demand. Bootstrap and its live
permission overlay must not scan the cost ledger. Existing current-user and
company ownership checks apply to every read.

## Alerts

Persist bounded, deterministic alerts for an execution-budget stop, a stage
exceeding its existing policy deadline, or a reconciler actually repairing
durable state. A poll, lease claim, replay or normal wait is not a new repair.
Owner-review/deploy waits follow their recorded policy and do not become a
generic active-compute stall. Use existing stage/retry timing sources; do not
invent a second timeout policy.

Reserve alert records with the accepted state transition. Identity includes the
original goal/scope and causal occurrence, so delivery retries and callbacks do
not duplicate an alert. Fixed kind/severity/reason codes, safe references,
timestamps and measured counters are sufficient; omit provider text, source
files, prompts, tokens and credential URLs. Alert delivery cannot restart setup,
change budgets or create a repository write.

Reuse a configured operational monitoring path with durable delivery outcomes;
do not send budget/repair bugs through owner-action notices. Missing delivery
configuration remains visible to operators and does not block the site goal.
Keep a separate default-off, server-controlled rollout/enrollment policy. No
live messages, new credentials or production operations are part of validation.

The first delivery increment covers accepted B4 budget stops.
`ARTICLES_OPERATIONAL_ALERTS_ENABLED=false` also requires the existing trusted
goal/B1 cohort. A pure projector validates the saved stop, binds its reason to
the original occurrence and emits fixed codes, safe IDs and numeric execution
bounds. `_persist_prepare` reserves one `goal-alert` row with the successful
goal compare-and-set, retaining the first incident snapshot. Owner-action
notifications remain independent.

Delivery reuses `CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID` and `SlackService`.
It claims a bounded lease, releases DB locks before transport, reuses a stable
client message UUID after uncertainty, and records only a matching provider
acknowledgement. The existing ops policy supplies three attempts with a
five-minute interval. Missing configuration is a visible internal state with
no outbound attempt; a later valid configuration can resume it. A changed
destination is not silently substituted after an attempted send. Erasure and
lost claims fence receipt updates, and no send path can create repository or
generation work. Local/CI validation uses mocked Slack clients only.

The B5 receiver reserves a repair incident in the same transaction as an
accepted authoritative pull projection and its delivery receipt. The identity
binds the original run, execution generation/state version and state digest.
Normal callbacks, duplicate or fenced snapshots, shadow checks and
observation-only projections do not create incidents. Original B4 goal/company
attribution is used only when the immutable admission scope validates; legacy
repairs explicitly retain an unavailable goal and unattributed company.
Delivery uses the same outbox and acknowledgement policy.

Stage incidents are recorded at the existing preparation policy's actual
expiry branches: strict non-deployment waiting, deployment observation and
transient retry deadlines. Their original start/deadline and safe stage/reason
bind a deterministic occurrence. Poll counts and intermediate retries cannot
create incidents, and repeated expiry observations retain the first snapshot.
An explicitly manual hosting fingerprint is an immediate owner-deploy wait,
not a stall; owner merge/review action branches do not enter stall policy.
The incident is reserved only with the accepted goal compare-and-set, including
when the same transition also records a separate B4 budget stop. No new timeout,
owner notice, setup dispatch or migration is introduced.

## Validation and completion

Run pure, database/network-denying tests locally for snapshot ordering,
shared-run deduplication, zero/null/invalid counters, tenant attribution,
coverage, alert causality and redaction. Exercise row-lock races, replays,
company authorization, internal-operation filtering and alert reservation in
the existing disposable PostgreSQL CI lane. No migration is proposed.

Validate CF's actual report/receipt integration and original-operation binding
before wiring the ledger. Reuse existing measured build/preview counters;
record unavailable meters as unavailable. Add the company/doctor read and its
lazy client with appropriate tests, then validate budget/stall/repair delivery
and replay behavior. Keep F2's endpoint budget and all rollout flags off during
this implementation.
