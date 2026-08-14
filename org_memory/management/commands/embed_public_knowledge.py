import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.embeddings import configured_embedding_target
from org_memory.models import PublicKnowledgeItem, PublicKnowledgeStatus
from org_memory.public_knowledge import (
    PublicKnowledgeError,
    embed_public_knowledge_item,
)


class Command(BaseCommand):
    help = "Embed active public knowledge items that have no current vector."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Organization UUID or exact domain.")
        parser.add_argument("--model")
        parser.add_argument("--version")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be embedded without calling the provider.",
        )

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive.")
        target = configured_embedding_target(
            model=options.get("model"),
            version=options.get("version"),
        )
        rows = PublicKnowledgeItem.objects.filter(status=PublicKnowledgeStatus.ACTIVE)
        selector = options.get("organization")
        if selector:
            matched = rows.filter(organization__domain__iexact=selector)
            if not matched.exists():
                try:
                    matched = rows.filter(organization_id=selector)
                except (TypeError, ValueError):
                    matched = rows.none()
            if not matched.exists():
                raise CommandError("Organization was not found.")
            rows = matched
        # Re-embed anything missing a vector or pinned to a superseded target,
        # so a model rollout converges by rerunning this command.
        pending = rows.exclude(
            embedding__isnull=False,
            embedding_model=target.model,
            embedding_version=target.version,
        ).order_by("published_at", "pk")[: options["limit"]]

        totals = {
            "model": target.model,
            "version": target.version,
            "embedded": 0,
            "failed": 0,
            "candidates": len(pending),
            "dry_run": bool(options["dry_run"]),
        }
        if options["dry_run"]:
            self.stdout.write(json.dumps(totals, sort_keys=True))
            return
        for item in pending:
            try:
                embed_public_knowledge_item(
                    item=item,
                    model=target.model,
                    version=target.version,
                )
            except (PublicKnowledgeError, RuntimeError, ValueError) as exc:
                totals["failed"] += 1
                self.stderr.write(f"{item.pk}: {exc}")
                continue
            totals["embedded"] += 1
        self.stdout.write(json.dumps(totals, sort_keys=True))
