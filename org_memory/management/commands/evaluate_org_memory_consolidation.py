import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.evals import evaluate_consolidation_seed_suite


class Command(BaseCommand):
    help = "Run the offline organisational-memory consolidation and temporal safety seed suite."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", help="Optional path to a consolidation seed-suite JSON fixture.")

    def handle(self, *args, **options):
        result = evaluate_consolidation_seed_suite(options.get("fixture"))
        self.stdout.write(json.dumps(result, sort_keys=True))
        if not result["ok"]:
            raise CommandError("Organisational-memory consolidation seed evals failed.")
