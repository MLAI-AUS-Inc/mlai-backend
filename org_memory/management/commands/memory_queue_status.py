import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.runtime import memory_queue_snapshot


class Command(BaseCommand):
    help = "Report organisational-memory queue, lease, dead-letter, and throttle metrics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail-on-degraded",
            action="store_true",
            help="Exit non-zero when dead work, failed outbox events, or expired leases exist.",
        )

    def handle(self, *args, **options):
        result = memory_queue_snapshot()
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
        if options["fail_on_degraded"] and any(
            result[key] for key in ("dead", "failed_outbox", "expired_leases")
        ):
            raise CommandError("Organisational-memory runtime is degraded.")
