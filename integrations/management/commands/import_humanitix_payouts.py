import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceConnection, ExternalServiceProvider
from integrations.services.humanitix_payouts import (
    HumanitixPayoutImportError,
    build_humanitix_xero_preview,
    import_payout_csv,
    serialize_humanitix_payout,
)
from organizations.models import Organization


class Command(BaseCommand):
    help = "Import a Humanitix global Payouts CSV and build Xero previews without posting."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument(
            "--domain",
            default=getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au"),
        )

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
        csv_path = Path(options["csv_path"]).expanduser()
        if not csv_path.is_file():
            raise CommandError(f"CSV file does not exist: {csv_path}")
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
                payouts = import_payout_csv(
                    organization=organization,
                    connection=connection,
                    source=source,
                )
            for payout in payouts:
                build_humanitix_xero_preview(payout)
        except HumanitixPayoutImportError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "payouts": [
                        serialize_humanitix_payout(payout, include_payload=True)
                        for payout in payouts
                    ],
                    "posted_to_xero": False,
                },
                sort_keys=True,
            )
        )
