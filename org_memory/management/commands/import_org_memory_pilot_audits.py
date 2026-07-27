import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Organization
from org_memory.pilot_evidence import (
    PilotEvidenceError,
    import_pilot_audit_batch,
    load_pilot_audit_batch,
)


class Command(BaseCommand):
    help = (
        "Validate and atomically import independent, content-free Admin Brain "
        "pilot query audits. Dry-run by default; pass --apply to persist."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--audit-batch", required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the validated immutable audit rows.",
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(
                domain__iexact=options["organization_domain"]
            )
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        try:
            batch = load_pilot_audit_batch(options["audit_batch"])
            with transaction.atomic():
                result = import_pilot_audit_batch(
                    organization=organization,
                    batch=batch,
                )
                if not options["apply"]:
                    transaction.set_rollback(True)
        except PilotEvidenceError as exc:
            raise CommandError(str(exc)) from exc
        result["applied"] = bool(options["apply"])
        self.stdout.write(json.dumps(result, sort_keys=True))
