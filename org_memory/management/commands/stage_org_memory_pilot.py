import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Organization
from org_memory.governance import DEFAULT_POLICY_PATH
from org_memory.pilot_deployment import (
    PilotDeploymentError,
    pilot_deployment_result,
    resolve_pilot_operator,
    stage_pilot_deployment,
)
from org_memory.pilot_readiness import (
    PilotApprovalError,
    build_pilot_readiness_report,
    load_pilot_approval_manifest,
)


class Command(BaseCommand):
    help = (
        "Stage an approval-bound Admin Roo pilot runtime allowlist. "
        "Dry-run by default; this never changes feature flags."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--approval-manifest", required=True)
        parser.add_argument(
            "--governance-manifest",
            default=str(DEFAULT_POLICY_PATH),
        )
        parser.add_argument("--operator-email", required=True)
        parser.add_argument("--idempotency-key", required=True)
        parser.add_argument(
            "--environment",
            choices=("staging", "production"),
            default="production",
        )
        parser.add_argument("--apply", action="store_true")

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
            operator = resolve_pilot_operator(
                organization,
                options["operator_email"],
            )
        except (PilotApprovalError, PilotDeploymentError) as exc:
            raise CommandError(str(exc)) from exc

        readiness = build_pilot_readiness_report(
            organization=organization,
            approval_manifest=approval_manifest,
            governance_manifest_path=options["governance_manifest"],
            environment=options["environment"],
            live=False,
            allow_runtime_staging=True,
        )
        if not readiness["ready"]:
            self.stdout.write(
                json.dumps(
                    {
                        "applied": False,
                        "readiness": readiness,
                    },
                    sort_keys=True,
                )
            )
            raise CommandError("Admin Brain pilot staging readiness has blockers.")
        try:
            with transaction.atomic():
                deployment, created = stage_pilot_deployment(
                    organization=organization,
                    approval_manifest=approval_manifest,
                    readiness_report=readiness,
                    operator=operator,
                    idempotency_key=options["idempotency_key"],
                )
                result = pilot_deployment_result(
                    deployment,
                    changed=created,
                    action="stage",
                )
                if not options["apply"]:
                    transaction.set_rollback(True)
        except PilotDeploymentError as exc:
            raise CommandError(str(exc)) from exc
        result["applied"] = bool(options["apply"])
        result["readiness_hash"] = readiness["approval_manifest_hash"]
        self.stdout.write(json.dumps(result, sort_keys=True))
