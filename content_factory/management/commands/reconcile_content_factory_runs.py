import json

from django.core.management.base import BaseCommand

from content_factory.reconciliation import run_content_factory_reconciliation_sweep


class Command(BaseCommand):
    help = (
        "Reconcile local content-factory runs stuck in an active status: adopt the "
        "remote state, or fail runs content-factory has no record of. The scheduler "
        "loop runs this automatically; the command exists for manual/ops use."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Max runs to probe this pass.")

    def handle(self, *args, **options):
        result = run_content_factory_reconciliation_sweep(limit=options.get("limit"))
        self.stdout.write(json.dumps(result, sort_keys=True))
