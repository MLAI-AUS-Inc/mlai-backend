import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.extraction_health import extraction_health_report


class Command(BaseCommand):
    help = (
        "Fail unless every eligible source version has a healthy run for the "
        "configured extractor and the organisation has queryable claims."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        report = extraction_health_report(
            organization=organization,
            provider=options.get("provider"),
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["ready"]:
            raise CommandError("Organisational-memory extraction health has blockers.")
