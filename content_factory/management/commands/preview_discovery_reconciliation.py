"""Read-only candidate counts before admitting discovery reconciliation."""
import json

from django.core.management.base import BaseCommand, CommandError

from content_factory.reconciliation import preview_discovery_reconciliation


class Command(BaseCommand):
    help = "Preview stale discovery candidates from an explicit creation boundary; no providers or repairs are invoked."

    def add_arguments(self, parser):
        parser.add_argument("--created-since", required=True, help="Aware ISO timestamp, e.g. 2026-11-02T12:00:00Z.")
        parser.add_argument("--limit", type=int, default=10, help="Maximum selected candidates (1–100).")

    def handle(self, *args, **options):
        try:
            result = preview_discovery_reconciliation(created_since=options["created_since"], limit=options["limit"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
