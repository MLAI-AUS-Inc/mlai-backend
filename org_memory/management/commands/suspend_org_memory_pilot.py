import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Organization
from org_memory.models import MemoryPilotSuspensionReason
from org_memory.pilot_deployment import (
    PilotDeploymentError,
    resolve_pilot_operator,
    suspend_pilot_deployments,
)


class Command(BaseCommand):
    help = (
        "Suspend every staged or active Admin Roo pilot deployment for one "
        "organization. Dry-run by default; use the query flag for immediate "
        "global shutdown."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--operator-email", required=True)
        parser.add_argument(
            "--reason",
            choices=tuple(
                value
                for value in MemoryPilotSuspensionReason.values
                if value != MemoryPilotSuspensionReason.SUPERSEDED
            ),
            required=True,
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
            operator = resolve_pilot_operator(
                organization,
                options["operator_email"],
            )
            with transaction.atomic():
                changed = suspend_pilot_deployments(
                    organization=organization,
                    operator=operator,
                    reason=options["reason"],
                )
                if not options["apply"]:
                    transaction.set_rollback(True)
        except PilotDeploymentError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "schema_version": "org-memory-pilot-suspension-v1",
                    "organization_domain": organization.domain,
                    "applied": bool(options["apply"]),
                    "suspended": changed,
                    "reason": options["reason"],
                },
                sort_keys=True,
            )
        )
