from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from jobs.services.job_pipeline import create_run, run_daily_jobs


class Command(BaseCommand):
    help = "Run the Roo jobs daily scraper pipeline."

    def add_arguments(self, parser):
        parser.add_argument("--collect-live", action="store_true", default=True)
        parser.add_argument("--post-to-slack", action="store_true", default=False)
        parser.add_argument("--post-to-notion", action="store_true", default=True)
        parser.add_argument("--max-pages", type=int, default=None)
        parser.add_argument("--per-keyword-limit", type=int, default=None)

    def handle(self, *args, **options):
        run = create_run()
        run_daily_jobs(
            run.run_id,
            collect_live=bool(options["collect_live"]),
            post_to_slack=bool(options["post_to_slack"]),
            post_to_notion=bool(options["post_to_notion"]),
            max_pages=options["max_pages"],
            per_keyword_limit=options["per_keyword_limit"],
        )
        self.stdout.write(json.dumps({"run_id": run.run_id, "status": "completed"}))
