from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from org_memory.models import ServicePrincipal
from org_memory.service_principals import issue_service_principal_credential


class Command(BaseCommand):
    help = "Issue a replacement credential and expire the previous current credential."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--grace-seconds", type=int, default=0)

    @transaction.atomic
    def handle(self, *args, **options):
        grace_seconds = int(options["grace_seconds"])
        if grace_seconds < 0 or grace_seconds > 86400:
            raise CommandError("--grace-seconds must be between 0 and 86400")
        try:
            principal = ServicePrincipal.objects.select_for_update().get(name=options["name"])
        except ServicePrincipal.DoesNotExist as exc:
            raise CommandError("Service principal not found") from exc

        now = timezone.now()
        current = principal.credentials.filter(
            revoked_at__isnull=True,
        ).order_by("-created_at").first()
        credential, token = issue_service_principal_credential(
            principal,
            rotated_from=current,
        )
        if current:
            current.expires_at = now + timedelta(seconds=grace_seconds)
            current.save(update_fields=("expires_at",))

        self.stdout.write(self.style.SUCCESS(f"Rotated service principal {principal.name}"))
        self.stdout.write(f"Credential ID: {credential.pk}")
        self.stdout.write("Store this token now; it cannot be recovered later:")
        self.stdout.write(token)
