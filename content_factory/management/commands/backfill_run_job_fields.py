"""Copy ContentFactoryJob facts into their runs' new columns (R7 job/run merge, 4.2 and 4.3)."""
from datetime import datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content_factory.run_job_backfill import DEFAULT_BATCH_SIZE, backfill_run_job_fields, undo_backfill


class Command(BaseCommand):
    help = (
        "Fill each ContentFactoryRun's Job columns from its ContentFactoryJob. Dry run by "
        "default; pass --apply to write. Never overwrites a non-empty run value (conflicts are "
        "counted by field) and never deletes a Job. With --create it also makes a tagged run for "
        "a finished Job that has none (never for a live one). --undo deletes the untouched runs "
        "it created and blanks the new columns. "
        "Output is field names and counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--dry-run", action="store_true", help="Report only. This is the default.")
        parser.add_argument("--undo", action="store_true", help="Reverse a backfill (design 4.3).")
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--create", action="store_true",
                            help="Also create runs for finished Jobs that have none (default: fill existing runs only).")
        parser.add_argument("--create-since", default="",
                            help="Create runs only for Jobs created on or after this date (YYYY-MM-DD).")

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("Pass either --apply or --dry-run, not both.")
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1.")
        apply = options["apply"]
        if options["undo"]:
            if apply and getattr(settings, "ARTICLES_RUN_JOB_READ_ENABLED", False) is True:
                raise CommandError("Turn ARTICLES_RUN_JOB_READ_ENABLED off before undoing: reads would see blank runs.")
            counts = undo_backfill(apply=apply, batch_size=options["batch_size"])
            self.stdout.write(f"Mode: {'apply' if apply else 'dry run (pass --apply to write)'} (undo)")
            for key in ("created_runs_deleted", "created_runs_kept_updated", "runs_blanked", "articles_unlinked"):
                self.stdout.write(f"{key}: {counts[key]}")
            if getattr(settings, "ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED", False) is True:
                self.stdout.write("Note: dual write is on, so the next Job write refills its run.")
            return
        create_since = None
        if options["create_since"]:
            try:
                day = datetime.strptime(options["create_since"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--create-since must be YYYY-MM-DD.") from exc
            create_since = timezone.make_aware(datetime.combine(day, time.min), timezone.get_default_timezone())
        report = backfill_run_job_fields(apply=apply, batch_size=options["batch_size"],
                                         create=options["create"], create_since=create_since)
        for line in report.lines():
            self.stdout.write(line)
