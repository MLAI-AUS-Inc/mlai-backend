from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.urls import Resolver404, resolve


# Routes that must stay resolvable after a deploy. A URLconf change that drops
# one of these surfaces to callers as a plain 404, so it is checked here rather
# than discovered by a user mid-upload.
REQUIRED_ROUTES = (
    "/api/v1/vibe-raising/uploads/video/session/",
    "/api/v1/vibe-raising/uploads/video/complete/",
)

# Partial unique index backing the coworking booking concurrency guard. It is
# created by migration, so a missing index means the guard silently stopped
# protecting against double bookings.
COWORKING_BOOKING_INDEX = "unique_active_booking_per_user_date"


class Command(BaseCommand):
    help = (
        "Run every post-migration deployment step in a single process. "
        "Fails on the first step that fails, before runtime services restart."
    )

    def handle(self, *args, **options):
        # Same fail-fast ordering deploy.sh used across seven separate
        # `docker compose run` containers, minus six cold Django boots.
        self._step(
            "Verifying migration readiness",
            "migrate",
            {"check_unapplied": True, "interactive": False},
        )
        self._step(
            "Verifying vector installation",
            "check_org_memory_search",
            {"require_vector": True, "require_installed": True},
        )
        self._step(
            "Rebuilding memory search vectors",
            "rebuild_memory_search_vectors",
            {},
        )
        self._step(
            "Verifying startup update schema",
            "validate_startup_update_schema",
            {},
        )

        self.stdout.write("-- Verifying Vibe Raising video upload routes...")
        for route in REQUIRED_ROUTES:
            try:
                resolve(route)
            except Resolver404 as exc:
                raise CommandError(f"Required route {route} no longer resolves.") from exc
        self.stdout.write("vibe raising video upload routes ok")

        self._step(
            "Configuring Firebase Storage CORS for direct video uploads",
            "configure_firebase_storage_cors",
            {},
        )

        self.stdout.write("-- Verifying coworking booking concurrency guard...")
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [f"public.{COWORKING_BOOKING_INDEX}"])
            index_name = cursor.fetchone()[0]
        if not index_name:
            raise CommandError(f"{COWORKING_BOOKING_INDEX} is missing")
        self.stdout.write(index_name)

        self.stdout.write(self.style.SUCCESS("Deployment post-migration checks passed."))

    def _step(self, label, name, kwargs):
        self.stdout.write(f"-- {label}...")
        try:
            call_command(name, stdout=self.stdout, stderr=self.stderr, **kwargs)
        except CommandError as exc:
            raise CommandError(f"{label} failed: {exc}") from exc
        except SystemExit as exc:
            # `migrate --check` signals unapplied migrations with sys.exit(1)
            # rather than CommandError. Relabel it so a failed deploy names the
            # step that failed instead of exiting silently.
            if exc.code:
                raise CommandError(f"{label} failed: exit status {exc.code}") from exc
