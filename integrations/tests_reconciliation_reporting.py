from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixEvent,
    HumanitixEventFinancialSummary,
    ReconciliationMapping,
    ReconciliationProfile,
)
from integrations.services.reconciliation_reporting import (
    build_reconciliation_profitability_report,
)
from organizations.models import Organization
from roo.models import PointsAdmin


User = get_user_model()


def _tracked_line(*, amount: float, category: str, option: str) -> dict:
    return {
        "UnitAmount": amount,
        "Quantity": 1,
        "AccountCode": "200" if amount >= 0 else "400",
        "Tracking": [{"Name": category, "Option": option}],
    }


class ReconciliationProfitabilityReportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Fixture", domain="fixture.test")
        self.user = User.objects.create_user(email="report@example.test")
        self.xero = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-report",
            access_token="secret-never-returned",
            scopes=["accounting.banktransactions"],
        )
        self.profile = ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.xero,
            event_tracking_category_name="Event Name",
            project_tracking_category_name="Project Name",
            fee_account_code="511",
        )

    def test_carbon_game_fixture_ties_and_has_source_drilldown(self):
        report = build_reconciliation_profitability_report(
            organization=self.organization,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            bank_transactions=[
                {
                    "BankTransactionID": "carbon-income",
                    "Type": "RECEIVE",
                    "Status": "AUTHORISED",
                    "DateString": "2026-07-10",
                    "LineItems": [
                        _tracked_line(
                            amount=48750,
                            category="Project Name",
                            option="Carbon Game",
                        )
                    ],
                },
                {
                    "BankTransactionID": "carbon-costs",
                    "Type": "SPEND",
                    "Status": "AUTHORISED",
                    "DateString": "2026-07-12",
                    "LineItems": [
                        _tracked_line(
                            amount=36010.16,
                            category="Project Name",
                            option="Carbon Game",
                        )
                    ],
                },
            ],
        )

        carbon = report["dimensions"]["projects"][0]
        self.assertEqual(carbon["dimension_name"], "Carbon Game")
        self.assertEqual(carbon["revenue_cents"], 4_875_000)
        self.assertEqual(carbon["cost_cents"], 3_601_016)
        self.assertEqual(carbon["profit_cents"], 1_273_984)
        self.assertEqual(carbon["tie_out_cents"], 0)
        self.assertEqual(
            {item["bank_transaction_id"] for item in carbon["sources"]},
            {"carbon-income", "carbon-costs"},
        )
        self.assertEqual(
            report["summaries"]["projects"]["eligible_profit_cents"],
            1_273_984,
        )
        self.assertEqual(
            report["monthly"][0]["projects"]["eligible_profit_cents"],
            1_273_984,
        )
        self.assertEqual(
            report["summaries"]["events"]["profitability_status"],
            "unavailable",
        )
        self.assertIsNone(
            report["summaries"]["events"]["profit_margin_percent"]
        )

    def test_humanitix_native_revenue_is_visible_but_excluded_by_default(self):
        humanitix = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.HUMANITIX,
            external_account_id="humanitix-report",
            access_token="humanitix-secret-never-returned",
        )
        event = HumanitixEvent.objects.create(
            organization=self.organization,
            connection=humanitix,
            external_event_id="humanitix-event-1",
            event_name="Humanitix Fixture",
            start_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            currency="AUD",
            source_hash="a" * 64,
        )
        HumanitixEventFinancialSummary.objects.create(
            event=event,
            source_hash="b" * 64,
            gateway_breakdown={
                "stripe": {
                    "classification": "stripe",
                    "orders": 1,
                    "gross_sales": "50.00",
                    "net_sales": "48.00",
                    "refunds": "0.00",
                },
                "cash": {
                    "classification": "offline",
                    "orders": 1,
                    "gross_sales": "20.00",
                    "net_sales": "20.00",
                    "refunds": "0.00",
                },
                "bpoint": {
                    "classification": "humanitix_native",
                    "orders": 2,
                    "gross_sales": "100.00",
                    "net_sales": "85.00",
                    "refunds": "10.00",
                },
            },
        )
        ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            source_id=event.external_event_id,
            event_tracking_option_name=event.event_name,
            active=True,
        )
        transactions = [
            {
                "BankTransactionID": "humanitix-event-cost",
                "Type": "SPEND",
                "Status": "AUTHORISED",
                "DateString": "2026-07-16",
                "LineItems": [
                    _tracked_line(
                        amount=20,
                        category="Event Name",
                        option=event.event_name,
                    )
                ],
            }
        ]

        excluded = build_reconciliation_profitability_report(
            organization=self.organization,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            bank_transactions=transactions,
        )
        row = excluded["dimensions"]["events"][0]
        self.assertEqual(row["revenue_cents"], 9000)
        self.assertEqual(row["cost_cents"], 2500)
        self.assertEqual(row["profit_cents"], 6500)
        self.assertFalse(row["profitability_included"])
        self.assertEqual(row["profitability_status"], "excluded_by_policy")
        self.assertEqual(
            excluded["summaries"]["events"]["visible_revenue_cents"],
            9000,
        )
        self.assertEqual(
            excluded["summaries"]["events"]["eligible_revenue_cents"],
            0,
        )
        self.assertEqual(
            excluded["summaries"]["events"]["profitability_status"],
            "unavailable",
        )

        self.profile.humanitix_profitability_included = True
        self.profile.profitability_policy_verified_by_slack_id = "UADMIN"
        self.profile.profitability_policy_verified_at = datetime.now(timezone.utc)
        self.profile.save(
            update_fields=[
                "humanitix_profitability_included",
                "profitability_policy_verified_by_slack_id",
                "profitability_policy_verified_at",
                "updated_at",
            ]
        )
        included = build_reconciliation_profitability_report(
            organization=self.organization,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            bank_transactions=transactions,
        )
        row = included["dimensions"]["events"][0]
        self.assertTrue(row["profitability_included"])
        self.assertEqual(row["profitability_status"], "positive")
        self.assertEqual(
            included["summaries"]["events"]["eligible_profit_cents"],
            6500,
        )


class ReconciliationProfitabilityReportApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Fixture", domain="fixture.test")
        PointsAdmin.objects.create(
            slack_user_id="UADMIN",
            role="admin",
            is_active=True,
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch(
        "integrations.api_views_reconciliation."
        "build_reconciliation_profitability_report"
    )
    def test_report_requires_bounded_period_and_is_read_only(
        self, build_report, _permission
    ):
        url = reverse("reconciliation_cashflow_report")
        base = {"slack_user_id": "UADMIN", "domain": "fixture.test"}
        self.assertEqual(
            self.client.get(url, base).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        build_report.return_value = {
            "schema_version": 1,
            "report_version": "reconciliation-profitability-v1",
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "dimensions": {"events": [], "projects": []},
            "summaries": {},
            "monthly": [],
            "xero_writes": False,
        }

        response = self.client.get(
            url,
            {**base, "since": "2026-07-01", "until": "2026-07-31"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["xero_writes"])
        self.assertEqual(
            build_report.call_args.kwargs["period_start"],
            date(2026, 7, 1),
        )
        self.assertEqual(
            build_report.call_args.kwargs["period_end"],
            date(2026, 7, 31),
        )
