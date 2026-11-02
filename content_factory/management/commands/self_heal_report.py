"""Inspect prospective recovery actions without mutating rollout or run state."""
import json
from django.core.management.base import BaseCommand
from content_factory.failure_policy_engine import dry_run_report


class Command(BaseCommand):
    """Read-only report of pending legacy recovery decisions."""
    help = 'Show prospective self-heal actions for legacy startups, without changes.'

    def add_arguments(self, parser):
        parser.add_argument('--org')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        rows = dry_run_report(organization_id=options.get('org'))
        if options['as_json']:
            self.stdout.write(json.dumps(rows))
        else:
            for row in rows:
                self.stdout.write(f"Org {row['organization_id']} run {row['run_id']}: {row['code']} -> {row['action']} ({row['cap'] or 'no cap'})")
