"""Local website consistency inventory; repairs require an exact connection."""
import json
from django.core.management.base import BaseCommand, CommandError
from content_factory.website_consistency import report_consistency


class Command(BaseCommand):
    """Report by default and allow separately authorized, bounded repair batches."""
    help = "Read-only consistency inventory by default; --apply requires --connection-id."

    def add_arguments(self, parser):
        """Bound the inventory and make apply scope explicit."""
        parser.add_argument("--connection-id")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        """Never connect to GitHub or publish, merge, notify, or refund."""
        try:
            result = report_consistency(connection_id=options["connection_id"], limit=options["limit"], apply=options["apply"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
