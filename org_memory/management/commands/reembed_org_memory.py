import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.embeddings import schedule_chunk_embeddings


class Command(BaseCommand):
    help = "Queue a version-pinned embedding rebuild for eligible memory chunks."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--organization", help="Organization UUID or exact domain.")
        scope.add_argument("--all", action="store_true", help="Queue every organization.")
        parser.add_argument("--model")
        parser.add_argument("--version")
        parser.add_argument("--dimensions", type=int)
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive.")
        organizations = Organization.objects.order_by("pk")
        selector = options.get("organization")
        if selector:
            organizations = organizations.filter(domain=selector)
            if not organizations.exists():
                try:
                    organizations = Organization.objects.filter(pk=selector)
                except (TypeError, ValueError):
                    organizations = Organization.objects.none()
            if not organizations.exists():
                raise CommandError("Organization was not found.")

        totals = {"scheduled": 0, "existing": 0, "organizations": 0}
        remaining = options["limit"]
        for organization in organizations:
            if remaining < 1:
                break
            result = schedule_chunk_embeddings(
                organization=organization,
                model=options.get("model"),
                version=options.get("version"),
                dimensions=options.get("dimensions"),
                limit=remaining,
            )
            totals["scheduled"] += result["scheduled"]
            totals["existing"] += result["existing"]
            totals["organizations"] += 1
            remaining -= result["scheduled"]
            totals.update(
                model=result["model"],
                version=result["version"],
                dimensions=result["dimensions"],
            )
        self.stdout.write(json.dumps(totals, sort_keys=True))
