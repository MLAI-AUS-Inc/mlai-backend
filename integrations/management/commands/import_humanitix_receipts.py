import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixPayout,
    HumanitixPayoutLine,
)
from integrations.services.humanitix_payouts import (
    HumanitixPayoutImportError,
    build_humanitix_xero_preview,
    import_humanitix_payout_receipt_bundle,
    import_humanitix_payout_receipt_pdf,
    serialize_humanitix_payout,
)
from organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Import Humanitix payout receipt PDFs, enrich payout previews, and never post to Xero."
    )

    def add_arguments(self, parser):
        parser.add_argument("pdf_paths", nargs="*")
        parser.add_argument(
            "--domain",
            default=getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au"),
        )
        parser.add_argument(
            "--zip-stdin",
            action="store_true",
            help="Read a ZIP of receipt PDFs from stdin and import it atomically.",
        )
        parser.add_argument(
            "--require-all-net-only",
            action="store_true",
            help="Require the ZIP to contain every current net-only payout needing review.",
        )

    def handle(self, *args, **options):
        if bool(options["zip_stdin"]) == bool(options["pdf_paths"]):
            raise CommandError(
                "Provide PDF paths or --zip-stdin, but not both."
            )
        if options["require_all_net_only"] and not options["zip_stdin"]:
            raise CommandError("--require-all-net-only requires --zip-stdin.")
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
            if options["zip_stdin"]:
                expected_references = None
                if options["require_all_net_only"]:
                    expected_references = list(
                        HumanitixPayout.objects.filter(
                            organization=organization,
                            status=HumanitixPayout.STATUS_NEEDS_REVIEW,
                            lines__component=HumanitixPayoutLine.COMPONENT_NET_PAYOUT,
                        )
                        .values_list("payout_reference", flat=True)
                        .distinct()
                    )
                payouts = import_humanitix_payout_receipt_bundle(
                    organization=organization,
                    connection=connection,
                    source=sys.stdin.buffer,
                    expected_references=expected_references,
                )
                imported_files = [
                    f"receipt_{payout.payout_reference}.pdf" for payout in payouts
                ]
            else:
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
                    "imported_count": len(payouts),
                    "ready_count": sum(
                        payout.status == HumanitixPayout.STATUS_READY
                        for payout in payouts
                    ),
                    "payouts": [
                        serialize_humanitix_payout(payout, include_payload=True)
                        for payout in payouts
                    ],
                    "posted_to_xero": False,
                },
                sort_keys=True,
            )
        )
