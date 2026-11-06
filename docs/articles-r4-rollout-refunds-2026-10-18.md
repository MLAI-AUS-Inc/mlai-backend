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

The combined explicit test list passed 161 tests in 0.718 seconds. All 536 test
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

The backend flag snapshot also observes the optional
`CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED` switch. A toggle
then changes the saved rollout identity, without making cosmetic formatting a
prerequisite for B5 business delivery. This command changes no admission value.

## Authenticated original-goal process diagnostics

`GET /api/content-factory/articles/runtime-observation/` is a private service
read. It requires `X-API-Key` matching the configured `INTERNAL_API_KEY`; owner,
superuser and local-development access do not bypass that check. Its five exact
query selectors are `company_id`, `connection_id`, `connection_generation`,
`goal_id` and `target` (`ready` or `first_article_live`). Extra or repeated fields,
malformed IDs and a different goal target are rejected before observation.

The endpoint reuses the original-goal scope resolver against existing saved
company, connection and operation records, inside PostgreSQL REPEATABLE READ,
READ ONLY transactions. It does not call `_get_config`/`get_or_create`, create a
goal, hydrate a run, refresh state or dispatch work. Scope is read again after
the remote observation; changed company/connection/goal/child evidence or web
configuration is rejected. Existing/nested transactions are refused rather than
silently weakening the read-only boundary.

The responding backend web process reports an opaque process instance, exact
runtime revision when available, the named rollout flags and their explicitly
limited digest, and the actual selected organization's new-work membership
predicates. The named flag digest is deliberately partial: the publishing-input
candidate producer switch introduced separately is outside this v1 vocabulary.
It must never be treated as the complete effective configuration. This describes
the current responding process. It does not prove
the settings used when the original goal was admitted or advanced. Forked
processes cannot reuse the parent identity. Missing or invalid full release
metadata remains unknown; no Git checkout or short display SHA is substituted.

CF diagnostics come from the separate private endpoint introduced in CF #1090.
Transport requires both an explicitly configured `CONTENT_FACTORY_API_KEY` and
an HTTPS `CONTENT_FACTORY_URL` without credentials, query or fragment. It uses
the server configuration only, follows no redirects, verifies TLS and ignores
ambient proxy/netrc credentials. A bounded streaming read caps the body at
2 MiB and checks an elapsed ten-second budget between chunks, with connect/read
timeouts of three/eight seconds. Missing authentication, transport failure,
stale/future data, invalid schema or inconsistent flag digests remain unknown;
neither credentials nor remote error text enters the response. The shared HTTP
helper eagerly buffers responses, so this bounded path uses a fresh Requests
session instead.

The closed CF projection deliberately remains `goal_binding=unbound`, even when
the request's original backend goal resolves. It cannot prove which CF process
executed that goal. Every response has `coverage=partial`,
`original_execution_eligibility=null` and `effective_rollout=null`, and names the
missing execution/scheduler observations, CF association and owner-action audit.
There is no fleet-completeness claim and no nightly collector wiring. The
endpoint is `Cache-Control: no-store`; it changes no rollout or enrollment flag.

Tests exercise the actual service view, original-scope resolver, read-only SQL
fence and streaming HTTPS client. Their CF fixture comes from the actual #1090
producer under network denial, with provenance beside the fixture. Database,
remote HTTP and process boundaries are substituted locally; full database and
deployment checks remain CI/rollout responsibilities. No live observation was
performed during implementation.
