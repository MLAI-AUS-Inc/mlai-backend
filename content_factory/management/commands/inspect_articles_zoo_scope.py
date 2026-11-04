"""Resolve recorded zoo goal/run ownership without modifying state or providers."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection as database, transaction

from content_factory.website_zoo_scope import ScopeInputError, read_observation, select_cases, scope_report


class Command(BaseCommand):
    """Read closed original-goal locators in one PostgreSQL read-only snapshot."""

    help = "Read-only zoo scope evidence; this command grants no runtime eligibility or qualification."

    def add_arguments(self, parser):
        """Select only an existing bounded recorded observation artifact."""
        parser.add_argument("--observation", required=True)

    def handle(self, *args, **options):
        """Fence isolation/read-only mode before the first model lookup."""
        if database.vendor != "postgresql":
            raise CommandError("This command requires PostgreSQL REPEATABLE READ, READ ONLY.")
        if (database.in_atomic_block or (database.connection is not None and not database.get_autocommit())):
            raise CommandError("Start zoo scope inspection outside an existing transaction.")
        try:
            value = read_observation(options["observation"])
            select_cases(value)
        except ScopeInputError as exc:
            raise CommandError(str(exc)) from None
        try:
            with transaction.atomic():
                with database.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                report = scope_report(value)
        except DatabaseError:
            raise CommandError("Read-only zoo scope inspection is unavailable.") from None
        self.stdout.write(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
