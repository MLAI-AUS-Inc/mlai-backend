import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.pilot_deployment import pilot_access_matrix_report
from org_memory.pilot_readiness import (
    PilotApprovalError,
    load_pilot_approval_manifest,
)


class Command(BaseCommand):
    help = (
        "Fail unless the exact active Admin Brain pilot access matrix permits "
        "approved paths and denies representative unapproved paths. This "
        "command performs no writes and emits no raw allowlist references."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--approval-manifest", required=True)

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"].strip(),
        ).first()
        if organization is None:
            raise CommandError("Pilot organization does not exist.")
        try:
            approval_manifest = load_pilot_approval_manifest(
                options["approval_manifest"]
            )
        except PilotApprovalError as exc:
            raise CommandError(str(exc)) from exc

        report = pilot_access_matrix_report(
            organization=organization,
            approval_manifest=approval_manifest,
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["ready"]:
            raise CommandError("Admin Brain pilot access matrix has blockers.")
