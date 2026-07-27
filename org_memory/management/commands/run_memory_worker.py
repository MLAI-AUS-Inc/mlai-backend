import json
import time

from django.core.management.base import BaseCommand, CommandError

from org_memory.runtime import (
    MemoryRuntimeError,
    default_worker_id,
    run_memory_worker_once,
)


class Command(BaseCommand):
    help = "Continuously claim and execute durable organisational-memory work."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one bounded poll.")
        parser.add_argument(
            "--max-items",
            type=int,
            default=0,
            help="Stop after this many claimed items; zero means no item limit.",
        )
        parser.add_argument("--worker-id", default="", help="Stable operator-visible worker ID.")
        parser.add_argument("--lease-seconds", type=int, default=None)
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        worker_id = str(options["worker_id"] or default_worker_id())[:255]
        max_items = int(options["max_items"] or 0)
        poll_seconds = float(options["poll_seconds"])
        if max_items < 0 or poll_seconds <= 0:
            raise CommandError("--max-items cannot be negative and --poll-seconds must be positive.")
        processed = 0
        try:
            while True:
                result = run_memory_worker_once(
                    worker_id=worker_id,
                    lease_seconds=options["lease_seconds"],
                )
                self.stdout.write(json.dumps(result, sort_keys=True, default=str))
                if result["status"] != "idle":
                    processed += 1
                if options["once"] or (max_items and processed >= max_items):
                    break
                if result["status"] == "idle":
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Memory worker stopped."))
        except MemoryRuntimeError as exc:
            raise CommandError(str(exc)) from exc
