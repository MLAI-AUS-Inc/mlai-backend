from django.core.management.base import BaseCommand, CommandError

from org_memory.models import ServicePrincipalCredential
from org_memory.service_principals import revoke_service_principal_credential


class Command(BaseCommand):
    help = "Immediately revoke one service-principal credential by UUID."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", required=True)
        parser.add_argument("--reason", default="operator revocation")

    def handle(self, *args, **options):
        try:
            credential = ServicePrincipalCredential.objects.select_related("principal").get(
                pk=options["credential_id"]
            )
        except (ServicePrincipalCredential.DoesNotExist, ValueError) as exc:
            raise CommandError("Service-principal credential not found") from exc
        revoke_service_principal_credential(credential, reason=options["reason"])
        self.stdout.write(self.style.SUCCESS(f"Revoked {credential.token_hint}"))
