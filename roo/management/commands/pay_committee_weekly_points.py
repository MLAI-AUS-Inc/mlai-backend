import json

from django.conf import settings
from django.core.management.base import BaseCommand

from roo.committee_remuneration import CommitteeRemunerationService, post_slack_summary


class Command(BaseCommand):
    help = "Pay the weekly Roo points remuneration to active committee members."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--no-post", action="store_true", default=False)
        parser.add_argument("--channel", default=None, help="Override the Slack channel.")

    def handle(self, *args, **options):
        if not getattr(settings, "COMMITTEE_REMUNERATION_ENABLED", False):
            self.stdout.write(json.dumps({"status": "disabled"}))
            return

        summary = CommitteeRemunerationService.pay(dry_run=options["dry_run"])

        # Nothing new to announce: an hourly loop re-runs all week and the
        # ISO-week idempotency key makes every run after the first a no-op.
        if not options["no_post"] and summary["paid"]:
            posted, message_ts = post_slack_summary(summary, channel=options["channel"])
            summary["slack_posted"] = posted
            summary["slack_message_ts"] = message_ts

        self.stdout.write(json.dumps(summary))
