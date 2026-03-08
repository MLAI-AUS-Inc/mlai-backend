from django.core.management.base import BaseCommand

from core.article_system import (
    default_article_system,
    normalize_article_system,
    resolve_article_system_with_source,
)
from core.models import OrganizationContentConfig


class Command(BaseCommand):
    help = "Backfill canonical article_system from historical scan_summary or scaffold metadata."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist backfilled article_system values to the database.",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        counts = {
            "existing": 0,
            "ambiguous": 0,
            "roo_scaffolded": 0,
            "unchanged": 0,
            "skipped": 0,
        }

        self.stdout.write(f"Starting article_system backfill (commit={commit})")

        for config in OrganizationContentConfig.objects.select_related("organization").all():
            current = normalize_article_system(getattr(config, "article_system", None) or {})
            resolved, source = resolve_article_system_with_source(config)

            if source not in {"scan_summary_fallback", "scaffold_flag"}:
                counts["skipped"] += 1
                continue

            if current != default_article_system():
                counts["unchanged"] += 1
                continue

            if commit:
                config.article_system = resolved
                config.save(update_fields=["article_system", "updated_at"])

            counts[resolved["state"]] = counts.get(resolved["state"], 0) + 1
            domain = getattr(getattr(config, "organization", None), "domain", "<unknown>")
            self.stdout.write(f"{domain}: {source} -> {resolved['state']}")

        summary = ", ".join(f"{key}={value}" for key, value in counts.items())
        self.stdout.write(self.style.SUCCESS(f"Article-system backfill complete: {summary}"))
