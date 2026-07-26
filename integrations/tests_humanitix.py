from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase

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
    build_humanitix_xero_preview,
    import_payout_csv,
    post_humanitix_xero_bank_transaction,
)
from organizations.models import Organization


User = get_user_model()


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
        self.assertEqual(summary["gateway_breakdown"]["invoice"]["classification"], "offline")
        self.assertEqual(summary["gross_sales"], Decimal("175.00"))


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

    @patch("integrations.services.humanitix_payouts.http_client.put")
    @patch("integrations.services.humanitix_payouts.http_client.get")
    def test_confirmed_post_is_idempotent_and_resolves_tracking(self, mock_get, mock_put):
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

    def test_global_payout_export_links_short_event_id_by_name_and_parses_date_paid(self):
        source = io.StringIO(
            "Event ID,Event Name,Event Date,Payout reference,Invoice note,Date Paid,"
            "Paid to account,Payout Amount\n"
            'MEDHACK01,Pitch Night: MedHack,27/02/2025,HPGLOBAL01,,'
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
            any("linked by exact event name" in warning for warning in payout.warnings)
        )
