# MLAI backend

The Django API and background-work service for the MLAI platform. It owns
shared identity, persistent product data, integrations, scheduled work, and API
contracts consumed by the MLAI website and Roo.

For the cross-repository system map, start with
[`mlai-engineering`](https://github.com/MLAI-AUS-Inc/mlai-engineering). AI
coding agents must also read [`AGENTS.md`](AGENTS.md).

## What this repository owns

- Authentication, users, organisations, and browser session contracts
- Founder Tools, startup updates, content workflows, and integrations
- Hackathon APIs for eSafety, Watt The Hack, HealthHack, and MedHack
- Roo points and internal service endpoints
- Dormant account, membership, and bridge APIs created for the inactive
  Buzz/MLAI Chat deployment experiment
- Organisational memory ingestion, retrieval, review, and publication services
- Scheduled jobs and background workers

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the application map and runtime
boundaries. Subsystem documentation is indexed in
[`docs/README.md`](docs/README.md).

## Requirements

- Python 3.11
- Dependencies from `requirements.txt` and `requirements-engine.txt`
- Docker only for the multi-service local stack
- PostgreSQL and Redis/Valkey for production-like integration work

The smallest local checks use SQLite and do not require production secrets.

## Local setup for static checks

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-engine.txt
test -f .env || cp .env.example .env

DATABASE_URL=sqlite:////tmp/mlai_backend_dev.sqlite3 \
  python manage.py makemigrations --check --dry-run
DATABASE_URL=sqlite:////tmp/mlai_backend_dev.sqlite3 \
  python manage.py check
```

`makemigrations --check --dry-run` reports model drift; it does not write or
apply a migration.

## Database migration safety

> **Never create, run, or apply a database migration without explicit approval
> for that specific migration.** This includes indirect migration execution by
> a startup command, test harness, deployment script, or Docker Compose service.

The full local Compose web service currently sets `RUN_MIGRATIONS_ON_START=1`.
Do not start it until the pending migration set has been inspected and approved.
Do not assume that “local” or “test” makes migration execution implicitly
approved.

Useful read-only checks include:

```bash
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
```

`migrate --plan` inspects the plan only. It must not be replaced with
`manage.py migrate` without explicit approval.

## Tests

CI uses Django's test runner with SQLite for its main validation job. The test
runner may construct a disposable test database using migration state, so obtain
the required migration approval before running it. Once approved, copy the
current test selection from `.github/workflows/deploy.yml` rather than relying
on an old list in secondary documentation.

Start with targeted tests for the Django app you changed. Do not run tests
against production or a copied production database.

## Full local stack

The production-like local stack is defined in `docker-compose.local.yml` and
expects `.env`, `.env.local-docker`, the external `mlai-local-shared` Docker
network, and—for content flows—a sibling `content-factory` checkout. It runs
multiple schedulers and workers in addition to the web service.

Because web startup automatically applies migrations, the startup command is
intentionally not presented as a copy-and-paste quick start. After a maintainer
has approved the exact migration plan, follow the cross-repository local
development guide and inspect the Compose file before starting services.

## API entry points

The root URL map is [`mlai/urls.py`](mlai/urls.py). Major route families include:

| Path | Owner |
| --- | --- |
| `/api/v1/auth/` | Core authentication |
| `/api/v1/founder-tools/` | Founder Tools |
| `/api/v1/hackathons/` | Hackathon APIs |
| `/api/v1/community-chat/` | Dormant Buzz/MLAI Chat experiment integration |
| `/api/v1/org-memory/` | Private organisational memory |
| `/api/v1/public-brain/` | Reviewed public memory |
| `/api/v1/jobs/` | Jobs and scheduled-work APIs |
| `/api/v1/points/` | Roo points APIs |
| `/api/v1/integrations/` | External connector APIs |

## Configuration and deployment

`.env.example` is the configuration inventory, not a ready-to-use production
file. Empty credentials must remain empty until an approved development or
staging value is provisioned. Never copy production secrets into routine local
development.

Normal production deployment is owned by the reviewed GitHub workflow and
deployment scripts. New engineers should not deploy during onboarding. Consult
the relevant runbook in `docs/` for subsystem operations.

## Documentation status

Architecture and operational documents describe current behavior. Files under
`plans/`, dated audits, and implementation plans are historical context unless
another current document explicitly adopts them.

The `community_chat` application and its runbooks were created for the inactive
Buzz/MLAI Chat deployment experiment. Their presence in the codebase does not
identify an active or supported MLAI product surface.
