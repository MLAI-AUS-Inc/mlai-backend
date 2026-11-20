"""Print the daily founder-health report and its SLO lines (plan F9). Read-only, always allowed.

The scheduler posts the same text to ops Slack only while
ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED is on; this command never posts.
Postgres runs the queries inside a READ ONLY transaction that is rolled back.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from content_factory.founder_health_report import ROW_LIMIT, build_report, render_text


class Command(BaseCommand):
    help = "Print the founder-health report (funnel, timings, setup and publish success, stops, staff fixing, SLOs)."

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS, help="Database alias to read.")
        parser.add_argument("--json", action="store_true", help="Print the report as one JSON object.")
        parser.add_argument("--row-limit", type=int, default=ROW_LIMIT, help="Rows read per section (at most 2000).")

    def handle(self, *args, **options):
        using = options["database"]
        limit = max(1, min(int(options["row_limit"]), ROW_LIMIT))
        with transaction.atomic(using=using):
            if connections[using].vendor == "postgresql":
                with connections[using].cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            report = build_report(using=using, row_limit=limit)
            transaction.set_rollback(True, using=using)
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
            return
        self.stdout.write(render_text(report))
