# Backend runtime and dependency contract

This describes the checkout, not a verified production deployment. Changes to
production processes remain subject to the deployment rules in [AGENTS](../AGENTS.md).

## Process ownership

[`scripts/runtime-services.sh`](../scripts/runtime-services.sh) is deployment's
application-writer inventory. Tests compare it with the production Compose
services so adding a service without including it in stop/rollback handling fails.

| Group | Services | Startup selection |
| --- | --- | --- |
| Required | web, scheduler, jobs-worker, memory-worker, memory-scheduler, password-email-worker, community-email-worker | Every normal deployment |
| Bridge | bridge-worker, bridge-reconciler, bridge-retention | Existing bridge enablement and adapter credential checks |
| Analytics | analytics-sync | Existing complete analytics configuration checks |
| Handoff | web-candidate | Explicitly managed during code-only web handoff |

Code-only releases keep writers serving during checks and use the
[two-slot web handoff](backend-api-web-handoff.md) for replacement. The candidate
is not part of normal runtime startup. The retired committee remuneration container is stopped and removed by its
exact Compose project/service labels before any migration transition. On code-only rollback,
newly introduced writers with no prior image are stopped before restoring the
previous runtime.

All application writers participate in deployment's stop and rollback inventory,
including optional services that were running before a configuration change.
If stopping any writer fails, deployment aborts before applying migrations.
An incomplete migration transition keeps writers stopped for forward recovery,
including after a partially committed credential backfill. Once the full graph
is applied and checked, recovery may recreate the new image and verify a fresh scheduler tick. Recovery preserves the failure exit
status; it never rolls back to a plaintext-writing image after migration begins.
Adding a service also requires its Compose definition, settings inventory, tests,
and an owning subsystem contract. The database is infrastructure, not an
application writer.

## Shared images and retired features

The web start script defaults to 1,000 requests plus a random 0–100-request
jitter before recycling a worker. Explicit `GUNICORN_MAX_REQUESTS` and
`GUNICORN_MAX_REQUESTS_JITTER` environment values override those defaults. The
larger default depends on the bootstrap run-lifetime fix; see the
[application comparison and rollback](articles-g4-recycle-limit-2026-10-18.md).
Worker count/timeouts remain independently configured. Process memory profiling
is off unless explicitly enabled. These are checkout defaults, not evidence that
a production host was changed or has sufficient RAM for every workload.

Deployment builds two images: `web` targets the common `runtime` stage and all
non-browser workers reuse `mlai-backend-web`; `jobs-worker` targets `jobs`, adding
Chromium. Compiler tools stay in the dependency builder. Rollback generates
per-service image overrides from the saved manifest so every writer gets its
exact former image even when services share a normal image tag.

The rsync payload and Docker context omit tests, docs, generated static assets
and workstation files. Static assets are still collected and verified in the
common image. Release checks no longer rebuild every memory search vector or
write Firebase bucket CORS: those existing commands are explicit maintenance.
The governed Admin Brain rollout operations remain unchanged.

Office Manager, Victor intake/applicant reporting and committee weekly payouts
are retired. Models, historical migrations and financial evidence remain.
Release the Roo consumer retirement and closed Victor website before the backend
removes their endpoints. See [Office Manager retirement](office-manager.md).

## Scheduling and Jobs

`run_scheduled_discovery` calls the bounded `enqueue_daily_jobs` entry point.
The dedicated `run_jobs_worker` command consumes persisted queued runs. Its
`--once` mode consumes at most one run; normal mode polls every five seconds.
Both manual API jobs and scheduled jobs use the existing persisted run settings.
`run_periodic_command` keeps discovery and memory scheduling in one Django
process each, closes stale connections around every tick and preserves failure
handling: discovery exits and clears its health marker on failure; memory waits
and retries. Local discovery retains its previous retry behavior.
The historical `run_daily_jobs_scheduler` service retains synchronous execution
by default for explicit callers; the shared scheduler deliberately uses its
enqueue-only wrapper.

The daily-run endpoint authenticates the opaque Jobs Bearer token or the existing
Roo API-key forms through a service authenticator. Browser JWT parsing must not
intercept these keys. Invalid or missing service credentials return 401; a valid
service key queues a run and returns 202.

Returned runner failures, nested queued-run failures, positive failure counts,
and halted scheduling now produce an overall command failure while allowing the
other selectors to run. Each selector logs its name, returned status and duration.

Ship the scheduler and Jobs worker together. A scheduler upgraded without its
worker will leave runs queued. Single-worker operation is the current baseline;
there is no new fencing token, heartbeat, or automatic recovery for a process
killed after marking a run running. Do not blindly retry such a run: publication
may already have happened. A durable lease and side-effect idempotency design is
still required. Other scheduler runners also still need latency review.

## Aggregate queue health

`python manage.py queue_health --max-pending-seconds 300 --fail-on-degraded`
is a read-only aggregate check. It reports due password-email backlog age,
expired email claims, queued Jobs age, and running Jobs count. It returns a
nonzero exit code when an email claim has expired or a due/queued item exceeds
the age threshold. Without `--fail-on-degraded` it always reports the JSON result.
It does not output recipients, tokens, request payloads, or financial data.

This command does not prove a worker is alive when its queue is empty and does
not detect abandoned running Jobs. Connect it to the owning environment's
monitoring only during an authorised operations change. It has not been run
against production as part of this refactor.

## Python dependencies

Python 3.11 is the supported runtime. Both requirements source files resolve into
[`requirements.lock`](../requirements.lock), installed with hash verification
by CI and Docker. The framework target is Django 5.2.17 with DRF 3.16.1 and
SimpleJWT 5.5.1. The psycopg binary distribution avoids an implicit dependency on
a machine's locally installed libpq for Python imports.

Static storage uses Django's `STORAGES` configuration. Production and the
`DJANGO_STATIC_BUILD` image-build mode select WhiteNoise's compressed manifest
storage, while local debug mode uses ordinary static storage. Image builds verify
hashed Django Admin and DRF assets before any deployment or migration starts.

Regenerate the lock after intentionally editing either requirements input:

```sh
env -u UV_INDEX_URL -u UV_EXTRA_INDEX_URL uv --no-config pip compile requirements.txt requirements-engine.txt --python-version 3.11 --universal --generate-hashes --no-emit-index-url --default-index https://pypi.org/simple --output-file requirements.lock
python scripts/check_dependency_lock.py --record-inputs
python scripts/check_dependency_lock.py
```

Review the dependency diff and run the applicable checks before accepting it.
The input-signature check detects an edited source file paired with an old lock;
it is not a dependency security audit. Hash verification fixes Python package
resolution. The Docker base tag, apt packages, Chromium installation and
production image build are not yet one immutable artifact tested by CI.

## Test assignment

`scripts/check_test_assignment.py` compares repository test modules with CI's
explicit commands. New modules outside the selected lanes fail the check.
Class/method-only selections are reported as partial. Existing omissions are
listed in [`tests/ci-unassigned-baseline.json`](../tests/ci-unassigned-baseline.json)
with a review date of 14 October 2026. This is selection debt, not evidence that
those tests pass or a quarantine of verified failures. Extending the date alone
does not resolve the omissions. The check does not measure assertions, branch
coverage, or detect a newly omitted method inside an already partial module.

`scripts/test_without_database.py` runs explicitly selected unittest and
SimpleTestCase modules with synthetic settings, dotenv disabled, and in-process
database/network connection guards. It rejects Django database test cases.
Tests requiring their own standalone settings should use their documented
runner, as the editorial unit lane does. Database-backed tests still require
specific migration approval before local execution.

The proposed [26 September disposable-test inventory](refactor-release-test-migrations-2026-09-26.json)
is enforced by `scripts/test_refactor_database.py`. The harness requires recorded
approval, an exact file set and SHA-256 match (including the new migrations), and
a graph without conflicting leaves. Until the proposed scope is approved, the
harness refuses replay. The previous September inventories remain historical
records; they do not cover the current integrated graph.

After that scope is approved, the harness creates its own temporary PostgreSQL
cluster with Unix-socket access only, or a fresh temporary SQLite database, with
synthetic settings, dotenv disabled and external networking blocked. `--replay`
seeds plaintext at Content Factory 0038; `--replay-from-main` instead seeds an
existing 0041 editorial record, preserves that branch while applying credential
compatibility, and verifies the final merge leaves both editorial content and
encrypted credentials intact. Run the two modes in separate invocations. The
harness rejects existing database URLs and removes its database files after
stopping the cluster. This remains scoped test approval, not production approval.
