import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.models import OrganizationMembership
from org_memory.reconciliation import run_daily_reconciliation


class Command(BaseCommand):
    help = "Refresh one organisation's daily memory-health report after bounded recovery."

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--operator-email")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        if not options["apply"]:
            self.stdout.write(
                json.dumps(
                    {
                        "apply": False,
                        "organization_domain": organization.domain,
                        "would_refresh": True,
                    },
                    sort_keys=True,
                )
            )
            return
        operator_email = str(options.get("operator_email") or "").strip()
        if not operator_email:
            raise CommandError("--operator-email is required with --apply.")
        if not OrganizationMembership.objects.filter(
            organization=organization,
            user__email__iexact=operator_email,
        ).exists():
            raise CommandError("Operator is not a member of the organization.")
        result = run_daily_reconciliation(
            organization_id=organization.pk,
            force=True,
        )
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
