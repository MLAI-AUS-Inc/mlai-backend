from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from org_memory.models import ServicePrincipal
from org_memory.service_principals import (
    ServicePrincipalCredentialError,
    issue_service_principal_credential,
    normalize_scopes,
    normalize_surfaces,
)
from organizations.models import Organization


class Command(BaseCommand):
    help = "Create an organisation-bound service principal and print its credential once."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--scope", action="append", dest="scopes", default=[])
        parser.add_argument("--surface", action="append", dest="surfaces", default=[])

    @transaction.atomic
    def handle(self, *args, **options):
        domain = str(options["organization_domain"]).strip().lower()
        try:
            organization = Organization.objects.get(domain__iexact=domain)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization not found: {domain}") from exc
        if ServicePrincipal.objects.filter(name=options["name"]).exists():
            raise CommandError("A service principal with this name already exists")
        try:
            scopes = normalize_scopes(options["scopes"] or ["org_memory.read"])
            surfaces = normalize_surfaces(options["surfaces"] or ["admin_roo"])
        except ServicePrincipalCredentialError as exc:
            raise CommandError(str(exc)) from exc

        principal = ServicePrincipal.objects.create(
            name=str(options["name"]).strip(),
            organization=organization,
            scopes=scopes,
            allowed_surfaces=surfaces,
        )
        credential, token = issue_service_principal_credential(principal)
        self.stdout.write(self.style.SUCCESS(f"Created service principal {principal.name}"))
        self.stdout.write(f"Credential ID: {credential.pk}")
        self.stdout.write("Store this token now; it cannot be recovered later:")
        self.stdout.write(token)
