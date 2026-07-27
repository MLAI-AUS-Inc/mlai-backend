import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.governance import DEFAULT_POLICY_PATH
from org_memory.pilot_readiness import (
    PilotApprovalError,
    build_pilot_readiness_report,
    load_pilot_approval_manifest,
)


class Command(BaseCommand):
    help = (
        "Build a read-only, content-free Admin Brain pilot-readiness report. "
        "This command never enables a feature or mutates rollout state."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--approval-manifest", required=True)
        parser.add_argument(
            "--governance-manifest",
            default=str(DEFAULT_POLICY_PATH),
        )
        parser.add_argument(
            "--environment",
            choices=("staging", "production"),
            default="production",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            help="Require the private query API to be enabled.",
        )
        parser.add_argument(
            "--fail-on-blockers",
            action="store_true",
            help="Exit non-zero after emitting the report when blockers exist.",
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(
                domain__iexact=options["organization_domain"]
            )
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        try:
            approval_manifest = load_pilot_approval_manifest(
                options["approval_manifest"]
            )
        except PilotApprovalError as exc:
            raise CommandError(str(exc)) from exc
        report = build_pilot_readiness_report(
            organization=organization,
            approval_manifest=approval_manifest,
            governance_manifest_path=options["governance_manifest"],
            environment=options["environment"],
            live=options["live"],
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_blockers"] and not report["ready"]:
            raise CommandError("Admin Brain pilot readiness has blockers.")
