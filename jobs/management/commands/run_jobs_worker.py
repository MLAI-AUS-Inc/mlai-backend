import json
import logging
import time

from django.core.management.base import BaseCommand, CommandError

from jobs.services.job_pipeline import process_next_queued_run

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Execute queued Jobs runs independently of the shared scheduler."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=5.0)

    def handle(self, *args, **options):
        poll_seconds = options["poll_seconds"]
        if not 0 < poll_seconds <= 300:
            raise CommandError("--poll-seconds must be between 0 and 300.")
        while True:
            try:
                result = process_next_queued_run()
            except Exception:
                if options["once"]:
                    raise
                logger.exception("Jobs execution failed; the persisted run records its outcome.")
            else:
                if options["once"]:
                    self.stdout.write(json.dumps(result, default=str))
                    return
            time.sleep(poll_seconds)
