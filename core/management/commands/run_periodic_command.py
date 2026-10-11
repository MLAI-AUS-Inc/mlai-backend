"""Run a supported scheduler repeatedly within one Django process."""

import logging
from pathlib import Path
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run discovery or memory scheduling without rebooting Django each tick."

    def add_arguments(self, parser):
        parser.add_argument(
            "command_name", choices=("run_scheduled_discovery", "schedule_memory_work")
        )
        parser.add_argument("--poll-seconds", type=int, default=60)
        parser.add_argument("--health-file")
        parser.add_argument("--continue-on-error", action="store_true")

    def handle(self, *args, **options):
        interval = options["poll_seconds"]
        if interval <= 0:
            raise CommandError("--poll-seconds must be greater than zero")
        marker = Path(options["health_file"]) if options.get("health_file") else None
        if marker:
            marker.unlink(missing_ok=True)
        while True:
            close_old_connections()
            try:
                call_command(options["command_name"], stdout=self.stdout, stderr=self.stderr)
            except Exception:
                if marker:
                    marker.unlink(missing_ok=True)
                if not options["continue_on_error"]:
                    raise
                logger.exception("Scheduler tick failed; the next bounded poll will retry")
            else:
                if marker:
                    marker.touch()
            finally:
                close_old_connections()
            time.sleep(interval)
