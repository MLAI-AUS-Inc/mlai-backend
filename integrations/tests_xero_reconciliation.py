from datetime import datetime, timedelta, timezone
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from organizations.models import Organization
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    ReconciliationMapping,
    ReconciliationProfile,
    StripePayoutReconciliation,
)
from integrations.services.reconciliation import (
    DEFAULT_STRIPE_API_VERSION,
    ReconciliationReportService,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    build_xero_preview,
    persist_report,
    post_xero_bank_transaction,
)
from integrations.tests_reconciliation import FakeSession
from roo.models import PointsAdmin


User = get_user_model()


class StripeAttributionTests(SimpleTestCase):
    def test_malformed_api_version_env_is_repaired(self):
        service = ReconciliationReportService(
            stripe_api_key="rk_test",
            stripe_api_version='os.environ.get("STRIPE_API_VERSION")',
        )
        self.assertEqual(service.stripe_api_version, DEFAULT_STRIPE_API_VERSION)

    def test_payment_balance_transaction_with_luma_metadata_is_revenue(self):
        def handler(path, params):
            if path == "/v1/payouts":
                return {"data": [{"id": "po_pay", "amount": 9700, "currency": "aud", "arrival_date": 1_780_600_000, "status": "paid"}], "has_more": False}
            if path == "/v1/balance_transactions":
                return {"data": [{
                    "id": "bt_pay",
                    "type": "payment",
                    "amount": 10000,
                    "fee": 300,
                    "net": 9700,
                    "currency": "aud",
                    "source": {"id": "py_1", "description": "Luma Night", "metadata": {"event_api_id": "evt_1", "email": "guest@example.com"}},
                }], "has_more": False}
            raise AssertionError(path)

        report = ReconciliationReportService(
            stripe_api_key="rk_test",
            base_url="https://stripe.test",
            session=FakeSession(handler),
        ).build_report(
            since=datetime(2026, 6, 1, tzinfo=timezone.utc),
            until=datetime(2026, 7, 1, tzinfo=timezone.utc),
            include_workbook=False,
        )
        payout = report["payouts"][0]
        self.assertEqual(payout["gross_cents"], 10000)
        self.assertEqual(payout["revenue_groups"][0]["source_type"], "luma_event")
        self.assertEqual(payout["revenue_groups"][0]["source_id"], "evt_1")
        self.assertEqual(report["unmatched_charge_count"], 0)

    def test_invoice_payment_and_refund_are_attributed_through_stripe_objects(self):
        def handler(path, params):
            if path == "/v1/payouts":
                return {"data": [{"id": "po_invoice", "amount": 9200, "currency": "aud", "arrival_date": 1_780_600_000, "status": "paid"}], "has_more": False}
            if path == "/v1/balance_transactions":
                return {"data": [
                    {"id": "bt_payment", "type": "payment", "amount": 10000, "fee": 300, "net": 9700, "currency": "aud", "source": {"id": "py_1", "payment_intent": "pi_invoice", "metadata": {}}},
                    {"id": "bt_refund", "type": "refund", "amount": -500, "fee": 0, "net": -500, "currency": "aud", "source": {"id": "re_1", "payment_intent": "pi_luma"}},
                ], "has_more": False}
            if path == "/v1/invoice_payments":
                self.assertEqual(params["payment[payment_intent]"], "pi_invoice")
                return {"data": [{"id": "ip_1", "invoice": "in_1"}], "has_more": False}
            if path == "/v1/invoices/in_1":
                return {"id": "in_1", "lines": {"data": [{"description": "MLAI Studio Pro"}]}}
            if path == "/v1/payment_intents/pi_luma":
                return {"id": "pi_luma", "description": "Luma Night", "metadata": {"event_api_id": "evt_2"}}
            raise AssertionError(path)

        report = ReconciliationReportService(
            stripe_api_key="rk_test",
            base_url="https://stripe.test",
            session=FakeSession(handler),
        ).build_report(
            since=datetime(2026, 6, 1, tzinfo=timezone.utc),
            until=datetime(2026, 7, 1, tzinfo=timezone.utc),
            include_workbook=False,
        )
        payout = report["payouts"][0]
        self.assertEqual(payout["revenue_groups"][0]["source_type"], "stripe_invoice")
        self.assertEqual(payout["revenue_groups"][0]["source_id"], "in_1")
        self.assertEqual(payout["refunds"][0]["source_id"], "evt_2")
        self.assertFalse(any("Tie-out mismatch" in warning for warning in payout["warnings"]))


class XeroReconciliationWorkflowTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="finance@example.com", slack_id="UFIN")
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            external_account_id="tenant-1",
            scopes=["offline_access", "accounting.banktransactions"],
        )
        self.profile = ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            stripe_account_id="acct_main",
            xero_bank_account_id="bank-1",
            revenue_account_code="200",
            fee_account_code="404",
            refund_account_code="200",
            revenue_tax_type="EXEMPTOUTPUT",
            fee_tax_type="INPUT",
            refund_tax_type="EXEMPTOUTPUT",
        )
        self.mapping = ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type="luma_event",
            source_id="evt_1",
            source_label="Luma Night",
            accounting_treatment="revenue",
            event_tracking_option_name="Luma Night",
        )
        self.report = {
            "payouts": [{
                "payout_id": "po_ledger",
                "arrival_date": "2026-07-10",
                "currency": "AUD",
                "deposit_cents": 9200,
                "gross_cents": 10000,
                "stripe_fee_cents": 300,
                "standalone_fee_cents": 0,
                "revenue_groups": [{
                    "source_type": "luma_event",
                    "source_id": "evt_1",
                    "source_label": "Luma Night",
                    "event_name": "Luma Night",
                    "gross_cents": 10000,
                    "stripe_fee_cents": 300,
                }],
                "refunds": [{
                    "id": "bt_ref",
                    "source_type": "luma_event",
                    "source_id": "evt_1",
                    "source_label": "Luma Night",
                    "net_cents": -500,
                }],
                "warnings": ["1 non-charge transaction(s) (refunds/adjustments) in this payout."],
            }],
        }

    def test_persistent_ledger_is_idempotent_and_preview_ties_exactly(self):
        first = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        second = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StripePayoutReconciliation.objects.count(), 1)
        preview = build_xero_preview(second)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["line_total_cents"], 9200)
        self.assertEqual([line["UnitAmount"] for line in preview["xero_payload"]["LineItems"]], [100.0, -3.0, -5.0])
        self.assertEqual(preview["xero_payload"]["Reference"], "po_ledger")
        self.assertTrue(preview["human_reconciliation_required"])

    def test_preview_blocks_missing_mapping_and_missing_write_scope(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        self.mapping.delete()
        preview = build_xero_preview(record)
        self.assertFalse(preview["ready"])
        self.assertTrue(any("Map luma_event:evt_1" in error for error in preview["errors"]))
        self.mapping = ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type="luma_event",
            source_id="evt_1",
            accounting_treatment="revenue",
            event_tracking_option_name="Luma Night",
        )
        self.connection.scopes = ["accounting.invoices.read"]
        self.connection.save(update_fields=["scopes", "updated_at"])
        preview = build_xero_preview(record)
        self.assertFalse(preview["ready"])
        self.assertTrue(any("accounting.banktransactions" in error for error in preview["errors"]))

    def test_explicit_post_is_idempotent_and_records_xero_id(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        empty = Mock()
        empty.json.return_value = {"BankTransactions": []}
        empty.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {"BankTransactions": [{"BankTransactionID": "xero-bt-1", "HasErrors": False}]}
        created.raise_for_status.return_value = None
        with patch("integrations.services.xero_reconciliation.http_client.get", return_value=empty) as get_mock, patch(
            "integrations.services.xero_reconciliation.http_client.put", return_value=created
        ) as put_mock:
            posted = post_xero_bank_transaction(record, approved_by_slack_id="UFIN")
            again = post_xero_bank_transaction(posted, approved_by_slack_id="UFIN")
        self.assertEqual(posted.xero_bank_transaction_id, "xero-bt-1")
        self.assertEqual(again.xero_bank_transaction_id, "xero-bt-1")
        self.assertEqual(posted.status, "posted")
        self.assertEqual(get_mock.call_count, 1)
        self.assertEqual(put_mock.call_count, 1)
        body = put_mock.call_args.kwargs["json"]
        self.assertEqual(body["BankTransactions"][0]["Reference"], "po_ledger")

    def test_post_rejects_unready_payout_without_network_call(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        self.mapping.delete()
        with patch("integrations.services.xero_reconciliation.http_client.put") as put_mock:
            with self.assertRaises(ReconciliationValidationError):
                post_xero_bank_transaction(record, approved_by_slack_id="UFIN")
        put_mock.assert_not_called()

    def test_standalone_fee_requires_project_tracking(self):
        report = deepcopy(self.report)
        payout = report["payouts"][0]
        payout["deposit_cents"] = 9100
        payout["standalone_fee_cents"] = 100
        record = persist_report(organization=self.organization, report=report, stripe_account_id="acct_main")[0]
        preview = build_xero_preview(record)
        self.assertFalse(preview["ready"])
        self.assertTrue(any("standalone Stripe fees" in error for error in preview["errors"]))

        self.profile.standalone_fee_project_option_name = "Stripe General"
        self.profile.save(update_fields=["standalone_fee_project_option_name", "updated_at"])
        preview = build_xero_preview(record)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["line_total_cents"], 9100)
        self.assertEqual(preview["xero_payload"]["LineItems"][2]["Tracking"][0]["Option"], "Stripe General")


class ReconciliationWorkflowApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        PointsAdmin.objects.create(slack_user_id="UADMIN", role="admin", is_active=True)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_profile_and_mapping_configuration_endpoints(self, _permission):
        profile_response = self.client.get(
            reverse("reconciliation_profile"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(profile_response.status_code, status.HTTP_200_OK)
        self.assertFalse(profile_response.data["profile"]["xero_write_scope"])

        mapping_response = self.client.put(
            reverse("reconciliation_mappings"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "source_type": "luma_event",
                "source_id": "evt_api",
                "source_label": "API Event",
                "accounting_treatment": "revenue",
                "event_tracking_option_name": "API Event",
            },
            format="json",
        )
        self.assertEqual(mapping_response.status_code, status.HTTP_200_OK)
        self.assertEqual(mapping_response.data["mappings"][0]["accounting_treatment"], "revenue")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_post_endpoint_requires_explicit_confirmation(self, _permission):
        StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po_api",
            amount_cents=100,
            currency="AUD",
        )
        response = self.client.post(
            reverse("reconciliation_payout_post", kwargs={"payout_id": "po_api"}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
