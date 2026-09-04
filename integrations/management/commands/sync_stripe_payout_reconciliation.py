import json
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from integrations.models import ReconciliationProfile
from integrations.services.reconciliation import (
    ReconciliationReportService,
    luma_api_key_for_organization,
)
from integrations.services.xero_reconciliation import persist_report, serialize_payout


class Command(BaseCommand):
    help = "Backfill Stripe payout reconciliation records without posting anything to Xero."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--domain", default=getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au"))

    def handle(self, *args, **options):
        days = int(options["days"])
        if days < 1 or days > 92:
            raise CommandError("--days must be between 1 and 92")
        organization = Organization.objects.filter(domain__iexact=str(options["domain"]).strip()).first()
        if organization is None:
            raise CommandError("Organisation was not found")
        until = datetime.now(timezone.utc)
        report = ReconciliationReportService(
            luma_api_key=luma_api_key_for_organization(organization)
        ).build_report(
            since=until - timedelta(days=days),
            until=until,
            include_workbook=False,
        )
        profile = ReconciliationProfile.objects.filter(organization=organization).first()
        records = persist_report(
            organization=organization,
            report=report,
            stripe_account_id=profile.stripe_account_id if profile else "",
        )
        self.stdout.write(json.dumps({"payouts": [serialize_payout(record) for record in records]}, sort_keys=True))
