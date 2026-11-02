"""Preview a bounded nightly cohort without invoking the mutating sweep."""
import json

from django.core.management.base import BaseCommand, CommandError
from content_factory.nightly_refresh_reconciliation import preview_nightly_refresh_reconciliation


class Command(BaseCommand):
    help = "Read-only, aggregate-only preview of new nightly refresh candidates."

    def add_arguments(self, parser):
        parser.add_argument("--created-since", required=True, help="Aware ISO creation boundary.")
        parser.add_argument("--limit", type=int, default=10, help="Maximum inspected candidates (1–100).")

    def handle(self, *args, **options):
        try:
            result = preview_nightly_refresh_reconciliation(created_since=options["created_since"], limit=options["limit"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
