# AI agent contributor guide

Read [`README.md`](README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the
relevant subsystem document in [`docs/README.md`](docs/README.md) before making
changes.

## Non-negotiable safety rules

- Never create, run, or apply a database migration without explicit user
  approval for that specific migration.
- Treat commands that apply migrations indirectly as migration commands. The
  web service in `docker-compose.local.yml` sets `RUN_MIGRATIONS_ON_START=1`.
- Never use production credentials or production data for routine local work.
- Do not deploy, invoke production workflows, or execute operational repair
  commands unless the user explicitly requests that action.
- Keep secrets out of source, logs, issue text, fixtures, and documentation.

Safe inspection commands include `python manage.py makemigrations --check
--dry-run` and `python manage.py migrate --plan`. Neither grants permission to
apply the reported migrations.

## Repository conventions

- Django project configuration is in `mlai/`.
- Domain applications are top-level Python packages and are registered in
  `mlai/settings.py`.
- The public URL composition root is `mlai/urls.py`.
- Keep business logic in the owning domain application; avoid introducing new
  cross-application coupling through `core`.
- Preserve organisation scoping, authentication, throttling, and audit behavior
  when adding or changing an endpoint.
- Update the relevant API contract or runbook when externally observable
  behavior changes.

## Validation

Use Python 3.11 and install both requirements files. Run the narrowest relevant
checks first. CI's canonical commands and targeted test modules live in
`.github/workflows/deploy.yml`.

Django tests may construct a database using migration state. Obtain explicit
migration approval before running them. Do not change migration files merely to
make a documentation task pass.

For documentation-only changes, validate Markdown links and paths without
starting services or running database-backed checks.

## Documentation routing

- Cross-repository ownership: `mlai-engineering`
- Repository architecture: `ARCHITECTURE.md`
- Subsystem and operations index: `docs/README.md`
- Current configuration inventory: `.env.example`
- Historical proposals: `plans/` and explicitly dated documents

When documentation disagrees with current code or configuration, report the
discrepancy and update the owning document. Do not silently infer production
state from an implementation plan.
