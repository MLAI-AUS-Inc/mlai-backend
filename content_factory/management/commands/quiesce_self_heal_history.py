"""Stamp background and pre-horizon failures so self-heal never sweeps them (A16).

Dry-run by default: prints what would change. Pass ``--apply`` to write.
"""
import json
from datetime import date, datetime, time, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError

from content_factory.self_heal_quiesce import quiesce_self_heal_history


class Command(BaseCommand):
    """Quiesce self-heal history: stamp scheduler runs, restore recoded failure codes."""

    help = ("Stamp scheduler runs and untouched pre-horizon failures so self-heal never sweeps them, and restore "
            "the original failure code the engine recoded on scheduler runs. Dry-run unless --apply.")

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", default=True, help="Report only (the default).")
        mode.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument("--before", help="History horizon as YYYY-MM-DD (default: ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE).")
        parser.add_argument("--org", type=int, dest="organization_id")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        before = None
        if options.get("before"):
            try:
                before = datetime.combine(date.fromisoformat(options["before"]), time.min, tzinfo=dt_timezone.utc)
            except ValueError as exc:
                raise CommandError("--before must be YYYY-MM-DD.") from exc
        if options.get("limit") is not None and options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")
        report = quiesce_self_heal_history(apply=bool(options["apply"]), before=before,
                                           organization_id=options.get("organization_id"), limit=options.get("limit"))
        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        for row in report["rows"]:
            restore = row["restore"]
            self.stdout.write(
                f"Org {row['organization_id']} run {row['run_id']} ({row['workflow']}, {row['audience']}): "
                + ("stamp" if row["stamp"] else "keep stamp")
                + (f"; restore {restore['from']} -> {restore['to']}" if restore else "")
                + (f"; cannot restore {row['unrestorable']}" if row["unrestorable"] else ""))
        self.stdout.write(
            f"{report['mode']}: checked {report['checked']}, stamped {report['stamped']}, restored {report['restored']}, "
            f"unrestorable {report['unrestorable']}, pending scheduler receipts {report['pending_scheduler_receipts']}.")
