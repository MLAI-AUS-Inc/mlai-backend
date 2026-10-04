"""Read-only connection health and rollout comparison for operators."""

import json

from django.core.management.base import BaseCommand, CommandError

from content_factory.website_health import website_connection_health


class Command(BaseCommand):
    """Report durable outcomes; optionally return nonzero for monitoring alerts."""

    help = "Read-only website connection health; --check exits nonzero for failed scans or overdue reconciliation."

    def add_arguments(self, parser):
        """Accept an optional exact company domain and bounded observation window."""
        parser.add_argument("--domain")
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--check", action="store_true")

    def handle(self, *args, **options):
        """Print sanitized JSON without invoking providers or changing authority."""
        if not 1 <= options["hours"] <= 720:
            raise CommandError("--hours must be between 1 and 720")
        report = website_connection_health(domain=str(options.get("domain") or "").lower().strip() or None, hours=options["hours"])
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["check"] and report["alerts"]:
            raise CommandError("Website connection health has actionable alerts; see JSON report.")
