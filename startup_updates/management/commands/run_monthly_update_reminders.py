import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from startup_updates.monthly_update_reminders import run_monthly_update_reminder_scheduler


class Command(BaseCommand):
    help = "Preview monthly-update reminders, or send them when explicitly requested."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Local reminder date in YYYY-MM-DD format (defaults to today).")
        parser.add_argument(
            "--send",
            action="store_true",
            help="Actually enqueue Customer.io messages. The default is a no-write dry run.",
        )

    def handle(self, *args, **options):
        timezone_name = str(getattr(settings, "MONTHLY_UPDATE_REMINDER_TIMEZONE", "Australia/Melbourne"))
        local_zone = ZoneInfo(timezone_name)
        target_date = datetime.now(local_zone).date()
        if options.get("date"):
            try:
                target_date = date.fromisoformat(str(options["date"]))
            except ValueError as exc:
                raise CommandError("--date must use YYYY-MM-DD format.") from exc

        scheduled_hour = int(getattr(settings, "MONTHLY_UPDATE_REMINDER_HOUR", 9))
        scheduled_minute = int(getattr(settings, "MONTHLY_UPDATE_REMINDER_MINUTE", 0))
        run_at = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            scheduled_hour,
            scheduled_minute,
            tzinfo=local_zone,
        )
        result = run_monthly_update_reminder_scheduler(now=run_at, dry_run=not bool(options.get("send")))
        self.stdout.write(json.dumps(result, sort_keys=True))
        if result.get("status") == "failed":
            raise CommandError(str(result.get("reason") or "Monthly-update reminder run failed."))
