import json
from datetime import date as calendar_date

from django.core.management.base import BaseCommand, CommandError

from integrations.services.daily_discovery import (
    enqueue_scheduled_discovery,
    run_daily_discovery_scheduler,
)


class Command(BaseCommand):
    help = "Run the scheduled daily discovery selector or enqueue one specific replay target."

    def add_arguments(self, parser):
        parser.add_argument("--domain", help="Specific domain to enqueue.")
        parser.add_argument("--slack-user-id", help="Specific Slack user ID to enqueue.")
        parser.add_argument(
            "--local-date",
            help="Override target local date using YYYY-MM-DD.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replay an existing terminal dispatch for the same user/domain/local date.",
        )

    def handle(self, *args, **options):
        domain = options.get("domain")
        slack_user_id = options.get("slack_user_id")
        local_date_raw = options.get("local_date")
        force = bool(options.get("force"))

        if bool(domain) != bool(slack_user_id):
            raise CommandError("--domain and --slack-user-id must be provided together.")

        local_date = None
        if local_date_raw:
            try:
                local_date = calendar_date.fromisoformat(str(local_date_raw))
            except ValueError as exc:
                raise CommandError("--local-date must use YYYY-MM-DD format.") from exc

        if domain and slack_user_id:
            result = enqueue_scheduled_discovery(
                domain=domain,
                slack_user_id=slack_user_id,
                local_date=local_date,
                force=force,
            )
            self.stdout.write(json.dumps(result, sort_keys=True))
            if result.get("status") == "failed":
                raise CommandError(result.get("error") or "Scheduled discovery enqueue failed.")
            return

        result = run_daily_discovery_scheduler()
        self.stdout.write(json.dumps(result, sort_keys=True))
