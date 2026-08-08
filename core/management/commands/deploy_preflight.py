from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


# Pre-migration release gates, in the order deploy.sh used to invoke them as
# separate `docker compose run` containers. Each container cost ~6s of cold
# Django start-up, so the five gates spent ~30s booting to do ~5s of work.
# Running them in one process keeps the same fail-fast semantics for a single
# boot. Each entry is (label, command name, kwargs).
PREFLIGHT_STEPS = (
    (
        "Validating shared Redis security state",
        "validate_health_hack_ai_cache",
        {},
    ),
    (
        "Validating production URL configuration and service connectivity",
        "validate_prod_urls",
        {"check_connectivity": True, "warn_connectivity": True, "timeout": 8.0},
    ),
    (
        "Validating organisational-memory provider governance",
        "validate_org_memory_governance",
        {"environment": "production"},
    ),
    (
        "Preflighting PostgreSQL full-text and vector support",
        "check_org_memory_search",
        {"require_vector": True},
    ),
    (
        "Verifying GitHub App server credentials",
        "check_github_app_credentials",
        {},
    ),
    # Informational: prints what the migration step is about to apply, so a
    # failed deploy can be read back without re-running anything. `--plan`
    # returns before touching the database.
    (
        "Migration plan",
        "migrate",
        {"plan": True, "interactive": False},
    ),
)


class Command(BaseCommand):
    help = (
        "Run every pre-migration deployment gate in a single process. "
        "Fails on the first gate that fails, leaving the database untouched."
    )

    def handle(self, *args, **options):
        for label, name, kwargs in PREFLIGHT_STEPS:
            self.stdout.write(f"-- {label}...")
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr, **kwargs)
            except CommandError as exc:
                raise CommandError(f"{label} failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS("Deployment preflight passed."))
