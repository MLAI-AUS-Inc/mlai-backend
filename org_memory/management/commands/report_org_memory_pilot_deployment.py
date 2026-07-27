import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.pilot_deployment import pilot_deployment_report


class Command(BaseCommand):
    help = "Report content-free Admin Roo pilot runtime deployment state."

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument(
            "--fail-if-ineffective",
            action="store_true",
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(
                domain__iexact=options["organization_domain"]
            )
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        report = pilot_deployment_report(organization)
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_if_ineffective"] and not report["effective"]:
            raise CommandError("Admin Brain pilot runtime deployment is ineffective.")
