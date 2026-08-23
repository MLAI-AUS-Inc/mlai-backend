from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from content_factory.services.island_refresh_scheduler import refresh_islands_for_domain


class Command(BaseCommand):
    help = (
        "Queue a content-factory island refresh for one organization. Bypasses "
        "CONTENT_ISLANDS_SCHEDULER_ENABLED and the org-local hour gate; --force "
        "re-dispatches even when today's dispatch row already exists."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Organization domain (e.g. mlai.au).")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-dispatch even if this org already has a dispatch for today.",
        )
        parser.add_argument(
            "--no-expansion",
            action="store_true",
            help="Skip the DataForSEO keyword-expansion step content-factory runs for daily refreshes.",
        )

    def handle(self, *args, **options):
        result = refresh_islands_for_domain(
            options["domain"],
            include_expansion=not options["no_expansion"],
            force=bool(options["force"]),
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
        if result.get("status") == "failed":
            raise CommandError(result.get("error") or "Island refresh dispatch failed.")
