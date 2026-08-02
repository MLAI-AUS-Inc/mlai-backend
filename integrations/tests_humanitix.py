from __future__ import annotations

import base64
import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixEvent,
    HumanitixEventFinancialSummary,
    HumanitixPayout,
    HumanitixPayoutLine,
    ReconciliationMapping,
    ReconciliationProfile,
)
from integrations.services.humanitix import (
    HumanitixClient,
    aggregate_orders,
    sync_humanitix_connection,
)
from integrations.services.humanitix_payouts import (
    HumanitixPayoutImportError,
    build_humanitix_xero_correction_preview,
    build_humanitix_xero_preview,
    import_payout_csv,
    import_humanitix_payout_receipt_bundle,
    import_humanitix_payout_receipt_pdf,
    import_humanitix_payout_receipt_text,
    parse_humanitix_payout_receipt_text,
    post_humanitix_xero_bank_transaction,
)
from integrations.services.xero_reconciliation import ReconciliationValidationError
from organizations.models import Organization
from roo.models import PointsAdmin


User = get_user_model()


class HumanitixOperationsApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="humanitix@example.com")
        PointsAdmin.objects.create(
            slack_user_id="UADMIN", role="admin", is_active=True
        )
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.HUMANITIX,
            access_token="humanitix-secret-never-return",
            external_account_id="humanitix-account",
            status="connected",
            sync_cursor={
                "humanitix_complete": True,
                "humanitix_full_backfill": True,
                "humanitix_events_synced": 1,
                "humanitix_orders_synced": 3,
                "humanitix_tickets_synced": 4,
            },
        )
        event = HumanitixEvent.objects.create(
            organization=self.organization,
            connection=self.connection,
            external_event_id="event-api-1",
            event_name="Pitch Night",
            currency="AUD",
            source_hash="a" * 64,
        )
        HumanitixEventFinancialSummary.objects.create(
            event=event,
            order_count=3,
            paid_order_count=2,
            ticket_count=4,
            gross_sales="120.00",
            net_sales="110.00",
            refunds="10.00",
            gateway_breakdown={
                "stripe": {
                    "classification": "stripe",
                    "orders": 1,
                    "gross_sales": "50.00",
                },
                "bpoint": {
                    "classification": "humanitix_native",
                    "orders": 1,
                    "gross_sales": "70.00",
                },
                "cash": {
                    "classification": "offline",
                    "orders": 1,
                    "gross_sales": "0.00",
                },
            },
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_status_and_events_are_pii_free_and_never_return_api_key(self, _permission):
        query = {"slack_user_id": "UADMIN", "domain": "mlai.au"}
        status_response = self.client.get(
            reverse("reconciliation_humanitix_status"), query
        )
        events_response = self.client.get(
            reverse("reconciliation_humanitix_events"), query
        )

        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertTrue(status_response.data["connected"])
        self.assertTrue(status_response.data["complete"])
        self.assertNotIn("humanitix-secret-never-return", str(status_response.data))
        self.assertEqual(events_response.status_code, status.HTTP_200_OK)
        self.assertFalse(events_response.data["pii_included"])
        self.assertEqual(events_response.data["events"][0]["order_count"], 3)
        self.assertEqual(
            events_response.data["gateway_policy"]["stripe"],
            "excluded_from_humanitix_payouts",
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch("integrations.api_views_reconciliation.sync_humanitix_connection")
    def test_sync_requires_confirmation_and_exposes_full_or_incremental_mode(
        self, sync, _permission
    ):
        url = reverse("reconciliation_humanitix_sync")
        base = {"slack_user_id": "UADMIN", "domain": "mlai.au"}
        unconfirmed = self.client.post(url, base, format="json")
        self.assertEqual(unconfirmed.status_code, status.HTTP_400_BAD_REQUEST)

        sync.return_value = {
            "status": "synced",
            "events_synced": 1,
            "orders_synced": 3,
            "tickets_synced": 4,
            "full_backfill": False,
            "complete": True,
        }
        response = self.client.post(
            url,
            {
                **base,
                "confirm": True,
                "full_backfill": False,
                "include_tickets": True,
                "max_events": 25,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["full_backfill"])
        sync.assert_called_once_with(
            self.connection,
            full_backfill=False,
            include_tickets=True,
            max_events=25,
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch(
        "integrations.api_views_reconciliation."
        "import_humanitix_payout_receipt_bundle"
    )
    def test_receipt_bundle_requires_confirmation_and_passes_expected_manifest(
        self, import_bundle, _permission
    ):
        url = reverse("reconciliation_humanitix_receipt_import")
        base = {
            "slack_user_id": "UADMIN",
            "domain": "mlai.au",
            "kind": "zip",
            "content_base64": base64.b64encode(b"fixture zip").decode(),
            "expected_references": ["HP-1", "HP-2"],
        }
        self.assertEqual(
            self.client.post(url, base, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        import_bundle.return_value = []

        response = self.client.post(
            url, {**base, "confirm": True}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["posted_to_xero"])
        self.assertEqual(
            import_bundle.call_args.kwargs["expected_references"],
            ["HP-1", "HP-2"],
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_global_csv_import_requires_confirmation(self, _permission):
        response = self.client.post(
            reverse("reconciliation_humanitix_payout_import"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "csv_content": "Payout Reference,Payout Amount\nHP-1,10.00\n",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_payout_list_returns_serialized_records(self, _permission):
        HumanitixPayout.objects.create(
            organization=self.organization,
            connection=self.connection,
            payout_reference="HP-LISTED",
            currency="AUD",
            payout_amount="10.00",
        )

        response = self.client.get(
            reverse("reconciliation_humanitix_payouts"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payouts"][0]["payout_reference"], "HP-LISTED")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch(
        "integrations.api_views_reconciliation."
        "post_humanitix_xero_bank_transaction"
    )
    def test_payout_post_requires_and_forwards_reviewed_payload_hash(
        self, post_payout, _permission
    ):
        payout = HumanitixPayout.objects.create(
            organization=self.organization,
            connection=self.connection,
            payout_reference="HP-HASHED",
            currency="AUD",
            payout_amount="10.00",
        )
        url = reverse(
            "reconciliation_humanitix_payout_post",
            kwargs={"payout_reference": payout.payout_reference},
        )
        base = {
            "slack_user_id": "UADMIN",
            "domain": "mlai.au",
            "confirm": True,
        }

        missing_hash = self.client.post(url, base, format="json")
        self.assertEqual(missing_hash.status_code, status.HTTP_400_BAD_REQUEST)
        post_payout.assert_not_called()

        post_payout.return_value = payout
        reviewed_hash = "a" * 64
        response = self.client.post(
            url,
            {**base, "payload_hash": reviewed_hash},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            post_payout.call_args.kwargs["expected_payload_hash"],
            reviewed_hash,
        )


class FakeResponse:
    def __init__(self, payload, *, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        path = urlparse(url).path
        self.calls.append((path, dict(params), dict(headers)))
        return self.handler(path, params)


class HumanitixAggregateTests(TestCase):
    def test_gateway_breakdown_distinguishes_stripe_native_and_offline(self):
        summary = aggregate_orders(
            [
                {
                    "financialStatus": "paid",
                    "paymentGateway": "stripe",
                    "manualOrder": False,
                    "totals": {"grossSales": 100, "netSales": 97, "refunds": 0},
                },
                {
                    "financialStatus": "paid",
                    "paymentGateway": "bpoint",
                    "manualOrder": False,
                    "totals": {"grossSales": 50, "netSales": 50, "refunds": 0},
                },
                {
                    "financialStatus": "paid",
                    "paymentGateway": "braintree",
                    "manualOrder": False,
                    "totals": {"grossSales": 40, "netSales": 40, "refunds": 0},
                },
                {
                    "financialStatus": "paid",
                    "paymentGateway": "invoice",
                    "manualOrder": True,
                    "totals": {"grossSales": 25, "netSales": 25, "refunds": 0},
                },
            ]
        )

        self.assertEqual(summary["gateway_breakdown"]["stripe"]["classification"], "stripe")
        self.assertEqual(
            summary["gateway_breakdown"]["bpoint"]["classification"],
            "humanitix_native",
        )
        self.assertEqual(
            summary["gateway_breakdown"]["braintree"]["classification"],
            "humanitix_native",
        )
        self.assertEqual(summary["gateway_breakdown"]["invoice"]["classification"], "offline")
        self.assertEqual(summary["gross_sales"], Decimal("215.00"))


class HumanitixSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.HUMANITIX,
            access_token="humanitix-secret",
            token_type="api_key",
            external_account_id="founder@example.com",
            account_label="Humanitix",
        )

    def test_full_sync_paginates_and_stores_only_financial_aggregates(self):
        def handler(path, params):
            if path == "/v1/events":
                if params["page"] == 2:
                    return FakeResponse(
                        {
                            "total": 2,
                            "page": 2,
                            "pageSize": 1,
                            "events": [
                                {
                                    "_id": "event-2",
                                    "name": "Free event",
                                    "currency": "AUD",
                                    "startDate": "2025-06-01T08:00:00Z",
                                    "endDate": "2025-06-01T10:00:00Z",
                                    "published": True,
                                    "totalCapacity": 100,
                                }
                            ],
                        }
                    )
                return FakeResponse(
                    {
                        "total": 2,
                        "page": 1,
                        "pageSize": 1,
                        "events": [
                            {
                                "_id": "event-1",
                                "name": "Pitch Night: MedHack",
                                "description": "This field is not persisted",
                                "currency": "AUD",
                                "startDate": "2025-02-27T06:45:00Z",
                                "endDate": "2025-02-27T09:00:00Z",
                                "published": True,
                                "totalCapacity": 370,
                            }
                        ],
                    }
                )
            if path.endswith("/orders"):
                event_id = path.split("/")[-2]
                if event_id == "event-2":
                    return FakeResponse(
                        {
                            "total": 0,
                            "page": 1,
                            "pageSize": 100,
                            "orders": [],
                        }
                    )
                return FakeResponse(
                    {
                        "total": 1,
                        "page": 1,
                        "pageSize": 100,
                        "orders": [
                            {
                                "_id": "order-1",
                                "email": "buyer@example.com",
                                "firstName": "Private",
                                "financialStatus": "paid",
                                "paymentGateway": "bpoint",
                                "manualOrder": False,
                                "totals": {
                                    "grossSales": 2343.80,
                                    "netSales": 2343.80,
                                    "clientDonation": 41,
                                    "humanitixFee": 0,
                                    "refunds": 0,
                                    "discounts": 0,
                                    "totalTaxes": 0,
                                },
                            }
                        ],
                    }
                )
            if path.endswith("/tickets"):
                event_id = path.split("/")[-2]
                tickets = [] if event_id == "event-2" else [
                    {
                        "_id": "ticket-1",
                        "firstName": "Private",
                        "email": "attendee@example.com",
                        "status": "complete",
                        "ticketTypeId": "general",
                        "ticketTypeName": "General Admission",
                        "netPrice": 13,
                        "taxes": 0,
                        "absorbedFee": 0,
                        "total": 13,
                    }
                ]
                return FakeResponse(
                    {
                        "total": len(tickets),
                        "page": 1,
                        "pageSize": 100,
                        "tickets": tickets,
                    }
                )
            raise AssertionError(path)

        client = HumanitixClient(
            api_key="secret",
            base_url="https://humanitix.test/v1",
            session=FakeSession(handler),
        )
        result = sync_humanitix_connection(
            self.connection,
            client=client,
            full_backfill=True,
        )

        self.assertEqual(result["events_synced"], 2)
        self.assertEqual(result["orders_synced"], 1)
        event = HumanitixEvent.objects.get(external_event_id="event-1")
        summary = HumanitixEventFinancialSummary.objects.get(event=event)
        self.assertEqual(summary.gross_sales, Decimal("2343.80"))
        self.assertEqual(summary.donations, Decimal("41.00"))
        self.assertEqual(summary.ticket_count, 1)
        self.assertEqual(
            summary.gateway_breakdown["bpoint"]["classification"],
            "humanitix_native",
        )
        stored = f"{event.source_payload} {summary.gateway_breakdown} {summary.ticket_type_breakdown}"
        self.assertNotIn("buyer@example.com", stored)
        self.assertNotIn("attendee@example.com", stored)
        self.assertNotIn("Private", stored)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.sync_cursor["humanitix_complete"])


class HumanitixPayoutImportTests(TestCase):
    RECEIPT_TEXT = """
Payout receipt
Reference: HPVYXE2PRN
Event name: Pitch Night: MedHack
Event date: Thu, 27 Feb 2025, 5:45pm - 9pm AEDT
Payout details $110.00Processed at: 4th Mar 2025
Payout breakdown to date
Sales via Humanitix payments $115.00
Absorbed Humanitix fees ($2.00)
Refunds ($3.00)
Earnings by type
Sales Refunds Total absorbed fees Earnings
Ticket sales $100.00 ($3.00) ($2.00) $95.00
Add-on sales $5.00 ($0.00) ($0.00) $5.00
Additional donations $10.00 ($0.00) ($0.00) $10.00
Total $115.00 ($3.00) ($2.00) $110.00
"""

    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.humanitix_connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.HUMANITIX,
            access_token="humanitix-secret",
            token_type="api_key",
            external_account_id="founder@example.com",
            account_label="Humanitix",
        )
        self.xero_connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.organization,
            provider=ExternalServiceProvider.XERO,
            access_token="xero-token",
            external_account_id="tenant-1",
            account_label="MLAI Xero",
            scopes=["accounting.banktransactions", "accounting.settings"],
        )
        self.event = HumanitixEvent.objects.create(
            organization=self.organization,
            connection=self.humanitix_connection,
            external_event_id="event-medhack",
            event_name="Pitch Night: MedHack",
            currency="AUD",
        )
        HumanitixEventFinancialSummary.objects.create(
            event=self.event,
            gateway_breakdown={
                "bpoint": {
                    "classification": "humanitix_native",
                    "orders": 163,
                    "gross_sales": "2343.80",
                    "net_sales": "2343.80",
                    "refunds": "0.00",
                }
            },
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.xero_connection,
            xero_bank_account_id="bank-1",
            revenue_account_code="202",
            fee_account_code="404",
            refund_account_code="202",
            revenue_tax_type="NONE",
            fee_tax_type="NONE",
            refund_tax_type="NONE",
            event_tracking_category_id="event-category",
            event_tracking_category_name="Event Name",
            humanitix_contact_name="Humanitix",
        )

    def _import_tied_payout(self):
        source = io.StringIO(
            "Payout Reference,Payout Date,Currency,Payout Amount,Event ID,Event Name,"
            "Sales via Humanitix payments,Additional donations,Absorbed Humanitix fees,Refunds\n"
            "HPVYXE2PRN,2025-03-04,AUD,2343.80,event-medhack,Pitch Night: MedHack,"
            "2343.80,41.00,0,0\n"
        )

        return import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )[0]

    def test_import_builds_tied_event_split_preview(self):
        payout = self._import_tied_payout()
        preview = build_humanitix_xero_preview(payout)

        self.assertTrue(preview["ready"], preview["errors"])
        self.assertEqual(len(preview["payload_hash"]), 64)
        self.assertEqual(preview["line_total"], "2343.80")
        self.assertEqual(
            [line["UnitAmount"] for line in preview["xero_payload"]["LineItems"]],
            [2302.8, 41.0],
        )
        self.assertEqual(preview["xero_payload"]["Reference"], "HPVYXE2PRN")
        self.assertEqual(
            preview["xero_payload"]["LineItems"][0]["Tracking"][0]["Option"],
            "Pitch Night: MedHack",
        )

    @patch(
        "integrations.services.humanitix_payouts.fetch_xero_bank_transactions"
    )
    @patch("integrations.services.humanitix_payouts.http_client.put")
    def test_post_rejects_a_stale_reviewed_payload_hash_before_xero_access(
        self, mock_put, mock_fetch
    ):
        payout = self._import_tied_payout()
        reviewed = build_humanitix_xero_preview(payout)
        profile = ReconciliationProfile.objects.get(organization=self.organization)
        profile.revenue_account_code = "999"
        profile.save(update_fields=["revenue_account_code", "updated_at"])

        with self.assertRaisesRegex(
            ReconciliationValidationError,
            "preview changed after review",
        ):
            post_humanitix_xero_bank_transaction(
                payout,
                approved_by_slack_id="UADMIN",
                expected_payload_hash=reviewed["payload_hash"],
            )

        mock_fetch.assert_not_called()
        mock_put.assert_not_called()
        mapping = ReconciliationMapping.objects.get(
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            source_id="event-medhack",
        )
        self.assertEqual(mapping.event_tracking_option_name, "Pitch Night: MedHack")
        self.assertEqual(
            set(payout.lines.values_list("component", flat=True)),
            {
                HumanitixPayoutLine.COMPONENT_TICKET_SALES,
                HumanitixPayoutLine.COMPONENT_DONATIONS,
            },
        )

    def test_correction_preview_allows_missing_xero_transaction(self):
        payout = self._import_tied_payout()

        preview = build_humanitix_xero_correction_preview(
            payout,
            bank_transactions=[],
        )

        self.assertEqual(preview["classification"], "missing_xero_transaction")
        self.assertEqual(preview["recommended_action"], "create_receive_money")
        self.assertTrue(preview["automatic_action_allowed"])

    def test_correction_preview_blocks_reconciled_legacy_net_transaction(self):
        payout = self._import_tied_payout()
        existing = {
            "BankTransactionID": "legacy-net-1",
            "Reference": "",
            "Date": "2025-03-04",
            "Type": "RECEIVE",
            "Status": "AUTHORISED",
            "IsReconciled": True,
            "Total": "2343.80",
            "BankAccount": {"AccountID": "bank-1"},
            "LineItems": [
                {
                    "Quantity": 1,
                    "UnitAmount": "2343.80",
                    "AccountCode": "202",
                    "TaxType": "NONE",
                    "Tracking": [],
                }
            ],
        }

        preview = build_humanitix_xero_correction_preview(
            payout,
            bank_transactions=[existing],
        )

        self.assertEqual(preview["classification"], "legacy_net_only")
        self.assertEqual(preview["recommended_action"], "unreconcile_then_replace")
        self.assertTrue(preview["requires_manual_unreconcile"])
        self.assertFalse(preview["automatic_action_allowed"])

    def test_correction_preview_recognizes_correct_existing_transaction(self):
        payout = self._import_tied_payout()
        proposed = build_humanitix_xero_preview(payout)["xero_payload"]
        existing = {
            **proposed,
            "BankTransactionID": "correct-existing-1",
            "Reference": "",
            "Total": "2343.80",
            "IsReconciled": True,
        }

        preview = build_humanitix_xero_correction_preview(
            payout,
            bank_transactions=[existing],
        )

        self.assertEqual(preview["classification"], "already_correct")
        self.assertEqual(preview["recommended_action"], "record_existing")
        self.assertTrue(preview["automatic_action_allowed"])

    @patch(
        "integrations.services.humanitix_payouts.fetch_xero_bank_transactions"
    )
    @patch("integrations.services.humanitix_payouts.http_client.put")
    def test_confirmed_post_blocks_legacy_transaction_before_xero_write(
        self,
        mock_put,
        mock_fetch,
    ):
        payout = self._import_tied_payout()
        mock_fetch.return_value = [
            {
                "BankTransactionID": "legacy-net-1",
                "Reference": "",
                "Date": "2025-03-04",
                "Type": "RECEIVE",
                "Status": "AUTHORISED",
                "IsReconciled": True,
                "Total": "2343.80",
                "BankAccount": {"AccountID": "bank-1"},
                "LineItems": [
                    {
                        "Quantity": 1,
                        "UnitAmount": "2343.80",
                        "AccountCode": "202",
                        "TaxType": "NONE",
                        "Tracking": [],
                    }
                ],
            }
        ]

        with self.assertRaises(ReconciliationValidationError):
            post_humanitix_xero_bank_transaction(
                payout,
                approved_by_slack_id="UADMIN",
            )

        mock_put.assert_not_called()
        payout.refresh_from_db()
        self.assertEqual(payout.status, HumanitixPayout.STATUS_READY)

    @patch(
        "integrations.services.humanitix_payouts.fetch_xero_bank_transactions"
    )
    @patch("integrations.services.humanitix_payouts.http_client.put")
    @patch("integrations.services.humanitix_payouts.http_client.get")
    def test_confirmed_post_reuses_supplied_bank_transaction_snapshot(
        self,
        mock_get,
        mock_put,
        mock_fetch,
    ):
        payout = self._import_tied_payout()

        categories_response = MagicMock()
        categories_response.raise_for_status.return_value = None
        categories_response.json.return_value = {
            "TrackingCategories": [
                {
                    "TrackingCategoryID": "event-category",
                    "Options": [
                        {
                            "TrackingOptionID": "event-option-medhack",
                            "Name": "Pitch Night: MedHack",
                        }
                    ],
                }
            ]
        }
        existing_response = MagicMock()
        existing_response.raise_for_status.return_value = None
        existing_response.json.return_value = {"BankTransactions": []}
        mock_get.side_effect = [categories_response, existing_response]
        create_response = MagicMock()
        create_response.raise_for_status.return_value = None
        create_response.json.return_value = {
            "BankTransactions": [{"BankTransactionID": "xero-batch-1"}]
        }
        mock_put.return_value = create_response

        posted = post_humanitix_xero_bank_transaction(
            payout,
            approved_by_slack_id="UADMIN",
            bank_transactions=[],
        )

        self.assertEqual(posted.xero_bank_transaction_id, "xero-batch-1")
        mock_fetch.assert_not_called()
        self.assertEqual(mock_put.call_count, 1)

    @patch(
        "integrations.services.humanitix_payouts.fetch_xero_bank_transactions",
        return_value=[],
    )
    @patch("integrations.services.humanitix_payouts.http_client.put")
    @patch("integrations.services.humanitix_payouts.http_client.get")
    def test_confirmed_post_is_idempotent_and_resolves_tracking(
        self,
        mock_get,
        mock_put,
        _mock_fetch,
    ):
        payout = self._import_tied_payout()

        categories_response = MagicMock()
        categories_response.raise_for_status.return_value = None
        categories_response.json.return_value = {
            "TrackingCategories": [
                {
                    "TrackingCategoryID": "event-category",
                    "Options": [
                        {
                            "TrackingOptionID": "event-option-medhack",
                            "Name": "Pitch Night: MedHack",
                        }
                    ],
                }
            ]
        }
        existing_response = MagicMock()
        existing_response.raise_for_status.return_value = None
        existing_response.json.return_value = {"BankTransactions": []}
        mock_get.side_effect = [categories_response, existing_response]
        create_response = MagicMock()
        create_response.raise_for_status.return_value = None
        create_response.json.return_value = {
            "BankTransactions": [{"BankTransactionID": "xero-bank-transaction-1"}]
        }
        mock_put.return_value = create_response

        posted = post_humanitix_xero_bank_transaction(
            payout,
            approved_by_slack_id="UADMIN",
        )
        posted_again = post_humanitix_xero_bank_transaction(
            posted,
            approved_by_slack_id="UADMIN",
        )

        self.assertEqual(posted.status, HumanitixPayout.STATUS_POSTED)
        self.assertEqual(posted.approved_by_slack_id, "UADMIN")
        self.assertIsNotNone(posted.approved_at)
        self.assertEqual(posted.xero_bank_transaction_id, "xero-bank-transaction-1")
        self.assertEqual(
            ReconciliationMapping.objects.get(
                source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
                source_id="event-medhack",
            ).event_tracking_option_id,
            "event-option-medhack",
        )
        self.assertEqual(posted_again.pk, posted.pk)
        self.assertEqual(mock_put.call_count, 1)

    def test_net_only_payout_stays_needs_review(self):
        source = io.StringIO(
            "Payout Reference,Payout Date,Currency,Payout Amount,Event ID,Event Name\n"
            "NETONLY,2025-03-04,AUD,100.00,event-medhack,Pitch Night: MedHack\n"
        )
        payout = import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )[0]
        preview = build_humanitix_xero_preview(payout)

        self.assertFalse(preview["ready"])
        self.assertTrue(any("net payout" in error for error in preview["errors"]))
        payout.refresh_from_db()
        self.assertEqual(payout.status, HumanitixPayout.STATUS_NEEDS_REVIEW)

    def test_receipt_text_parser_extracts_accounting_components(self):
        row = parse_humanitix_payout_receipt_text(self.RECEIPT_TEXT)

        self.assertEqual(row["payout reference"], "HPVYXE2PRN")
        self.assertEqual(row["payout date"], "2025-03-04")
        self.assertEqual(row["payout amount"], "110.00")
        self.assertEqual(row["event name"], "Pitch Night: MedHack")
        self.assertEqual(row["ticket sales"], "100.00")
        self.assertEqual(row["add-on sales"], "5.00")
        self.assertEqual(row["additional donations"], "10.00")
        self.assertEqual(row["refunds"], "3.00")
        self.assertEqual(row["absorbed humanitix fees"], "2.00")

    def test_receipt_text_parser_joins_wrapped_payout_reference(self):
        row = parse_humanitix_payout_receipt_text(
            self.RECEIPT_TEXT.replace(
                "Reference: HPVYXE2PRN",
                "Reference: HPMP JAD8U6",
            )
        )

        self.assertEqual(row["payout reference"], "HPMPJAD8U6")

    def test_receipt_parser_excludes_earnings_paid_outside_humanitix(self):
        row = parse_humanitix_payout_receipt_text(
            self.RECEIPT_TEXT.replace(
                "Ticket sales $100.00",
                "Ticket sales $120.00",
            )
        )

        self.assertEqual(row["reported ticket sales"], "120.00")
        self.assertEqual(row["non-humanitix earnings excluded"], "20.00")
        self.assertEqual(row["ticket sales"], "100.00")

    def test_receipt_import_preserves_global_report_payout_date(self):
        source = io.StringIO(
            "Payout Reference,Payout Date,Currency,Payout Amount,Event ID,Event Name\n"
            "HPVYXE2PRN,2025-03-06,AUD,110.00,event-medhack,Pitch Night: MedHack\n"
        )
        import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )

        payout = import_humanitix_payout_receipt_text(
            organization=self.organization,
            connection=self.humanitix_connection,
            text=self.RECEIPT_TEXT,
        )

        self.assertEqual(payout.payout_date.isoformat(), "2025-03-06")
        self.assertEqual(
            payout.source_payload["receipt_processed_date"],
            "2025-03-04",
        )

    def test_receipt_replaces_net_only_line_and_builds_ready_preview(self):
        source = io.StringIO(
            "Payout Reference,Payout Date,Currency,Payout Amount,Event ID,Event Name\n"
            "HPVYXE2PRN,2025-03-04,AUD,110.00,event-medhack,Pitch Night: MedHack\n"
        )
        net_only = import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )[0]
        self.assertEqual(
            set(net_only.lines.values_list("component", flat=True)),
            {HumanitixPayoutLine.COMPONENT_NET_PAYOUT},
        )

        payout = import_humanitix_payout_receipt_text(
            organization=self.organization,
            connection=self.humanitix_connection,
            text=self.RECEIPT_TEXT,
        )
        preview = build_humanitix_xero_preview(payout)

        self.assertTrue(preview["ready"], preview["errors"])
        self.assertEqual(preview["line_total"], "110.00")
        self.assertEqual(
            set(payout.lines.values_list("component", flat=True)),
            {
                HumanitixPayoutLine.COMPONENT_TICKET_SALES,
                HumanitixPayoutLine.COMPONENT_ADD_ONS,
                HumanitixPayoutLine.COMPONENT_DONATIONS,
                HumanitixPayoutLine.COMPONENT_REFUNDS,
                HumanitixPayoutLine.COMPONENT_ABSORBED_FEES,
            },
        )
        self.assertEqual(set(payout.lines.values_list("event", flat=True)), {self.event.id})

    @patch("integrations.services.humanitix_payouts.import_humanitix_payout_receipt_text")
    @patch("integrations.services.humanitix_payouts.PdfReader")
    def test_receipt_pdf_buffers_non_seekable_stream(self, mock_reader, mock_import_text):
        class NonSeekableStream(io.BytesIO):
            def seekable(self):
                return False

            def seek(self, *args, **kwargs):
                raise io.UnsupportedOperation("not seekable")

        page = MagicMock()
        page.extract_text.return_value = self.RECEIPT_TEXT
        mock_reader.return_value.pages = [page]
        expected = MagicMock()
        mock_import_text.return_value = expected

        payout = import_humanitix_payout_receipt_pdf(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=NonSeekableStream(b"%PDF receipt"),
        )

        self.assertIs(payout, expected)
        buffered_stream = mock_reader.call_args.args[0]
        self.assertIsInstance(buffered_stream, io.BytesIO)
        mock_import_text.assert_called_once_with(
            organization=self.organization,
            connection=self.humanitix_connection,
            text=self.RECEIPT_TEXT,
        )

    @patch("integrations.services.humanitix_payouts.extract_humanitix_payout_receipt_text")
    def test_receipt_bundle_imports_expected_references_atomically(self, mock_extract):
        second_receipt = self.RECEIPT_TEXT.replace(
            "HPVYXE2PRN",
            "HPSECOND01",
        )
        mock_extract.side_effect = [self.RECEIPT_TEXT, second_receipt]
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("receipt_HPVYXE2PRN.pdf", b"%PDF first")
            archive.writestr("receipt_HPSECOND01.pdf", b"%PDF second")

        payouts = import_humanitix_payout_receipt_bundle(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=bundle.getvalue(),
            expected_references={"HPVYXE2PRN", "HPSECOND01"},
        )

        self.assertEqual(
            {payout.payout_reference for payout in payouts},
            {"HPVYXE2PRN", "HPSECOND01"},
        )
        self.assertEqual(
            set(
                HumanitixPayout.objects.filter(
                    payout_reference__in={"HPVYXE2PRN", "HPSECOND01"}
                ).values_list("status", flat=True)
            ),
            {HumanitixPayout.STATUS_READY},
        )

    @patch("integrations.services.humanitix_payouts.extract_humanitix_payout_receipt_text")
    def test_receipt_bundle_rejects_missing_reference_before_database_writes(
        self,
        mock_extract,
    ):
        mock_extract.return_value = self.RECEIPT_TEXT
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("receipt_HPVYXE2PRN.pdf", b"%PDF first")

        with self.assertRaisesRegex(
            HumanitixPayoutImportError,
            "missing HPSECOND01",
        ):
            import_humanitix_payout_receipt_bundle(
                organization=self.organization,
                connection=self.humanitix_connection,
                source=bundle.getvalue(),
                expected_references={"HPVYXE2PRN", "HPSECOND01"},
            )

        self.assertFalse(
            HumanitixPayout.objects.filter(
                payout_reference__in={"HPVYXE2PRN", "HPSECOND01"}
            ).exists()
        )

    @patch("integrations.services.humanitix_payouts.extract_humanitix_payout_receipt_text")
    def test_receipt_bundle_rejects_receipts_when_expected_set_is_empty(
        self,
        mock_extract,
    ):
        mock_extract.return_value = self.RECEIPT_TEXT
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("receipt_HPVYXE2PRN.pdf", b"%PDF first")

        with self.assertRaisesRegex(
            HumanitixPayoutImportError,
            "unexpected HPVYXE2PRN",
        ):
            import_humanitix_payout_receipt_bundle(
                organization=self.organization,
                connection=self.humanitix_connection,
                source=bundle.getvalue(),
                expected_references=set(),
            )

    @patch(
        "integrations.management.commands.import_humanitix_receipts."
        "import_humanitix_payout_receipt_bundle"
    )
    def test_receipt_bundle_command_allows_zip_without_expected_manifest(
        self,
        mock_import_bundle,
    ):
        mock_import_bundle.return_value = []
        output = io.StringIO()
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"ZIP bundle"))

        with patch(
            "integrations.management.commands.import_humanitix_receipts.sys.stdin",
            new=fake_stdin,
        ):
            call_command(
                "import_humanitix_receipts",
                "--zip-stdin",
                "--domain",
                "mlai.au",
                stdout=output,
            )

        self.assertIsNone(mock_import_bundle.call_args.kwargs["expected_references"])

    @patch(
        "integrations.management.commands.import_humanitix_receipts."
        "import_humanitix_payout_receipt_bundle"
    )
    def test_receipt_bundle_command_requires_all_current_net_only_references(
        self,
        mock_import_bundle,
    ):
        source = io.StringIO(
            "Payout Reference,Payout Date,Currency,Payout Amount,Event ID,Event Name\n"
            "HPVYXE2PRN,2025-03-04,AUD,110.00,event-medhack,Pitch Night: MedHack\n"
        )
        import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )
        mock_import_bundle.return_value = []
        output = io.StringIO()
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(b"ZIP bundle"))

        with patch(
            "integrations.management.commands.import_humanitix_receipts.sys.stdin",
            new=fake_stdin,
        ):
            call_command(
                "import_humanitix_receipts",
                "--zip-stdin",
                "--require-all-net-only",
                "--domain",
                "mlai.au",
                stdout=output,
            )

        self.assertEqual(
            mock_import_bundle.call_args.kwargs["expected_references"],
            ["HPVYXE2PRN"],
        )

    def test_global_payout_export_links_short_event_id_by_name_and_parses_date_paid(self):
        self.event.start_at = datetime(2025, 3, 12, 6, 45, tzinfo=timezone.utc)
        self.event.timezone_name = "Australia/Melbourne"
        self.event.save(update_fields=["start_at", "timezone_name", "updated_at"])
        HumanitixEvent.objects.create(
            organization=self.organization,
            connection=self.humanitix_connection,
            external_event_id="event-medhack-older",
            event_name="pitch night medhack",
            start_at=datetime(2024, 10, 2, 8, 0, tzinfo=timezone.utc),
            timezone_name="Australia/Melbourne",
            currency="AUD",
        )
        source = io.StringIO(
            "Event ID,Event Name,Event Date,Payout reference,Invoice note,Date Paid,"
            "Paid to account,Payout Amount\n"
            'MEDHACK01,Pitch Night: MedHack,12/03/2025,HPGLOBAL01,,'
            '"Wed 4th Mar 2025, 2:02 pm AEDT",06XXXX-XXXXX550,$2343.80\n'
        )

        payout = import_payout_csv(
            organization=self.organization,
            connection=self.humanitix_connection,
            source=source,
        )[0]
        line = payout.lines.get()

        self.assertEqual(payout.payout_date, date(2025, 3, 4))
        self.assertEqual(line.event, self.event)
        self.assertEqual(line.external_event_id, self.event.external_event_id)
        self.assertEqual(line.component, HumanitixPayoutLine.COMPONENT_NET_PAYOUT)
        self.assertTrue(
            any("linked by exact event name and date" in warning for warning in payout.warnings)
        )
