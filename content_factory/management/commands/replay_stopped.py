"""Re-open website preparations stopped on a fixed code, through the owner's Try again (A16).

Dry-run by default: prints which operations would re-open. Pass ``--apply`` to replay.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from content_factory.stopped_replay import ReplayInputError, replay_stopped


class Command(BaseCommand):
    """Replay current-generation, connected prepare operations stopped on ``--code``."""

    help = ("Re-open current-generation, connected website preparations stopped on --code through the owner's "
            "Try again path, within the setup and pull request caps. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True)
        parser.add_argument("--since", help="Only operations stopped (updated) at or after this ISO time with zone.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", default=True, help="Report only (the default).")
        mode.add_argument("--apply", action="store_true", help="Replay the eligible operations.")
        parser.add_argument("--org", type=int, dest="organization_id")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        since = None
        if options.get("since"):
            since = parse_datetime(options["since"])
            if since is None:
                raise CommandError("--since must be an ISO date-time with a time zone.")
        try:
            report = replay_stopped(stop_code=options["code"], since=since, apply=bool(options["apply"]),
                                    organization_id=options.get("organization_id"), limit=options["limit"])
        except ReplayInputError as exc:
            raise CommandError(str(exc)) from exc
        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        for row in report["rows"]:
            self.stdout.write(f"Org {row['organization_id']} operation {row['operation_id']} ({row['state']}, "
                              f"generation {row['generation']}): {row['outcome']}")
        skipped = ", ".join(f"{key} {value}" for key, value in sorted(report["skipped"].items())) or "none"
        self.stdout.write(f"{report['mode']} {report['replay_key']}: checked {report['checked']}, eligible "
                          f"{report['eligible']}, replayed {report['replayed']}; skipped {skipped}.")
