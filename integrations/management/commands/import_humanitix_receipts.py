import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceConnection, ExternalServiceProvider
from integrations.services.humanitix_payouts import (
    HumanitixPayoutImportError,
    build_humanitix_xero_preview,
    import_humanitix_payout_receipt_pdf,
    serialize_humanitix_payout,
)
from organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Import Humanitix payout receipt PDFs, enrich payout previews, and never post to Xero."
    )

    def add_arguments(self, parser):
        parser.add_argument("pdf_paths", nargs="+")
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

        payouts = []
        imported_files = []
        try:
            for raw_path in options["pdf_paths"]:
                pdf_path = Path(raw_path).expanduser()
                if not pdf_path.is_file():
                    raise CommandError(f"PDF file does not exist: {pdf_path}")
                payout = import_humanitix_payout_receipt_pdf(
                    organization=organization,
                    connection=connection,
                    source=pdf_path,
                )
                build_humanitix_xero_preview(payout)
                payouts.append(payout)
                imported_files.append(pdf_path.name)
        except HumanitixPayoutImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            json.dumps(
                {
                    "files": imported_files,
                    "payouts": [
                        serialize_humanitix_payout(payout, include_payload=True)
                        for payout in payouts
                    ],
                    "posted_to_xero": False,
                },
                sort_keys=True,
            )
        )
