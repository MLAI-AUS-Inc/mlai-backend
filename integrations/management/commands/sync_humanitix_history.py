import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
)
from integrations.services.humanitix import (
    HumanitixAPIError,
    HumanitixConfigurationError,
    sync_humanitix_connection,
)
from organizations.models import Organization


class Command(BaseCommand):
    help = "Backfill Humanitix events/orders/tickets into PII-free accounting aggregates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            default=getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au"),
        )
        parser.add_argument("--incremental", action="store_true")
        parser.add_argument("--skip-tickets", action="store_true")
        parser.add_argument("--max-events", type=int)

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=str(options["domain"]).strip()
        ).first()
        if organization is None:
            raise CommandError("Organisation was not found.")
        connection = (
            ExternalServiceConnection.objects.filter(
                organization=organization,
                provider=ExternalServiceProvider.HUMANITIX,
            )
            .exclude(status="disconnected")
            .order_by("-updated_at", "-id")
            .first()
        )
        if connection is None:
            raise CommandError("Humanitix is not connected for this organisation.")
        try:
            result = sync_humanitix_connection(
                connection,
                full_backfill=not options["incremental"],
                include_tickets=not options["skip_tickets"],
                max_events=options["max_events"],
            )
        except (HumanitixConfigurationError, HumanitixAPIError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
