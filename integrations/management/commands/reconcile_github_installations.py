import json

from django.core.management.base import BaseCommand

from integrations.services.github_installations import (
    run_github_installation_reconciliation_sweep,
)


class Command(BaseCommand):
    help = (
        "Probe founder GitHub App installations against GitHub and prune the ones "
        "that are gone (uninstalled), so stale rows stop poisoning the 'registry "
        "exists' guards. The scheduler loop runs this automatically; the command "
        "exists for manual/ops use. Pass --limit to override the per-pass batch cap."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max installations to probe this pass (default: batch-limit setting).",
        )

    def handle(self, *args, **options):
        result = run_github_installation_reconciliation_sweep(limit=options.get("limit"))
        self.stdout.write(json.dumps(result, sort_keys=True))
