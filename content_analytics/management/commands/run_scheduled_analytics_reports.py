import json
from datetime import date as calendar_date

from django.core.management.base import BaseCommand, CommandError

from content_analytics.services.report_scheduler import (
    generate_report_for_domain,
    run_daily_article_report_scheduler,
)


class Command(BaseCommand):
    help = (
        "Run the daily article-performance report scheduler tick, or generate one "
        "org's report directly with --domain (bypasses the kill switch and the "
        "local-hour gate)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", help="Generate for one organization domain.")
        parser.add_argument(
            "--date",
            help="Report date override (YYYY-MM-DD, org-local). Requires --domain.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Rebuild an existing snapshot in place. Requires --domain.",
        )

    def handle(self, *args, **options):
        domain = options.get("domain")
        date_raw = options.get("date")
        force = bool(options.get("force"))

        if (date_raw or force) and not domain:
            raise CommandError("--date and --force require --domain.")

        if domain:
            report_date = None
            if date_raw:
                try:
                    report_date = calendar_date.fromisoformat(str(date_raw))
                except ValueError as exc:
                    raise CommandError("--date must use YYYY-MM-DD format.") from exc
            result = generate_report_for_domain(domain, report_date=report_date, force=force)
        else:
            result = run_daily_article_report_scheduler()

        self.stdout.write(json.dumps(result, sort_keys=True))
        if result.get("status") == "failed" or result.get("failed"):
            raise CommandError("Article report generation reported failures.")
