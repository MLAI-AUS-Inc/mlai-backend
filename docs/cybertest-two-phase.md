# Vibe Security phased purchases (disabled until coordinated release)

`CYBERTEST_PHASED_SCANS_ENABLED=false` is the default. Enabling it alone does not
enable billing: existing billing, live-billing and staging restrictions still
apply. Deploy the reviewed scan service, checkout UI and approved Roo migration
0047 together before enabling. Do not enable against an older scan service.

The staging Compose service explicitly forwards `CYBERTEST_PHASED_SCANS_ENABLED`
with a `false` default. Keep it false while releasing compatible services; merely
adding the variable to the host env file does not affect an existing container.
Before activation, run `python scripts/check_cybertest_staging_profile.py
--enabled --same-origin --phased` against the candidate. This checks Compose
forwarding and the actual Django profile without a database connection or model call.

## Purchase contract

The authenticated Cybertest service supplies `binding.purchase`, version 1,
containing `phase`, `tier`, `parentCheckoutId`, `planId`, immutable `commitSha`,
`sourceManifestSha256`, and bounded check IDs/kinds/required flags. The backend
normalizes this into the consent digest. It calculates prices; browser amounts
cannot override them. Legacy receipts without a purchase retain their policy.

| Purchase | Total points | Points due now |
| --- | ---: | ---: |
| Basic | 24 per block | 24 per block |
| Standard upgrade | 48 | 24 |
| Large upgrade | 124 | 100 |

An upgrade requires a delivered, settled basic purchase by the same payer for
the same company, workspace, repository, selected scope and immutable source.
A basic block is 64 `source_batch` checks: a basic purchase costs 24 points
for each started block (`basic_points` in `roo/cybertest_delivery.py`), so a
repository with 910 source batches costs 360 points. The scan service applies
the same rule to the same contract. Basic execution plans may be up to 1.5 MB.
Upgrade prices are unchanged.

Checks of a repository in `CYBERTEST_FREE_REPOSITORY_IDS` (GitHub repository
IDs; by default MLAI's mlai-au, mlai-backend, mlai-chat, content-factory and
valley-backend) are free. Every other repository is paid. The quote still records the normal price, so the scan
service's price check is unchanged, but `pricing_snapshot.sponsored` is set,
the receipt reports `sponsored: true` and `due_points: 0`, no balance is
required, and charge, refund and settlement write no ledger rows. The scan
service has already checked the payer's GitHub access to the repository.

There can be one active or delivered upgrade per basic purchase. A fully
refunded failed upgrade may be explicitly purchased again. There is no second
48-to-124 upgrade step. Expired unpaid offers can be replaced.

## Settlement API

`POST /api/v1/points/cybertest/quotes/{checkout_id}/settle-delivery/`
requires the existing server-only `X-Cybertest-Key`. Browser authentication is
insufficient. Body: `{audit_id, evidence, costs}`. Evidence contains the audit
and plan IDs, check observations, and a durably saved report SHA256. Only the
scan service may assert persistence, after history has acknowledged the save.
The backend validates membership, uniqueness, execution states and identity.
It cannot independently inspect the history service; this is a trusted service
boundary, not a browser-provided completion checkbox.

All required substantive checks plus a saved report meet minimum delivery.
Finding count is irrelevant. Optional omissions produce partial coverage and
do not force refunds. Below-minimum basic delivery retains only verified model
usage using the receipt's conversion snapshot, capped at the basic price paid. Unknown
usage leaves paid basic settlement pending. With sponsored basic delivery below
the minimum, the receipt settles with zero points charged or refunded even if
provider usage is unavailable. Below-minimum upgrades
refund the full 24 or 100 point difference even if usage is unknown; the parent
receipt is untouched. Replaying identical settlement is safe; contradictory
evidence is rejected. Disabling new billing does not disable compensation.

## Recovering a purchase

The service-only quote request can include `execution_plan`, bounded metadata
containing the exact commit, source digest, check manifest, source ranges and
resource limits. It must match `binding.purchase` and the selected scope. It is
stored inside the phase snapshot; source text, credentials and arbitrary extra
fields are rejected. The public checkout response does not expose this plan.

`POST /api/v1/points/cybertest/quotes/recover/` takes `{binding, token}` under
the existing server-only `X-Cybertest-Key`. Here `binding` is the current MLAI
payer/company/GitHub/workspace/repository/scope selection without a purchase.
It returns `{receipt: null}` or the matching pending basic receipt plus its
stored execution plan. Recovery never grants consent, debits points or starts
work. Unconfirmed links rotate to the supplied token; authorized receipts keep
their original consent and expiration. Charged/started receipts remain
recoverable after the quote's original expiration until settlement.

Creating a new planned basic quote locks its payer before checking the same
pending selection, so different web instances cannot each create an active
purchase. A losing request receives `BASIC_PURCHASE_PENDING` and performs the
recovery operation instead of retrying quote creation. Expired, uncharged
quotes and settled receipts do not block a new explicit purchase. This locking
path has passed disposable-local PostgreSQL concurrency acceptance. Hosted
staging acceptance remains required before feature activation.

The scan service must recheck current company and GitHub access, use the
recovered plan rather than a new preflight, and fetch its immutable commit
before execution. A completed purchase remains in history; this endpoint does
not restart it. Startup must leave the feature disabled when durable recovery
is not available.

For an upgrade, pass `{binding, token, parent_checkout_id}` to the same service
endpoint. It recovers only the matching payer/selection and parent receipt.
Charged, running and successfully delivered upgrades remain recoverable; this
does not dispatch them again. Fully refunded attempts and expired unpaid offers
are not recovered, permitting a new explicit purchase. `UPGRADE_EXISTS` during
quote creation is handled by recovery, not another quote-creation attempt.

Upgrade `execution_plan.algorithm` is `incremental-upgrade-v1`. It contains a
bounded list of additional runtime targets, local dependency pairs and the
available schema-policy set, plus explicit resource/price snapshots. It stores
paths and metadata, not source text. Its parent audit ID, base evidence digest
and base report digest must match the delivered parent receipt. The worker
independently rebuilds the plan from the pinned source before paid execution.
The model allowance is an admission estimate, not a promise of actual usage.

Ledger changes and receipt settlement are atomic. When a basic charge must be
partially retained, the shared ledger reverses the original whole charge and
retains verified usage in the same transaction. Failure rolls back both.

## Local evidence and remaining release checks

The authorized 387-migration inventory is pinned in
`cybertest-two-phase-test-migrations.json`. `scripts/test_cybertest_phases.py`
clears inherited settings, blocks dotenv and external network, verifies the
inventory hash and uses a new in-memory SQLite database. `--service-only` runs
the HTTP authentication tests without constructing a database.

Latest completed basic/recovery/upgrade/settlement database suite: 82 tests passed.
Upgrade plan/contract pure checks: 17 tests passed; service endpoint checks:
5 tests passed. The separate PostgreSQL concurrency suite passed 4 tests:
competing basic and upgrade quotes, duplicate charge/dispatch, and concurrent
full upgrade refunds. This used the exact approved 387 migrations in a fresh
local disposable database; its container/network were removed afterward.
SQLite alone is not proof of row-lock behavior. Hosted staging acceptance is
still outstanding.
After separate explicit approval on2026-11-12, only Roo0047 was applied to the
existing `staging_account_api` database on DO mlai-chat-staging. The exact pending
plan and file hash were verified; three columns and both constraints passed
database introspection afterward. Backend health remained ok. The migration file
was added to the running container for this operation; the next candidate image
must contain it. This is schema preparation, not two-phase application acceptance.
No production migration, real customer points or paid-model call was involved.

The staging private configuration reader also validates and preserves the optional
`CYBERTEST_PHASED_SCANS_ENABLED` flag across deployment and restart. Only literal
`true`/`false` values are accepted; enabling requires the complete enabled
test-billing service configuration. Omission preserves legacy behavior. The
installed `config.py` and its pinned helper hash must be updated together with
the reviewed Compose mapping before enabling phases. Do not bypass the existing
startup/network policy checks. Deployment/restart/isolation regressions:39 passed
locally, with no database or live Docker operations.

The public checkout response now includes `purchase_scope`: phase, total/due
points, pinned commit, grouped required/optional checks, declared runtime target
names and additional source-check file paths. It excludes private execution
limits, model settings, pricing rates and repository source. The full execution
plan remains on the authenticated service-only interface.

`scripts/test_cybertest_phase_postgres.py` verifies the same authorized inventory
before creating its test database. It requires a fresh local PostgreSQL instance
at loopback port55473 with the exact synthetic seed/user names, no public tables
and no previous test database. It blocks dotenv and inherited credentials and
external Python networking. The concurrency tests cover competing basic/upgrade
quotes, duplicated charge/dispatch and simultaneous upgrade refunds using local
synthetic shared-ledger rows. A successful SQLite run must not be reported as
evidence for these PostgreSQL locks. The disposable container/network must be
removed after the run; do not point this runner at an existing shared service.

## Founder report delivery

New Basic plans use `basic-source-v2` and store one `backendAnswer` of
`yes`, `no` or `unsure`. Version 1 plans retain their original schema.
Basic recovery accepts an optional `backend_answer`. A changed answer cancels
only an unaccepted quote. A previously approved or dispatched check returns
409 `BACKEND_ANSWER_LOCKED` and keeps its original scope.

A full, persisted report queues one private MLAI Chat message for the original
payer when `CYBERTEST_COMPLETION_NOTICES_ENABLED=true` on the web service and
discovery scheduler. Use the existing verified Chat notifier configuration.
The default is off. Test receipts never send a private message.

After a free continuation, the authenticated scanner can post cumulative
evidence to `/api/v1/points/cybertest/quotes/{checkout_id}/notify-complete/`
with `X-Cybertest-Key`. The body contains `audit_id` (the original audit) and
`evidence` (as for settlement with that root audit's ID). History must have
acknowledged the saved report. Every planned check, including evidence review
when present, must be completed. The callback returns `queued` or `disabled`
in its `status` field. Retrying it is safe. Charges and refunds continue to
use only their existing settlement endpoint.

The minute scheduler retries private messages for up to two days. The report
link checks company, workspace and account access when opened. The sender
rechecks recipient membership and verified devices immediately before delivery.
The message contains only a fixed completion sentence and the report link.
