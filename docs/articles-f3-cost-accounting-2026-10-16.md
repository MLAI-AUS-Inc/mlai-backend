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
