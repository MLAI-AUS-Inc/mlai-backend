"""Read-only, bounded preparation diagnostics for a selected company."""
import json
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.db import connection as database, transaction

from content_factory.activation import mapping
from content_factory.website_discovery import discovery_snapshot

RECEIPT_FIELDS = ("status", "current_step", "reason_code", "message", "step_failures", "completed_steps", "child_run_id", "user_action")


def inspection_report(connection, config, operations, snapshot, targets):
    """Format selected diagnostic fields, excluding credentials and source bodies."""
    pending = mapping(mapping(config.article_system).get("pending_article_system_setup"))
    evidence = mapping(snapshot.evidence) if snapshot else {}
    discovery = discovery_snapshot({**mapping(evidence.get("scan_summary")), **evidence})
    resolution = mapping(discovery.get("resolution"))
    return {
        "connection": {"id": str(connection.pk), "generation": connection.generation, "state": connection.state,
            "repo": connection.github_repo, "repository_id": connection.repository_id, "branch": connection.branch,
            "app_root": connection.app_root, "installation_id": connection.installation_id, "verified_sha": connection.verified_sha,
            "blocker_codes": [item.get("code") if isinstance(item, dict) else item for item in connection.blockers],
            "capabilities": sorted(key for key, value in connection.capabilities.items() if value is True)},
        "operations": [{"id": str(op.pk), "action": op.action, "state": op.state, "attempts": op.attempts,
            "next_attempt_at": op.next_attempt_at, "created_at": op.created_at, "updated_at": op.updated_at,
            "receipt": {key: mapping(op.receipt).get(key) for key in RECEIPT_FIELDS}} for op in operations],
        "pending_article_system_setup": {"mode": pending.get("mode"), "route": pending.get("route_path"),
            "scan_run": pending.get("source_scan_run_id"), "setup_run": pending.get("run_id")},
        "inventory": {"run_id": snapshot.run_id, "source_sha": snapshot.source_sha,
            "existence_state": resolution.get("existence_state"), "listing_surface": resolution.get("listing_surface")} if snapshot else None,
        "targets": [{"key": target.target_key, "adapter": target.adapter, "source_sha": target.source_sha,
            "verified_at": target.verified_at} for target in targets],
    }


class Command(BaseCommand):
    """Inspect receipts inside a database-enforced read-only transaction."""

    help = "Read-only website preparation diagnostics; --journey adds read-only GitHub checks."

    def add_arguments(self, parser):
        """Require an exact company UUID or domain."""
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--company-id", type=str)
        scope.add_argument("--domain", type=str)
        parser.add_argument("--journey", action="store_true")

    def handle(self, *args, **options):
        """Read bounded local evidence; only journey checks may call GitHub."""
        from founder_tools.models import VibeRaisingCompany
        from content_factory.models import OrganizationContentConfig
        from content_factory.website_journey import journey_for_context
        from django.db.models import Q
        if database.vendor != "postgresql":
            raise CommandError("This command requires PostgreSQL SET TRANSACTION READ ONLY.")
        with transaction.atomic():
            with database.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            companies = VibeRaisingCompany.objects.select_related("organization", "profile__user")
            company = companies.filter(pk=options["company_id"]).first() if options.get("company_id") else companies.filter(
                organization__domain=str(options["domain"]).strip().lower()).first()
            if company is None:
                raise CommandError("Company not found.")
            config = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=company.organization).first()
            if config is None or config.website_connection is None:
                raise CommandError("Company has no website connection.")
            website = config.website_connection
            snapshots = website.scan_snapshots.filter(generation=website.generation).exclude(detector_version="github_head").filter(
                Q(evidence__repository_inventory__discovery_complete=True) | Q(evidence__repository_discovery__discovery_complete=True)
                | Q(evidence__scan_summary__repository_inventory__discovery_complete=True) | Q(evidence__scan_complete=True))
            report = inspection_report(website, config, website.operations.order_by("-created_at")[:5],
                snapshots.order_by("-created_at").first(), website.targets.filter(generation=website.generation))
            if options["journey"]:
                context = SimpleNamespace(company=company, organization=company.organization, user=company.profile.user,
                    profile=company.profile)
                journey = journey_for_context(context, config)
                report["journey"] = {"nextAction": journey.get("nextAction"), "prerequisites": journey.get("prerequisites"),
                    "allowedActions": journey.get("allowedActions")}
            self.stdout.write(json.dumps(report, default=str, sort_keys=True, indent=2))
