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
    StripePayoutReconciliation,
)
from integrations.services.reconciliation_reporting import (
    build_reconciliation_event_finance_audit,
    build_reconciliation_profitability_report,
)
from organizations.models import Organization
from roo.models import PointsAdmin
from startup_updates.models import LumaEventSelection


User = get_user_model()


def _tracked_line(
    *,
    amount: float,
    category: str,
    option: str,
    account_code: str | None = None,
    description: str = "",
) -> dict:
    return {
        "UnitAmount": amount,
        "Quantity": 1,
        "AccountCode": account_code or ("200" if amount >= 0 else "400"),
        "Description": description,
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


class ReconciliationEventFinanceAuditTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Fixture",
            domain="fixture.test",
        )
        self.user = User.objects.create_user(email="audit@example.test")
        self.xero = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-audit",
            access_token="secret-never-returned",
            scopes=["accounting.banktransactions"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.xero,
            event_tracking_category_name="Event Name",
            project_tracking_category_name="Project Name",
            fee_account_code="511",
        )

    def test_audits_catalog_and_xero_only_events_against_expected_categories(self):
        luma = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.LUMA,
            external_account_id="luma-audit",
            access_token="luma-secret-never-returned",
        )
        complete_event = LumaEventSelection.objects.create(
            connection=luma,
            user=self.user,
            organization=self.organization,
            event_id="evt-complete",
            event_name="Provider Gala",
            start_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
            selected=True,
        )
        LumaEventSelection.objects.create(
            connection=luma,
            user=self.user,
            organization=self.organization,
            event_id="evt-empty",
            event_name="No Ledger Activity",
            start_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            selected=True,
        )
        ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type=ReconciliationMapping.SOURCE_LUMA_EVENT,
            source_id=complete_event.event_id,
            event_tracking_option_name="Mapped Gala",
            active=True,
        )
        StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po-audit",
            arrival_date=date(2026, 7, 15),
            amount_cents=9_500,
            report_payload={
                "revenue_groups": [
                    {
                        "source_type": "luma_event",
                        "source_id": complete_event.event_id,
                        "source_label": complete_event.event_name,
                        "ticket_count": 2,
                        "gross_cents": 10_000,
                        "stripe_fee_cents": 500,
                    }
                ]
            },
        )
        transactions = [
            {
                "BankTransactionID": "sponsor-1",
                "Type": "RECEIVE",
                "Status": "AUTHORISED",
                "DateString": "2026-07-11",
                "Reference": "Sponsor agreement 42",
                "Contact": {"Name": "Fixture Sponsor"},
                "LineItems": [
                    _tracked_line(
                        amount=500,
                        category="Event Name",
                        option="Mapped Gala",
                        account_code="201",
                        description="Gold sponsorship",
                    )
                ],
            },
            {
                "BankTransactionID": "event-costs-1",
                "Type": "SPEND",
                "Status": "AUTHORISED",
                "DateString": "2026-07-12",
                "LineItems": [
                    _tracked_line(
                        amount=120,
                        category="Event Name",
                        option="Mapped Gala",
                        account_code="401",
                        description="Venue catering",
                    ),
                    _tracked_line(
                        amount=300,
                        category="Event Name",
                        option="Mapped Gala",
                        account_code="405",
                        description="Event producer",
                    ),
                ],
            },
            {
                "BankTransactionID": "xero-only-ticket",
                "Type": "RECEIVE",
                "Status": "AUTHORISED",
                "DateString": "2026-07-13",
                "LineItems": [
                    _tracked_line(
                        amount=80,
                        category="Event Name",
                        option="Xero Only Event",
                        account_code="202",
                    )
                ],
            },
        ]
        accounts = [
            {"Code": "201", "Name": "Sponsorships & Grants", "Type": "REVENUE", "Status": "ACTIVE"},
            {"Code": "202", "Name": "Ticket Sales", "Type": "REVENUE", "Status": "ACTIVE"},
            {"Code": "401", "Name": "Catering / Food & Beverages", "Type": "EXPENSE", "Status": "ACTIVE"},
            {"Code": "405", "Name": "Contractor Expenses", "Type": "EXPENSE", "Status": "ACTIVE"},
        ]

        audit = build_reconciliation_event_finance_audit(
            organization=self.organization,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            bank_transactions=transactions,
            accounts=accounts,
        )

        by_name = {event["event_name"]: event for event in audit["events"]}
        self.assertEqual(set(by_name), {"Mapped Gala", "No Ledger Activity", "Xero Only Event"})
        gala = by_name["Mapped Gala"]
        self.assertEqual(gala["completeness_status"], "complete")
        self.assertEqual(gala["categories"]["ticket_sales"]["status"], "present")
        self.assertEqual(gala["categories"]["sponsorship_revenue"]["status"], "present")
        self.assertEqual(gala["categories"]["catering_cost"]["status"], "present")
        contractor = gala["categories"]["contractor_cost"]
        self.assertEqual(contractor["status"], "present")
        self.assertEqual(contractor["evidence"][0]["account_name"], "Contractor Expenses")
        self.assertEqual(
            gala["categories"]["sponsorship_revenue"]["evidence"][0]["contact_name"],
            "Fixture Sponsor",
        )
        self.assertEqual(
            by_name["No Ledger Activity"]["missing_categories"],
            [
                "ticket_sales",
                "sponsorship_revenue",
                "catering_cost",
                "contractor_cost",
            ],
        )
        self.assertEqual(
            by_name["Xero Only Event"]["present_categories"],
            ["ticket_sales"],
        )
        self.assertEqual(audit["summary"]["event_count"], 3)
        self.assertEqual(audit["summary"]["complete_count"], 1)
        self.assertFalse(audit["xero_writes"])
        self.assertEqual(audit["account_resolution_warnings"], [])


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

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch(
        "integrations.api_views_reconciliation."
        "build_reconciliation_event_finance_audit"
    )
    def test_event_finance_audit_is_bounded_and_read_only(
        self, build_audit, _permission
    ):
        url = reverse("reconciliation_event_finance_audit")
        base = {"slack_user_id": "UADMIN", "domain": "fixture.test"}
        self.assertEqual(
            self.client.get(url, base).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        build_audit.return_value = {
            "schema_version": 1,
            "audit_version": "reconciliation-event-finance-audit-v1",
            "period_start": "2026-02-01",
            "period_end": "2026-08-01",
            "events": [],
            "summary": {"event_count": 0},
            "xero_writes": False,
        }

        response = self.client.get(
            url,
            {**base, "since": "2026-02-01", "until": "2026-08-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["xero_writes"])
        self.assertEqual(
            build_audit.call_args.kwargs["period_start"],
            date(2026, 2, 1),
        )
