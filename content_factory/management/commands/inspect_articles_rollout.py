"""Inspect company rollout configuration without changing it or calling providers."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection as database, transaction

from content_factory.website_rollout_inspection import rollout_report


class Command(BaseCommand):
    """Read effective backend settings and bounded operator CF observations."""

    help = "Read-only per-company Articles flags/enrollment; missing CF observations remain unknown."

    def add_arguments(self, parser):
        """Select one company and optionally supply its observed CF run scope."""
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--company-id")
        scope.add_argument("--domain")
        parser.add_argument("--cf-snapshot")
        parser.add_argument("--run-id")

    def handle(self, *args, **options):
        """Enforce PostgreSQL read-only before resolving the selected company."""
        from founder_tools.models import VibeRaisingCompany
        if database.vendor != "postgresql":
            raise CommandError("This command requires PostgreSQL SET TRANSACTION READ ONLY.")
        snapshot = None
        if options.get("cf_snapshot"):
            try:
                with open(options["cf_snapshot"], encoding="utf-8") as source:
                    content = source.read(65537)
                if len(content.encode("utf-8")) > 65536:
                    raise ValueError
                snapshot = json.loads(content)
            except (OSError, ValueError, UnicodeError):
                raise CommandError("CF snapshot must be valid JSON of at most 64 KiB.") from None
        with transaction.atomic():
            with database.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            companies = VibeRaisingCompany.objects.select_related("organization")
            company = companies.filter(pk=options["company_id"]).first() if options.get("company_id") else companies.filter(
                organization__domain=str(options["domain"]).strip().lower()).first()
            if company is None:
                raise CommandError("Company not found.")
            report = rollout_report(company.organization_id, cf_snapshot=snapshot, run_id=options.get("run_id"))
            report["company_id"] = str(company.pk)
            self.stdout.write(json.dumps(report, sort_keys=True, indent=2))
