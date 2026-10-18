# Fixture rollout inspection and refund retention

R4 preparation adds `inspect_articles_rollout` and prevents website lifecycle
cancellation from cancelling the original first-article refund outbox. No flag
or organization enrollment changes are made.

## Refund behavior

`transition_connection` first calls the existing `cancel_held_goals`, which
cancels the original preparation and reserves its credit obligation. The next
bulk cancellation previously cancelled that same `goal-refund` row, overwrote
its receipt and prevented `advance_refund` from claiming it. Existing queued
refunds were affected too.

The bulk cancellation now excludes `goal-refund`. Its original generation,
receipt, attempt and settlement lease survive disconnect and other lifecycle
revocations. Ordinary scans and work still cancel. Settlement uses the existing
original-goal billing/refund path and remains available after rollout is off.
No refund is issued by disconnect itself and no new payment policy is added.

This is an intentional default-path correction for existing paid goals. Do not
describe the PR as behaviorally inert because rollout flags default off.

## Read-only inspection

From the deployed backend, an operator can run:

```sh
python manage.py inspect_articles_rollout --company-id COMPANY_UUID
python manage.py inspect_articles_rollout --domain fixture.example.test \
  --cf-snapshot /tmp/cf-rollout.json --run-id ORIGINAL_FIXTURE_RUN_ID
```

The command requires PostgreSQL and executes `SET TRANSACTION READ ONLY` before
the company lookup. It never calls providers, prepares goals, edits configuration
or enrolls organizations. It reports only the selected company's membership
booleans, not the enrollment lists of other organizations or raw environment
values. The deploy's raw `APP_RELEASE`, if it is a full lowercase Git SHA, is
reported as the backend revision. `APP_COMMIT_SHA` is a compatibility fallback
only when `APP_RELEASE` is absent; an invalid primary value remains unknown.

Every B through F rollout row lists its backend switches and prerequisites and
the required CF counterparts. Goal eligibility uses the actual `goals.enrolled`
predicate, including taxonomy policy and both organization lists. The combined
F3 group also needs goal enrollment because its operational-alert path does.
Its CF counterpart is `CONTENT_FACTORY_ARTICLES_COST_ACCOUNTING_ENABLED`; an
unobserved counterpart remains unknown even when backend switches are on.
Setup delivery has a separate enrollment list.

Rows are `disabled`, `unknown` or `configured`. Configured means the inspected
configuration is eligible for new work; it does not prove an active goal,
deployed image, site authority, successful milestone or completed two-night
window. Existing sticky delivery and financial reconciliation can continue when
new-work configuration is disabled.

## CF observation contract

Without a CF observation, required worker flags and run predicates remain null.
The optional JSON file is an **operator snapshot**, not a signed attestation or
authorization input. Collect it inside the actual CF runtime and transfer it
without credentials. The backend never guesses CF values from its own settings.

The snapshot has these fields:

- `schema_version`: integer 1.
- `organization_id`: the selected backend organization ID.
- `observed_at`: an aware ISO timestamp, at most five minutes old and no more
  than 30 seconds in the future.
- `revision`: the observed full lowercase Git SHA, or null if unknown.
- `flags`: a map from the CF flag names printed in the report to actual Boolean
  values observed in that runtime. Missing or non-Boolean values remain unknown.
- `run_id`: the exact run selected with `--run-id`, if run evidence was inspected.
- `run_predicates`: Boolean or null observations for `taxonomy_enabled`,
  `setup_delivery_enrolled` and `goal_usage_enabled` for that exact run.

The producer must use CF's real configuration and admission predicates rather
than a shell truthiness shortcut. In particular, taxonomy emission and durable
setup enrollment can persist for sticky runs; goal usage also needs a real
`goal_budget` in the run request. Missing or unreadable run evidence is unknown,
not false. The inspector omits unrecognized keys and private values.

A CF runtime collector and the public original-goal human-action audit remain
separate R3/R4 work. An operator snapshot alone cannot qualify a zoo night or
certify service revisions. No production command was run during implementation.

## Validation

The new disconnect test runs the real lifecycle cancellation, original-goal
billing and refund worker with only persistence and PointsService boundaries
replaced. On unchanged main, both newly reserved and existing refund rows become
`cancelled`; after the fix they settle once with rollout disabled. Ordinary work
still cancels. A remote-only database regression covers actual SQL exclusions
for pending, running and needs-attention rows, preserving their receipts/leases.
It does not claim to integrate a real payment ledger.

The inspector tests run the actual management command and report logic with the
database boundary replaced, prove the read-only query ordering, and cover both
organization lists, separate setup enrollment, stale/foreign/run-mismatched CF
snapshots, strict Boolean values, secret omission and unknown worker state.
Two review regressions first failed against the initial inspector: deployment
uses the raw full `APP_RELEASE`, and F3 alerts require goal enrollment. Both now
pass, including invalid or empty primary revisions and the absent-only fallback.

```sh
python scripts/test_without_database.py \
  content_factory.tests_website_zoo_scope_unit \
  content_factory.tests_website_rollout_inspection_unit \
  content_factory.tests_website_first_article_billing_unit \
  content_factory.tests_website_goal_unit \
  content_factory.tests_website_doctor_unit \
  content_factory.tests_website_first_article_unit \
  content_factory.tests_website_strategy_budget_unit \
  content_factory.tests_website_status_unit \
  content_factory.tests_setup_delivery_unit
python scripts/check_test_assignment.py
python scripts/test_without_database.py --check-models
```

The combined explicit test list passed 160 tests in 0.743 seconds. All 536 test
modules are fully assigned, and the read-only model check found no changes.
The new scope module has nineteen tests, including actual goal/dispatch/request
lineage and compilation of the exact PostgreSQL queries without a connection.

No local database, migrations, services, production operations or customer
repositories are used. Database coverage belongs to the already authorized
disposable CI; no new migration is introduced.

## Original-goal scope observations

The separate `inspect_articles_zoo_scope --observation <recorded-night.json>`
command resolves a bounded Ready/full observation to its actual company,
selected connection, original prepare goal and saved owned worker runs. It
starts a fresh PostgreSQL REPEATABLE READ, READ ONLY transaction before model
lookups; an existing transaction or failed read-only setup is refused. It does
not create config, refresh status, reconcile work or call a provider.

Saved scope and original-goal lineage must match. Missing or contradictory
child evidence remains incomplete, without a latest-run fallback. Only safe
identifiers, closed field names and digests leave the command; encrypted config,
raw request/results, paths and credentials do not. The input reader opens every
path component without following symlinks and rejects non-regular, oversized or
changing files.

This is attribution evidence, not observed runtime eligibility. Collect it
before post-live adoption/disconnect changes the selected connection. Actual CF
saved requests and executing worker roles still need to be bound through the
protected sandbox collector, with runtime observations at admission/completion.
That transport, fleet coverage and runner wiring remain unfinished; this
command cannot qualify a zoo night or close the partial action audit.
