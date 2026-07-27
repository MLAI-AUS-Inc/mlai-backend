import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.pilot_evidence import (
    PilotEvidenceError,
    build_pilot_evidence_report,
    load_pilot_exit_policy,
)
from org_memory.pilot_readiness import (
    PilotApprovalError,
    load_pilot_approval_manifest,
)


class Command(BaseCommand):
    help = (
        "Build a read-only, content-free exit-gate report for a completed "
        "Admin Brain pilot window."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--approval-manifest", required=True)
        parser.add_argument("--exit-policy", required=True)
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
            exit_policy = load_pilot_exit_policy(options["exit_policy"])
        except (PilotApprovalError, PilotEvidenceError) as exc:
            raise CommandError(str(exc)) from exc
        report = build_pilot_evidence_report(
            organization=organization,
            approval_manifest=approval_manifest,
            exit_policy=exit_policy,
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_blockers"] and not report["ready_to_exit"]:
            raise CommandError("Admin Brain pilot exit gates have blockers.")
