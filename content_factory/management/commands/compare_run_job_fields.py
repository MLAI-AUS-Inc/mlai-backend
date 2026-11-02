"""Count per-field mismatches between ContentFactoryJob rows and their runs (R7 parity gate)."""
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content_factory.run_job_backfill import DEFAULT_BATCH_SIZE, compare_run_job_fields


class Command(BaseCommand):
    help = (
        "Read-only. Compare every ContentFactoryJob with the run columns that mirror it and "
        "print per-field mismatch counts (no ids or values). The parity gate before switching "
        "reads to the run: zero mismatches for Jobs written after dual write was turned on."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--updated-since", default="",
                            help="Compare only Jobs updated on or after this date (YYYY-MM-DD).")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1.")
        updated_since = None
        if options["updated_since"]:
            try:
                day = datetime.strptime(options["updated_since"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--updated-since must be YYYY-MM-DD.") from exc
            updated_since = timezone.make_aware(datetime.combine(day, time.min), timezone.get_default_timezone())
        report = compare_run_job_fields(batch_size=options["batch_size"], updated_since=updated_since)
        for line in report.lines():
            self.stdout.write(line)
