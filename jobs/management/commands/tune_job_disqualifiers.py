from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from jobs.services.feedback import prune_old_feedback, update_disqualifier_candidates


class Command(BaseCommand):
    help = "Promote repeated jobs feedback into active disqualifier candidates."

    def add_arguments(self, parser):
        parser.add_argument("--min-signals", type=int, default=3)
        parser.add_argument("--retention-days", type=int, default=90)
        parser.add_argument("--skip-prune", action="store_true")

    def handle(self, *args, **options):
        result = update_disqualifier_candidates(min_signals=max(1, int(options["min_signals"])))
        result["pruned_feedback"] = 0
        if not options["skip_prune"]:
            result["pruned_feedback"] = prune_old_feedback(retention_days=max(1, int(options["retention_days"])))
        self.stdout.write(json.dumps(result, sort_keys=True))
