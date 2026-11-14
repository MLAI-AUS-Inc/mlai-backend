"""Run a reviewed, company-scoped article research reset."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization

from content_factory.research_workspace import reset_workspace


class Command(BaseCommand):
    help = "Plan, freeze, clear or resume article research for one existing company."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("plan", "freeze", "clear", "resume"))
        parser.add_argument("--company-id", required=True)
        parser.add_argument("--organization-id", type=int, required=True)
        parser.add_argument("--domain", required=True)
        parser.add_argument("--reviewed-digest")

    def handle(self, *args, **options):
        with transaction.atomic():
            organization = Organization.objects.select_for_update().filter(
                pk=options["organization_id"], domain=options["domain"],
            ).first()
            if organization is None:
                raise CommandError("The reviewed organization and domain do not match.")
            companies = list(VibeRaisingCompany.objects.filter(organization=organization).values_list("pk", flat=True)[:2])
            if [str(company_id) for company_id in companies] != [options["company_id"]]:
                raise CommandError("The reviewed company does not exclusively own this organization.")
            try:
                result = reset_workspace(
                    organization, action=options["action"], reviewed_digest=options["reviewed_digest"],
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({"company_id": options["company_id"], **result}, default=str))
