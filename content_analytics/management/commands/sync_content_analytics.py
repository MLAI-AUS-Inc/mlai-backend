import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_analytics.models import AnalyticsSyncSource
from content_analytics.services.sync import sync_due_analytics, sync_organization_analytics
from organizations.models import Organization


class Command(BaseCommand):
    help = "Synchronize daily Content Factory article aggregates from Umami and Search Console."

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=["all", AnalyticsSyncSource.UMAMI, AnalyticsSyncSource.SEARCH_CONSOLE], default="all")
        parser.add_argument("--organization-id", type=int)
        parser.add_argument("--domain")
        parser.add_argument("--days", type=int)
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        if not getattr(settings, "CONTENT_ANALYTICS_SYNC_ENABLED", True):
            self.stdout.write(json.dumps({"status": "disabled"}))
            return
        organization_id = options.get("organization_id")
        domain = str(options.get("domain") or "").strip()
        source = options["source"]
        days = options.get("days")
        if days is not None and days < 1:
            raise CommandError("--days must be positive.")
        if organization_id or domain:
            lookup = {"id": organization_id} if organization_id else {"domain": domain}
            try:
                organization = Organization.objects.get(**lookup)
            except Organization.DoesNotExist as exc:
                raise CommandError("Organization not found.") from exc
            sources = (
                [AnalyticsSyncSource.UMAMI, AnalyticsSyncSource.SEARCH_CONSOLE]
                if source == "all"
                else [source]
            )
            results = []
            failures = []
            for source_name in sources:
                try:
                    results.append(
                        sync_organization_analytics(
                            organization,
                            source=source_name,
                            days=days,
                            force=bool(options["force"]),
                        )
                    )
                except Exception as exc:
                    failures.append({"source": source_name, "error": str(exc)})
            payload = {"status": "partial" if failures else "succeeded", "results": results, "failures": failures}
        else:
            payload = sync_due_analytics(
                source="" if source == "all" else source,
                limit=max(1, int(options["limit"])),
                days=days,
                force=bool(options["force"]),
            )
        self.stdout.write(json.dumps(payload, default=str, sort_keys=True))
        if payload.get("status") in {"failed", "partial"}:
            raise CommandError("Content analytics synchronization had one or more failures.")
