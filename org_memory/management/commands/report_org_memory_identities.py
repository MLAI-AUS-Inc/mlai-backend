import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.membership_backfill import build_identity_resolution_report


class Command(BaseCommand):
    help = "Report unresolved, missing, and conflicting organisational identity mappings."

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain")

    def handle(self, *args, **options):
        organization = None
        domain = options.get("organization_domain")
        if domain:
            try:
                organization = Organization.objects.get(domain=domain)
            except Organization.DoesNotExist as exc:
                raise CommandError("Organization does not exist") from exc
        report = build_identity_resolution_report(organization)
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
