"""Read-only, bounded preparation diagnostics for a selected company."""
import json
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.db import connection as database, transaction

from content_factory.activation import mapping
from content_factory.website_failure_redaction import redact_secret_text
from content_factory.website_discovery import discovery_snapshot

from content_factory.website_doctor import safe_identifier, safe_code, sha, stamp, number, read_doctor
from content_factory import website_goal as goals


def safe_route(value):
    """Only repository paths are useful here; prose and credential URLs are omitted."""
    return value if isinstance(value, str) and len(value) <= 255 and value.startswith("/") and not value.startswith("//") and not any(c in value for c in "?#\\\r\n") and redact_secret_text(value) == value else None


def inspection_report(connection, config, operations, snapshot, targets, *, scope=None, observed_sha=None):
    """Whitelist diagnostics; never print arbitrary receipt text, source or credentials."""
    pending = mapping(mapping(config.article_system).get("pending_article_system_setup"))
    evidence = mapping(snapshot.evidence) if snapshot else {}
    discovery = discovery_snapshot({**mapping(evidence.get("scan_summary")), **evidence})
    resolution = mapping(discovery.get("resolution"))
    reports = []
    for op in operations:
        receipt = mapping(op.receipt)
        reports.append({"id": safe_identifier(str(op.pk)), "action": safe_code(op.action), "state": safe_code(op.state),
            "attempts": number(op.attempts, integer=True), "next_attempt_at": stamp(op.next_attempt_at),
            "created_at": stamp(op.created_at), "updated_at": stamp(op.updated_at),
            "receipt": {"status": safe_code(receipt.get("status")), "current_step": safe_code(receipt.get("current_step")),
                "reason_code": safe_code(receipt.get("reason_code")), "child_run_id": safe_identifier(receipt.get("child_run_id"))},
            "doctor": read_doctor(op, scope=scope, source_sha=observed_sha) if scope else None})
    return {
        "connection": {"id": safe_identifier(str(connection.pk)), "generation": connection.generation, "state": safe_code(connection.state),
            "repository_id": safe_identifier(str(connection.repository_id)), "installation_id": safe_identifier(str(connection.installation_id)),
            "verified_sha": sha(connection.verified_sha), "observed_sha": sha(observed_sha),
            "blocker_codes": [code for item in (connection.blockers if isinstance(connection.blockers, list) else [])[:32]
                if (code := safe_code(mapping(item).get("code") if isinstance(item, dict) else item))],
            "capabilities": sorted(key for key, value in mapping(connection.capabilities).items() if value is True and safe_code(key))[:32]},
        "operations": reports,
        "pending_article_system_setup": {"mode": safe_code(pending.get("mode")), "route": safe_route(pending.get("route_path")),
            "scan_run": safe_identifier(pending.get("source_scan_run_id") or pending.get("sourceScanRunId")),
            "setup_run": safe_identifier(pending.get("setup_run_id") or pending.get("setupRunId") or pending.get("run_id"))},
        "inventory": {"run_id": safe_identifier(snapshot.run_id), "source_sha": sha(snapshot.source_sha),
            "existence_state": safe_code(resolution.get("existence_state")),
            "listing_surface": {"route": safe_route(mapping(resolution.get("listing_surface")).get("route"))}} if snapshot else None,
        "targets": [{"key": safe_identifier(target.target_key), "adapter": safe_code(target.adapter), "source_sha": sha(target.source_sha),
            "verified_at": stamp(target.verified_at)} for target in targets],
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
                | Q(evidence__scan_summary__repository_inventory__discovery_complete=True)
                | Q(evidence__scan_summary__repository_discovery__discovery_complete=True) | Q(evidence__scan_complete=True))
            observed = website.scan_snapshots.filter(generation=website.generation).order_by("-created_at", "-pk").first()
            report = inspection_report(website, config, website.operations.select_related("connection").order_by("-created_at")[:5],
                snapshots.order_by("-created_at").first(), website.targets.filter(generation=website.generation)[:32],
                scope=goals.scope_for(website, company.pk), observed_sha=observed.source_sha if observed else None)
            if options["journey"]:
                context = SimpleNamespace(company=company, organization=company.organization, user=company.profile.user,
                    profile=company.profile)
                journey = journey_for_context(context, config)
                report["journey"] = {"nextAction": safe_code(mapping(journey.get("nextAction")).get("id")),
                    "prerequisites": {safe_code(key): safe_code(mapping(value).get("status")) for key, value in mapping(journey.get("prerequisites")).items() if safe_code(key)},
                    "allowedActions": [code for item in journey.get("allowedActions", [])[:32] if (code := safe_code(mapping(item).get("id") if isinstance(item, dict) else item))]}
            self.stdout.write(json.dumps(report, default=str, sort_keys=True, indent=2))
