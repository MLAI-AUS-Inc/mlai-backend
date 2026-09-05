from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.slack_founder_links import purge_stale_slack_founder_link_requests


class Command(BaseCommand):
    help = "Delete terminal Roo-Founder Tools link requests past retention"

    def add_arguments(self, parser):
        parser.add_argument(
            "--retention-days",
            type=int,
            default=settings.ROO_FOUNDER_LINK_REQUEST_RETENTION_DAYS,
        )

    def handle(self, *args, **options):
        retention_days = options["retention_days"]
        if retention_days < 1:
            raise CommandError("--retention-days must be at least 1")
        deleted = purge_stale_slack_founder_link_requests(
            retention_days=retention_days,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} stale Slack-Founder link request(s)."
            )
        )
