from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import ANY, Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from organizations.models import Organization
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnection,
    ExternalServiceProvider,
    GoogleConnection,
    HumanitixEvent,
    ReconciliationMapping,
    ReconciliationDecision,
    ReconciliationPartyIdentity,
    ReconciliationProfile,
    ReconciliationRule,
    ReconciliationSuggestion,
    StripePayoutReconciliation,
    XeroStatementLineSnapshot,
    XeroStatementPosting,
    XeroStatementScan,
    XeroStatementSuggestion,
)
from startup_updates.models import (
    GmailMessageArtifact,
    LinearProjectArtifact,
    LinearProjectMemberArtifact,
    LinearProjectSelection,
    LumaEventSelection,
    SlackMessageArtifact,
)
from integrations.services.reconciliation import (
    DEFAULT_STRIPE_API_VERSION,
    ReconciliationReportService,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    XeroPostingError,
    build_event_cashflow_validation,
    build_event_revenue_rollup,
    build_xero_correction_preview,
    build_xero_preview,
    ensure_xero_tracking_options,
    persist_report,
    post_xero_bank_transaction,
)
from integrations.services.reconciliation_context import (
    approve_reconciliation_suggestion,
    build_reconciliation_enrichment_context,
    save_reconciliation_suggestions,
)
from integrations.services.reconciliation_catalogs import (
    build_reconciliation_catalog_status,
)
from integrations.services.xero_statement_reconciliation import (
    STATEMENT_CAPTURE_SOURCE_BROWSER,
    build_statement_reconciliation_context,
    canonical_bank_account_id,
    format_statement_browser_comment,
    import_xero_statement_lines,
    merchant_key,
    save_statement_suggestions,
    select_current_statement_capture,
    serialize_statement_suggestion,
)
from integrations.services.xero_statement_posting import (
    _resolved_tracking,
    build_statement_posting_preview,
    execute_statement_posting,
)
from integrations.services.external_connectors import _upsert_xero_payments
from integrations.tests_reconciliation import FakeSession
from roo.models import PointsAdmin
from workflow_runs.models import (
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryStepStatus,
)


User = get_user_model()


class StripeAttributionTests(SimpleTestCase):
    def test_browser_comment_is_short_and_puts_confidence_last(self):
        comment = format_statement_browser_comment(
            description="AI reconciliation draft (92% confidence): uber trip",
            review_note="Human approval required.",
            confidence=0.92,
        )
        self.assertEqual(comment, "Uber trip. Confidence: 92%.")

    def test_browser_comment_skips_generic_text_and_appends_confidence_once(self):
        comment = format_statement_browser_comment(
            description="Unreconciled bank statement line. Confidence: 20%.",
            review_note="Likely for Demo Night because the venue appears on the receipt.",
            confidence=0.81,
        )
        self.assertEqual(
            comment,
            "Likely for Demo Night because the venue appears on the receipt. Confidence: 81%.",
        )
        self.assertEqual(comment.count("Confidence:"), 1)

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
            if path == "/v1/payment_intents/pi_invoice":
                return {"id": "pi_invoice", "metadata": {}}
            if path == "/v1/invoices/in_1":
                return {
                    "id": "in_1",
                    "subscription": "sub_1",
                    "lines": {"data": [{
                        "description": "MLAI Studio Pro",
                        "price": {"product": "prod_studio"},
                    }]},
                }
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
        self.assertEqual(payout["revenue_groups"][0]["stripe_invoice_ids"], ["in_1"])
        self.assertEqual(
            payout["revenue_groups"][0]["stripe_invoice_payment_ids"],
            ["ip_1"],
        )
        self.assertEqual(
            payout["revenue_groups"][0]["stripe_payment_intent_ids"],
            ["pi_invoice"],
        )
        self.assertEqual(
            payout["revenue_groups"][0]["stripe_product_ids"],
            ["prod_studio"],
        )
        self.assertEqual(
            payout["revenue_groups"][0]["stripe_subscription_ids"],
            ["sub_1"],
        )
        self.assertEqual(payout["refunds"][0]["source_id"], "evt_2")
        self.assertEqual(payout["refunds"][0]["stripe_payment_intent_id"], "pi_luma")
        self.assertFalse(any("Tie-out mismatch" in warning for warning in payout["warnings"]))

    def test_product_metadata_preserves_immutable_product_lineage(self):
        def handler(path, params):
            if path == "/v1/payouts":
                return {
                    "data": [{
                        "id": "po_product",
                        "amount": 9700,
                        "currency": "aud",
                        "arrival_date": 1_780_600_000,
                        "status": "paid",
                    }],
                    "has_more": False,
                }
            if path == "/v1/balance_transactions":
                return {
                    "data": [{
                        "id": "bt_product",
                        "type": "payment",
                        "amount": 10000,
                        "fee": 300,
                        "net": 9700,
                        "currency": "aud",
                        "source": {
                            "id": "py_product",
                            "description": "Studio subscription",
                            "metadata": {
                                "product_id": "prod_studio",
                                "subscription_id": "sub_studio",
                            },
                        },
                    }],
                    "has_more": False,
                }
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
        group = report["payouts"][0]["revenue_groups"][0]
        self.assertEqual(group["source_type"], "stripe_product")
        self.assertEqual(group["source_id"], "prod_studio")
        self.assertEqual(group["stripe_product_ids"], ["prod_studio"])
        self.assertEqual(group["stripe_subscription_ids"], ["sub_studio"])


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
            account_label="MLAI Tenant",
            scopes=[
                "offline_access",
                "accounting.banktransactions",
                "accounting.payments",
                "accounting.settings",
                "accounting.contacts.read",
            ],
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
            event_tracking_category_id="event-category-1",
            event_tracking_category_name="Event Name",
            project_tracking_category_id="project-category-1",
            project_tracking_category_name="Project Name",
        )
        self.mapping = ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type="luma_event",
            source_id="evt_1",
            source_label="Luma Night",
            accounting_treatment="revenue",
            event_tracking_option_id="event-option-1",
            event_tracking_option_name="Luma Night",
        )
        self.luma_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LUMA,
            user=self.user,
            organization=self.organization,
            access_token="luma-token",
            external_account_id="luma-main",
        )
        self.linear_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="linear-token",
            external_account_id="linear-main",
        )
        LumaEventSelection.objects.create(
            connection=self.luma_connection,
            user=self.user,
            organization=self.organization,
            event_id="evt_1",
            event_name="Luma Night",
            event_url="https://lu.ma/evt_1",
            registration_count=18,
            checked_in_count=14,
            selected=True,
        )
        LinearProjectArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            linear_project_id="lin_event_1",
            name="Luma Night",
            description="Public event delivery project",
        )
        LinearProjectArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            linear_project_id="lin_project_1",
            name="Community Events",
            description="Parent project for community event delivery",
        )
        LinearProjectSelection.objects.create(
            connection=self.linear_connection,
            user=self.user,
            organization=self.organization,
            linear_project_id="lin_event_1",
            project_name="Luma Night",
            selected=True,
        )
        LinearProjectSelection.objects.create(
            connection=self.linear_connection,
            user=self.user,
            organization=self.organization,
            linear_project_id="lin_project_1",
            project_name="Community Events",
            selected=True,
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

    @staticmethod
    def _active_xero_bank_accounts():
        return [{
            "AccountID": "bank-1",
            "Name": "Operating",
            "Type": "BANK",
            "Status": "ACTIVE",
        }]

    @staticmethod
    def _active_event_tracking_categories_response():
        response = Mock()
        response.json.return_value = {
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Event Name",
                "Status": "ACTIVE",
                "Options": [{
                    "TrackingOptionID": "event-option-1",
                    "Name": "Luma Night",
                    "Status": "ACTIVE",
                }],
            }],
        }
        response.raise_for_status.return_value = None
        return response

    def _capture_stripe_statement_line(self, record):
        raw_lines = [{
            "statement_line_id": f"stripe-{record.payout_id}",
            "date": record.arrival_date.strftime("%d %b %Y"),
            "narration": f"Stripe payout {record.payout_id}",
            "direction": "credit",
            "amount": f"{record.amount_cents / 100:.2f}",
            "currency": record.currency,
        }]
        account_source_sha256 = hashlib.sha256(
            json.dumps(
                raw_lines,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            complete_scan=True,
            capture_metadata={
                "schema_version": 2,
                "capture_source": STATEMENT_CAPTURE_SOURCE_BROWSER,
                "capture_id": f"stripe-capture-{record.pk}",
                "scan_id": f"stripe-scan-{record.pk}",
                "account_source_sha256": account_source_sha256,
                "report_format": "xero_bank_reconciliation_dom",
                "tenant_id": "tenant-1",
                "organisation_name": "MLAI Tenant",
                "bank_account_label": "Operating",
                "account_position": 1,
                "account_count": 1,
                "active_bank_account_ids": ["bank-1"],
                "all_accounts_requested": True,
                "full_organisation_coverage_confirmed": True,
                "date_range_confirmed": True,
                "derived_complete": True,
                "blocking_reasons": [],
            },
            lines=raw_lines,
        )[0]
        return {
            "statement_line_id": line.statement_line_id,
            "bank_account_id": line.bank_account_id,
            "statement_source_hash": line.source_hash,
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

    def test_correction_preview_identifies_reconciled_legacy_net_transaction(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        preview = build_xero_correction_preview(
            record,
            bank_transactions=[{
                "BankTransactionID": "legacy-net-1",
                "Type": "RECEIVE",
                "Reference": "po_ledger",
                "DateString": "2026-07-10",
                "Total": 92.00,
                "IsReconciled": True,
                "BankAccount": {"AccountID": "bank-1"},
                "LineItems": [{
                    "Description": "Stripe payout",
                    "Quantity": 1,
                    "UnitAmount": 92.00,
                    "AccountCode": "200",
                    "TaxType": "EXEMPTOUTPUT",
                    "Tracking": [],
                }],
            }],
        )
        self.assertEqual(preview["classification"], "legacy_net_only")
        self.assertEqual(preview["recommended_action"], "unreconcile_then_replace")
        self.assertTrue(preview["requires_manual_unreconcile"])
        self.assertFalse(preview["automatic_action_allowed"])
        self.assertEqual(preview["differences"]["current_line_count"], 1)
        self.assertEqual(preview["differences"]["proposed_line_count"], 3)

    def test_correction_preview_recognises_exact_split_transaction(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        proposed = build_xero_preview(record)["xero_payload"]
        preview = build_xero_correction_preview(
            record,
            bank_transactions=[{
                "BankTransactionID": "split-1",
                "Type": "RECEIVE",
                "Reference": "po_ledger",
                "DateString": "2026-07-10",
                "Total": 92.00,
                "IsReconciled": True,
                "BankAccount": {"AccountID": "bank-1"},
                "LineItems": proposed["LineItems"],
            }],
        )
        self.assertEqual(preview["classification"], "already_correct")
        self.assertEqual(preview["recommended_action"], "no_action")
        self.assertTrue(preview["differences"]["line_items_match"])

    def test_event_revenue_rollup_combines_stripe_and_luma_evidence(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        row = build_event_revenue_rollup([record])[0]
        self.assertEqual(row["event_name"], "Luma Night")
        self.assertEqual(row["luma_registration_count"], 18)
        self.assertEqual(row["luma_checked_in_count"], 14)
        self.assertEqual(row["stripe_charge_count"], 0)
        self.assertEqual(row["gross_cents"], 10000)
        self.assertEqual(row["refunds_cents"], -500)
        self.assertEqual(row["stripe_fee_cents"], 300)
        self.assertEqual(row["net_cash_contribution_cents"], 9200)

    def test_event_cashflow_validation_excludes_stripe_payout_and_flags_loss(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        revenue = build_event_revenue_rollup([record])
        validation = build_event_cashflow_validation(
            event_revenue=revenue,
            bank_transactions=[
                {
                    "BankTransactionID": "stripe-payout",
                    "Type": "RECEIVE",
                    "Status": "AUTHORISED",
                    "DateString": "2026-05-01",
                    "LineItems": [{
                        "UnitAmount": 92.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "TrackingCategoryID": "event-category-1",
                            "Name": "Event Name",
                            "Option": "Luma Night",
                        }],
                    }],
                },
                {
                    "BankTransactionID": "sponsor-income",
                    "Type": "RECEIVE",
                    "Status": "AUTHORISED",
                    "DateString": "2026-05-02",
                    "LineItems": [{
                        "UnitAmount": 100.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "TrackingCategoryID": "event-category-1",
                            "Name": "Event Name",
                            "Option": "Luma Night",
                        }],
                    }],
                },
                {
                    "BankTransactionID": "event-cost",
                    "Type": "SPEND",
                    "Status": "AUTHORISED",
                    "DateString": "2026-05-03",
                    "LineItems": [{
                        "UnitAmount": 250.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "TrackingCategoryID": "event-category-1",
                            "Name": "Event Name",
                            "Option": "Luma Night",
                        }],
                    }],
                },
                {
                    "BankTransactionID": "unmatched-cost",
                    "Type": "SPEND",
                    "Status": "AUTHORISED",
                    "DateString": "2026-05-04",
                    "LineItems": [{
                        "UnitAmount": 30.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "Name": "Event Name",
                            "Option": "Costs Only Event",
                        }],
                    }],
                },
                {
                    "BankTransactionID": "prior-period-cost",
                    "Type": "SPEND",
                    "Status": "AUTHORISED",
                    "DateString": "2025-12-31",
                    "LineItems": [{
                        "UnitAmount": 999.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "Name": "Event Name",
                            "Option": "Luma Night",
                        }],
                    }],
                },
                {
                    "BankTransactionID": "humanitix-payout-transfer",
                    "Type": "RECEIVE",
                    "Status": "AUTHORISED",
                    "DateString": "2026-05-05",
                    "LineItems": [{
                        "UnitAmount": 500.00,
                        "Quantity": 1,
                        "Tracking": [{
                            "Name": "Event Name",
                            "Option": "Luma Night",
                        }],
                    }],
                },
            ],
            payout_previews=[{
                "existing_transactions": [{
                    "bank_transaction_id": "stripe-payout",
                }],
            }],
            profile=self.profile,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 30),
            excluded_transfer_transaction_ids={"humanitix-payout-transfer"},
        )
        row = validation["rows"][0]
        self.assertEqual(row["xero_other_income_cents"], 10000)
        self.assertEqual(row["xero_cost_cents"], 25000)
        self.assertEqual(row["xero_current_stripe_net_cents"], 9200)
        self.assertEqual(row["xero_stripe_variance_cents"], 0)
        self.assertEqual(row["xero_stripe_coding_status"], "mismatch")
        self.assertIn("xero_stripe_coding_incomplete", row["validation_flags"])
        self.assertEqual(row["estimated_cashflow_cents"], -5800)
        self.assertEqual(row["profitability_status"], "negative")
        self.assertIn("negative_cashflow", row["validation_flags"])
        self.assertEqual(validation["negative_count"], 1)
        self.assertEqual(validation["period_start"], "2026-01-01")
        self.assertEqual(validation["period_end"], "2026-06-30")
        self.assertEqual(
            {
                item["bank_transaction_id"]
                for item in validation["excluded_payout_transfer_lines"]
            },
            {"stripe-payout", "humanitix-payout-transfer"},
        )
        self.assertEqual(
            {item["bank_transaction_id"] for item in row["xero_lines"]},
            {"sponsor-income", "event-cost"},
        )
        self.assertEqual(
            validation["unmatched_xero_tracking"][0]["event_name"],
            "Costs Only Event",
        )
        self.assertEqual(
            validation["unmatched_xero_tracking"][0]["xero_cost_cents"],
            3000,
        )

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
        binding = self._capture_stripe_statement_line(record)
        empty = Mock()
        empty.json.return_value = {"BankTransactions": []}
        empty.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {"BankTransactions": [{"BankTransactionID": "xero-bt-1", "HasErrors": False}]}
        created.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            return_value=self._active_xero_bank_accounts(),
        ) as accounts_mock, patch(
            "integrations.services.xero_reconciliation.http_client.get",
            side_effect=[self._active_event_tracking_categories_response(), empty],
        ) as get_mock, patch(
            "integrations.services.xero_reconciliation.http_client.put", return_value=created
        ) as put_mock:
            posted = post_xero_bank_transaction(
                record,
                approved_by_slack_id="UFIN",
                **binding,
            )
            again = post_xero_bank_transaction(
                posted,
                approved_by_slack_id="UFIN",
                **binding,
            )
        self.assertEqual(posted.xero_bank_transaction_id, "xero-bt-1")
        self.assertEqual(again.xero_bank_transaction_id, "xero-bt-1")
        self.assertEqual(posted.status, "posted")
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(put_mock.call_count, 1)
        self.assertEqual(accounts_mock.call_count, 2)
        body = put_mock.call_args.kwargs["json"]
        self.assertEqual(body["BankTransactions"][0]["Reference"], "po_ledger")

    def test_explicit_post_refuses_to_accept_existing_legacy_net_transaction(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        binding = self._capture_stripe_statement_line(record)
        existing = Mock()
        existing.json.return_value = {
            "BankTransactions": [{
                "BankTransactionID": "legacy-net-1",
                "Reference": "po_ledger",
                "Total": 92.00,
                "IsReconciled": True,
                "LineItems": [{
                    "Quantity": 1,
                    "UnitAmount": 92.00,
                    "AccountCode": "200",
                    "TaxType": "EXEMPTOUTPUT",
                }],
            }]
        }
        existing.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            return_value=self._active_xero_bank_accounts(),
        ), patch(
            "integrations.services.xero_reconciliation.http_client.get",
            side_effect=[self._active_event_tracking_categories_response(), existing],
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                ReconciliationValidationError,
                "already exists for this payout",
            ):
                post_xero_bank_transaction(
                    record,
                    approved_by_slack_id="UFIN",
                    **binding,
                )
        put_mock.assert_not_called()

    def test_explicit_post_replaces_deleted_xero_transaction(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        binding = self._capture_stripe_statement_line(record)
        deleted = Mock()
        deleted.json.return_value = {
            "BankTransactions": [{
                "BankTransactionID": "deleted-legacy-1",
                "Reference": "po_ledger",
                "Status": "DELETED",
            }]
        }
        deleted.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {
            "BankTransactions": [{
                "BankTransactionID": "replacement-1",
                "HasErrors": False,
            }]
        }
        created.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            return_value=self._active_xero_bank_accounts(),
        ), patch(
            "integrations.services.xero_reconciliation.http_client.get",
            side_effect=[self._active_event_tracking_categories_response(), deleted],
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put",
            return_value=created,
        ) as put_mock:
            posted = post_xero_bank_transaction(
                record,
                approved_by_slack_id="UFIN",
                **binding,
            )
        self.assertEqual(posted.xero_bank_transaction_id, "replacement-1")
        self.assertEqual(posted.status, "posted")
        self.assertEqual(put_mock.call_count, 1)

    def test_explicit_post_creates_missing_project_tracking_option(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.event_tracking_option_name = ""
        self.mapping.project_source_type = "linear"
        self.mapping.project_source_id = "lin_project_1"
        self.mapping.project_tracking_option_name = "Community Events"
        self.mapping.save(update_fields=[
            "event_tracking_option_id",
            "event_tracking_option_name",
            "project_source_type",
            "project_source_id",
            "project_tracking_option_name",
            "updated_at",
        ])
        ReconciliationSuggestion.objects.create(
            organization=self.organization,
            payout=record,
            run_id="approved-project-option",
            source_type=self.mapping.source_type,
            source_id=self.mapping.source_id,
            allocation_mode=ReconciliationSuggestion.ALLOCATION_PROJECT,
            project_source_type="linear",
            project_source_id="lin_project_1",
            project_tracking_option_name="Community Events",
            source_hash=record.source_hash,
            status=ReconciliationSuggestion.STATUS_APPROVED,
            reviewed_at=datetime.now(timezone.utc),
        )
        categories = Mock()
        categories.json.return_value = {
            "TrackingCategories": [
                {
                    "TrackingCategoryID": "project-category-1",
                    "Name": "Project Name",
                    "Status": "ACTIVE",
                    "Options": [],
                }
            ]
        }
        categories.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {
            "Options": [{
                "TrackingOptionID": "project-option-1",
                "Name": "Community Events",
                "Status": "ACTIVE",
            }]
        }
        created.raise_for_status.return_value = None
        with patch("integrations.services.xero_reconciliation.http_client.get", return_value=categories), patch(
            "integrations.services.xero_reconciliation.http_client.put", return_value=created
        ) as put_mock:
            ensure_xero_tracking_options(record, profile=self.profile)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.project_tracking_option_id, "project-option-1")
        self.assertEqual(
            put_mock.call_args.args[0],
            "https://api.xero.com/api.xro/2.0/TrackingCategories/project-category-1/Options",
        )
        self.assertEqual(put_mock.call_args.kwargs["json"], {"Options": [{"Name": "Community Events"}]})

    def test_missing_stripe_option_requires_current_approved_suggestion(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Event Name",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })

        with patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                XeroPostingError,
                "not bound to a current approved suggestion",
            ):
                ensure_xero_tracking_options(record, profile=self.profile)

        put_mock.assert_not_called()

    def test_missing_stripe_option_rejects_unselected_canonical_event(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
        ReconciliationSuggestion.objects.create(
            organization=self.organization,
            payout=record,
            run_id="approved-event-option",
            source_type=self.mapping.source_type,
            source_id=self.mapping.source_id,
            allocation_mode=ReconciliationSuggestion.ALLOCATION_EVENT,
            event_source_type="luma",
            event_source_id="evt_1",
            event_tracking_option_name="Luma Night",
            source_hash=record.source_hash,
            status=ReconciliationSuggestion.STATUS_APPROVED,
            reviewed_at=datetime.now(timezone.utc),
        )
        LumaEventSelection.objects.filter(
            organization=self.organization,
            event_id="evt_1",
        ).update(selected=False)
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Event Name",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })

        with patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "no longer exists"):
                ensure_xero_tracking_options(record, profile=self.profile)

        put_mock.assert_not_called()

    def test_missing_stripe_option_does_not_reuse_an_older_approved_suggestion(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
        ReconciliationSuggestion.objects.create(
            organization=self.organization,
            payout=record,
            run_id="older-approved-event",
            source_type=self.mapping.source_type,
            source_id=self.mapping.source_id,
            allocation_mode=ReconciliationSuggestion.ALLOCATION_EVENT,
            event_source_type="luma",
            event_source_id="evt_1",
            event_tracking_option_name="Luma Night",
            source_hash=record.source_hash,
            status=ReconciliationSuggestion.STATUS_APPROVED,
            reviewed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        ReconciliationSuggestion.objects.create(
            organization=self.organization,
            payout=record,
            run_id="current-approved-unassigned",
            source_type=self.mapping.source_type,
            source_id=self.mapping.source_id,
            allocation_mode=ReconciliationSuggestion.ALLOCATION_UNASSIGNED,
            source_hash=record.source_hash,
            status=ReconciliationSuggestion.STATUS_APPROVED,
            reviewed_at=datetime.now(timezone.utc),
        )
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Event Name",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })

        with patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                XeroPostingError,
                "not bound to a current approved suggestion",
            ):
                ensure_xero_tracking_options(record, profile=self.profile)

        put_mock.assert_not_called()

    def test_stripe_tracking_catalog_rejects_category_name_mismatch(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Wrong Category",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })

        with patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "Wrong Category"):
                ensure_xero_tracking_options(record, profile=self.profile)

        put_mock.assert_not_called()

    def test_stripe_tracking_catalog_rejects_duplicate_option_ids(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        self.mapping.event_tracking_option_id = ""
        self.mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "event-category-1",
                "Name": "Event Name",
                "Status": "ACTIVE",
                "Options": [{
                    "TrackingOptionID": "duplicate-option",
                    "Name": "Luma Night",
                    "Status": "ACTIVE",
                }, {
                    "TrackingOptionID": "duplicate-option",
                    "Name": "Different Event",
                    "Status": "ACTIVE",
                }],
            }],
        })

        with patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                XeroPostingError,
                "more than one tracking option with ID duplicate-option",
            ):
                ensure_xero_tracking_options(record, profile=self.profile)

        put_mock.assert_not_called()

    def test_post_rejects_unready_payout_without_network_call(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        binding = self._capture_stripe_statement_line(record)
        self.mapping.delete()
        with patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            return_value=self._active_xero_bank_accounts(),
        ), patch("integrations.services.xero_reconciliation.http_client.put") as put_mock:
            with self.assertRaises(ReconciliationValidationError):
                post_xero_bank_transaction(
                    record,
                    approved_by_slack_id="UFIN",
                    **binding,
                )
        put_mock.assert_not_called()

    def test_standalone_fee_allows_blank_or_verified_project_tracking(self):
        report = deepcopy(self.report)
        payout = report["payouts"][0]
        payout["deposit_cents"] = 9100
        payout["standalone_fee_cents"] = 100
        record = persist_report(organization=self.organization, report=report, stripe_account_id="acct_main")[0]
        preview = build_xero_preview(record)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["xero_payload"]["LineItems"][2].get("Tracking"), None)

        self.profile.standalone_fee_project_option_name = "Stripe General"
        self.profile.save(update_fields=["standalone_fee_project_option_name", "updated_at"])
        preview = build_xero_preview(record)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["line_total_cents"], 9100)
        self.assertEqual(preview["xero_payload"]["LineItems"][2]["Tracking"][0]["Option"], "Stripe General")

    def test_correction_preview_ignores_deleted_transaction_and_allows_replacement(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        preview = build_xero_correction_preview(
            record,
            bank_transactions=[{
                "BankTransactionID": "deleted-legacy-1",
                "Type": "RECEIVE",
                "Reference": "po_ledger",
                "DateString": "2026-07-10",
                "Total": 92.00,
                "Status": "DELETED",
                "BankAccount": {"AccountID": "bank-1"},
                "LineItems": [],
            }],
        )
        self.assertEqual(preview["classification"], "missing_xero_transaction")
        self.assertTrue(preview["automatic_action_allowed"])
        self.assertEqual(
            preview["ignored_inactive_transactions"][0]["bank_transaction_id"],
            "deleted-legacy-1",
        )

    def test_payout_preview_hash_rejects_stale_post(self):
        record = persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )[0]
        binding = self._capture_stripe_statement_line(record)
        preview = build_xero_preview(record)
        self.assertEqual(len(preview["payload_hash"]), 64)
        with patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            return_value=self._active_xero_bank_accounts(),
        ):
            with self.assertRaisesMessage(
                ReconciliationValidationError,
                "preview changed after review",
            ):
                post_xero_bank_transaction(
                    record,
                    approved_by_slack_id="UFIN",
                    expected_payload_hash="f" * 64,
                    **binding,
                )

    def test_monthly_context_suggestion_adds_linear_project_and_review_note(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        context = build_reconciliation_enrichment_context(
            organization=self.organization,
            run_id="monthly-1",
        )
        self.assertEqual(context["luma_events"][0]["source_id"], "evt_1")
        self.assertEqual(context["luma_events"][0]["exact_linear_matches"][0]["source_id"], "lin_event_1")
        projects = {item["source_id"]: item for item in context["linear_projects"]}
        self.assertEqual(projects["lin_event_1"]["dimension_hint"], "event_mirror")
        self.assertEqual(projects["lin_project_1"]["dimension_hint"], "project")

        suggestion = save_reconciliation_suggestions(
            organization=self.organization,
            run_id="monthly-1",
            model_name="reasoning-model",
            suggestions=[{
                "payout_id": "po_ledger",
                "source_type": "luma_event",
                "source_id": "evt_1",
                "event": {"source_type": "luma", "source_id": "evt_1"},
                "allocation_mode": "event",
                "confidence": 0.96,
                "rationale": "The Luma and Linear names match and Slack confirms the event workstream.",
                "review_note": "Ticket revenue for the Luma Night project; confirmed in the event planning thread.",
                "evidence": [{"source_provider": "slack", "source_record_id": "thread-1", "summary": "Event planning"}],
            }],
        )[0]
        self.assertEqual(suggestion.status, ReconciliationSuggestion.STATUS_PROPOSED)
        approved, mapping = approve_reconciliation_suggestion(suggestion, reviewed_by_slack_id="UFIN")
        self.assertEqual(approved.status, ReconciliationSuggestion.STATUS_APPROVED)
        self.assertEqual(mapping.event_tracking_option_name, "Luma Night")
        self.assertEqual(mapping.event_tracking_option_id, "")
        self.assertEqual(mapping.project_source_id, "")
        self.assertEqual(mapping.project_tracking_option_name, "")

        retried = save_reconciliation_suggestions(
            organization=self.organization,
            run_id="monthly-1",
            suggestions=[{
                "payout_id": "po_ledger",
                "source_type": "luma_event",
                "source_id": "evt_1",
                "event": {"source_type": "luma", "source_id": "evt_1"},
                "confidence": 0.2,
                "rationale": "A retry must not reset the approved decision.",
            }],
        )[0]
        self.assertEqual(retried.status, ReconciliationSuggestion.STATUS_APPROVED)
        self.assertEqual(retried.event_source_id, "evt_1")
        self.assertEqual(retried.project_source_id, "")

        preview = build_xero_preview(record)
        revenue_line = preview["xero_payload"]["LineItems"][0]
        self.assertEqual([item["Name"] for item in revenue_line["Tracking"]], ["Event Name"])
        self.assertNotIn("Project: Community Events", revenue_line["Description"])
        self.assertIn("confirmed in the event planning thread", revenue_line["Description"])
        self.assertEqual(preview["context_notes"][0]["project_name"], "")

    def test_payout_suggestion_rejects_explicit_unassigned_with_event(self):
        persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )

        with self.assertRaisesMessage(
            ValueError,
            "allocation_mode does not match the selected allocation",
        ):
            save_reconciliation_suggestions(
                organization=self.organization,
                run_id="monthly-explicit-unassigned",
                suggestions=[{
                    "payout_id": "po_ledger",
                    "source_type": "luma_event",
                    "source_id": "evt_1",
                    "allocation_mode": "unassigned",
                    "event": {"source_type": "luma", "source_id": "evt_1"},
                    "confidence": 0.1,
                }],
            )

    def test_payout_suggestion_rejects_event_and_project_together(self):
        persist_report(
            organization=self.organization,
            report=self.report,
            stripe_account_id="acct_main",
        )

        with self.assertRaisesMessage(ValueError, "either an event or a project"):
            save_reconciliation_suggestions(
                organization=self.organization,
                run_id="monthly-dual-allocation",
                suggestions=[{
                    "payout_id": "po_ledger",
                    "source_type": "luma_event",
                    "source_id": "evt_1",
                    "event": {"source_type": "luma", "source_id": "evt_1"},
                    "project": {
                        "source_type": "linear",
                        "source_id": "lin_project_1",
                    },
                }],
            )

    @patch("integrations.services.reconciliation_context.active_xero_project_options")
    def test_context_deduplicates_linear_and_xero_projects_and_keeps_xero_only_options(self, options):
        options.return_value = [
            {
                "source_type": "xero_tracking",
                "source_id": "xero-community",
                "tracking_option_id": "xero-community",
                "name": "Community Events",
            },
            {
                "source_type": "xero_tracking",
                "source_id": "xero-victor",
                "tracking_option_id": "xero-victor",
                "name": "VictorAI",
            },
        ]

        context = build_reconciliation_enrichment_context(
            organization=self.organization,
            run_id="project-catalog",
        )

        projects = context["linear_projects"]
        community = [item for item in projects if item["name"] == "Community Events"]
        self.assertEqual(len(community), 1)
        self.assertEqual(community[0]["xero_tracking_option_id"], "xero-community")
        victor = next(item for item in projects if item["name"] == "VictorAI")
        self.assertEqual(victor["source_type"], "xero_tracking")
        self.assertEqual(victor["source_id"], "xero-victor")

    def test_contextual_review_note_requires_source_evidence(self):
        persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")
        with self.assertRaisesMessage(ValueError, "must cite source evidence"):
            save_reconciliation_suggestions(
                organization=self.organization,
                run_id="monthly-ungrounded",
                suggestions=[{
                    "payout_id": "po_ledger",
                    "source_type": "luma_event",
                    "source_id": "evt_1",
                    "event": {"source_type": "luma", "source_id": "evt_1"},
                    "review_note": "This unsupported note must not reach Xero.",
                    "evidence": [],
                }],
            )

    def test_statement_backfill_uses_only_historical_patterns_or_matching_bills(self):
        lines = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[
                {
                    "statement_line_id": "ready-uber",
                    "date": "20 May 2026",
                    "narration": "UBER *TRIP HELP.UB Card xx3532",
                    "reference": "POS",
                    "direction": "debit",
                    "amount": "12.93",
                    "contact": "uber",
                    "account": "406 - Travel-national",
                    "description": "uber trip",
                    "tax_type": "GST on Expenses",
                    "ui_mode": "green_match",
                    "has_ok": True,
                },
                {
                    "statement_line_id": "blank-uber",
                    "date": "21 May 2026",
                    "narration": "UBER *TRIP HELP.UB Card xx1336",
                    "reference": "POS",
                    "direction": "debit",
                    "amount": "18.50",
                    "has_ok": False,
                },
                {
                    "statement_line_id": "blank-bill",
                    "date": "1 Jun 2026",
                    "narration": "PRINT LOCKER ALPHI",
                    "reference": "POS",
                    "direction": "debit",
                    "amount": "1308.12",
                    "has_ok": False,
                },
            ],
        )
        self.assertEqual(len(lines), 3)
        ExternalFinancialRecord.objects.create(
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            external_record_id="bill-print-locker",
            external_account_id="tenant-1",
            currency="AUD",
            amount="1308.12",
            direction="debit",
            status="AUTHORISED",
            transaction_date=datetime(2026, 5, 27).date(),
            description="BILL-101 · Print Locker",
            merchant_name="Print Locker",
            category="bill",
            class_name="ACCPAY",
        )
        context = build_statement_reconciliation_context(organization=self.organization)
        candidates = {item["statement_line_id"]: item for item in context["statement_candidates"]}
        self.assertEqual(candidates["blank-uber"]["allowed_historical_patterns"][0]["account_code"], "406")
        self.assertEqual(
            candidates["blank-uber"]["allowed_historical_patterns"][0]["example_statement_line_id"],
            "ready-uber",
        )
        self.assertEqual(candidates["blank-bill"]["matching_xero_bills"][0]["xero_bill_id"], "bill-print-locker")
        self.assertTrue(
            candidates["blank-bill"]["matching_xero_bills"][0]["exact_outstanding_match"]
        )

        saved = save_statement_suggestions(
            organization=self.organization,
            run_id="monthly-statement-1",
            suggestions=[
                {
                    "statement_line_id": "blank-uber",
                    "proposed_action": "create_bank_transaction",
                    "contact_name": "uber",
                    "account_code": "406",
                    "account_name": "Travel-national",
                    "tax_type": "GST on Expenses",
                    "description": "Uber travel supported by the same Xero merchant rule.",
                    "review_note": "Exact historical merchant pattern; human must click OK.",
                    "confidence": 0.95,
                    "evidence": [{"source_provider": "xero_ui", "source_record_id": "ready-uber"}],
                },
                {
                    "statement_line_id": "blank-bill",
                    "proposed_action": "pay_existing_bill",
                    "matched_xero_bill_id": "bill-print-locker",
                    "description": "Exact amount matches the authorised Print Locker bill.",
                    "confidence": 0.9,
                    "evidence": [{"source_provider": "xero", "source_record_id": "bill-print-locker"}],
                },
            ],
        )
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0].status, XeroStatementSuggestion.STATUS_PROPOSED)
        self.assertEqual(saved[1].matched_xero_bill_id, "bill-print-locker")
        self.assertEqual(XeroStatementLineSnapshot.objects.filter(ready_in_xero=False).count(), 2)
        serialized = serialize_statement_suggestion(saved[0])
        self.assertEqual(serialized["browser_comment"], "Uber travel supported by the same Xero merchant rule. Confidence: 95%.")
        self.assertEqual(
            serialized["create_fields"],
            {
                "contact_name": "uber",
                "account_code": "406",
                "account_name": "Travel-national",
                "account_display": "406 - Travel-national",
                "description": "Uber travel supported by the same Xero merchant rule.",
                "event_name": "",
                "project_name": "",
                "tax_type": "GST on Expenses",
            },
        )

        with self.assertRaisesMessage(ValueError, "not backed by exact merchant history or an approved accounting option"):
            save_statement_suggestions(
                organization=self.organization,
                run_id="monthly-statement-invalid",
                suggestions=[{
                    "statement_line_id": "blank-uber",
                    "proposed_action": "prefill_create",
                    "contact_name": "uber",
                    "account_code": "999",
                    "account_name": "Invented account",
                    "tax_type": "GST on Expenses",
                    "evidence": [{"source_provider": "xero_ui", "source_record_id": "ready-uber"}],
                }],
            )

        approved = save_statement_suggestions(
            organization=self.organization,
            run_id="monthly-statement-approved-option",
            suggestions=[{
                "statement_line_id": "blank-uber",
                "proposed_action": "prefill_create",
                "contact_name": "Uber for Business",
                "account_code": "406",
                "account_name": "Travel-national",
                "tax_type": "GST on Expenses",
                "description": "Likely business transport.",
                "confidence": 0.8,
                "evidence": [{"source_provider": "xero_ui", "source_record_id": "blank-uber"}],
            }],
        )[0]
        self.assertEqual(approved.contact_name, "Uber for Business")

        partial = save_statement_suggestions(
            organization=self.organization,
            run_id="monthly-statement-partial-contact",
            suggestions=[{
                "statement_line_id": "blank-uber",
                "proposed_action": "needs_review",
                "contact_name": "Uber",
                "description": "Likely an Uber trip.",
                "confidence": 0.6,
                "evidence": [{"source_provider": "xero_ui", "source_record_id": "blank-uber"}],
            }],
        )[0]
        self.assertEqual(partial.contact_name, "Uber")

        with self.assertRaisesMessage(
            ValueError,
            "allocation_mode does not match the selected allocation",
        ):
            save_statement_suggestions(
                organization=self.organization,
                run_id="monthly-statement-explicit-unassigned",
                suggestions=[{
                    "statement_line_id": "blank-uber",
                    "proposed_action": "needs_review",
                    "allocation_mode": "unassigned",
                    "event": {"source_type": "luma", "source_id": "evt_1"},
                    "confidence": 0.1,
                }],
            )

    def test_mandatory_tracking_does_not_turn_uncertainty_into_mlai_core(self):
        self.profile.require_statement_tracking = True
        self.profile.default_project_tracking_option_name = "MLAI core"
        self.profile.save(
            update_fields=[
                "require_statement_tracking",
                "default_project_tracking_option_name",
                "updated_at",
            ]
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "uncertain-allocation",
                "date": "20 Jul 2026",
                "narration": "UNKNOWN PURCHASE",
                "direction": "debit",
                "amount": "10.00",
            }],
        )[0]

        suggestion = save_statement_suggestions(
            organization=self.organization,
            run_id="uncertain-allocation-run",
            suggestions=[{
                "statement_line_id": line.statement_line_id,
                "proposed_action": "needs_review",
                "allocation_mode": "unassigned",
                "confidence": 0.0,
            }],
        )[0]

        self.assertEqual(
            suggestion.allocation_mode,
            XeroStatementSuggestion.ALLOCATION_UNASSIGNED,
        )
        self.assertFalse(suggestion.execution_ready)

    def test_statement_backfill_accepts_snapshot_field_aliases(self):
        imported = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "ready-snapshot-shape",
                "date": "20 Jul 2026",
                "narration": "UBER *TRIP HELP.",
                "reference": "POS",
                "direction": "debit",
                "amount": "31.07",
                "current_contact": "uber",
                "current_account": "406 - Travel-national",
                "current_description": "Uber trip",
                "current_event_name": "HealthHack | Sydney",
                "current_project_name": "MedHack: Sydney",
                "current_tax_type": "GST on Expenses",
                "ui_mode": "green_match",
                "has_ok": True,
            }],
        )

        line = imported[0]
        self.assertEqual(line.current_contact, "uber")
        self.assertEqual(line.current_account, "406 - Travel-national")
        self.assertEqual(line.current_description, "Uber trip")
        self.assertEqual(line.current_event_name, "HealthHack | Sydney")
        self.assertEqual(line.current_project_name, "MedHack: Sydney")
        self.assertEqual(line.current_tax_type, "GST on Expenses")
        context = build_statement_reconciliation_context(organization=self.organization)
        self.assertEqual(
            context["approved_accounting_options"][0],
            {
                "account_code": "406",
                "account_name": "Travel-national",
                "tax_type": "GST on Expenses",
                "examples": [{
                    "statement_line_id": "ready-snapshot-shape",
                    "merchant_key": "uber trip help",
                    "contact_name": "uber",
                    "description": "Uber trip",
                }],
            },
        )

    def test_prefilled_create_with_ok_is_still_a_candidate(self):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "prefilled-luiz",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "reference": "NPP",
                "direction": "debit",
                "amount": "520.00",
                "contact": "Luiz F Oliveira Araujo",
                "account": "405 - Contractor Expenses",
                "description": "Contractor work for Aaron AI.",
                "project_name": "[Studio] Aaron AI",
                "tax_type": "GST Free Expenses",
                "has_ok": True,
            }],
        )[0]

        self.assertEqual(line.ui_mode, XeroStatementLineSnapshot.UI_CREATE_PREFILLED)
        self.assertTrue(line.create_prefill_complete)
        self.assertFalse(line.is_green_match)
        context = build_statement_reconciliation_context(organization=self.organization)
        self.assertEqual(
            [item["statement_line_id"] for item in context["statement_candidates"]],
            ["prefilled-luiz"],
        )
        self.assertEqual(context["prior_xero_examples"], [])

    def test_incomplete_scan_does_not_deactivate_unseen_rows(self):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "first-row",
                "date": "30 Jun 2026",
                "narration": "First",
                "direction": "debit",
                "amount": "10.00",
            }, {
                "statement_line_id": "second-row",
                "date": "30 Jun 2026",
                "narration": "Second",
                "direction": "debit",
                "amount": "20.00",
            }],
            expected_count=2,
        )

        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "first-row",
                "date": "30 Jun 2026",
                "narration": "First",
                "direction": "debit",
                "amount": "10.00",
            }],
            expected_count=2,
            complete_scan=False,
        )

        second = XeroStatementLineSnapshot.objects.get(statement_line_id="second-row")
        self.assertTrue(second.active)
        self.assertEqual(
            XeroStatementScan.objects.latest("id").status,
            XeroStatementScan.STATUS_INCOMPLETE,
        )

    def test_equivalent_xero_account_ids_merge_current_queue_without_false_confirmation(self):
        hyphenated_id = "feb39489-f354-4852-88df-266a69b627d7"
        compact_id = "FEB39489F354485288DF266A69B627D7"
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id=hyphenated_id,
            expected_count=2,
            lines=[{
                "statement_line_id": "shared-row",
                "date": "30 Jun 2026",
                "narration": "Shared",
                "direction": "debit",
                "amount": "10.00",
            }, {
                "statement_line_id": "old-row",
                "date": "30 Jun 2026",
                "narration": "Old",
                "direction": "debit",
                "amount": "20.00",
            }],
        )

        imported = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id=compact_id,
            expected_count=2,
            lines=[{
                "statement_line_id": "shared-row",
                "date": "30 Jun 2026",
                "narration": "Shared current",
                "direction": "debit",
                "amount": "10.00",
            }, {
                "statement_line_id": "new-row",
                "date": "1 Jul 2026",
                "narration": "New",
                "direction": "debit",
                "amount": "30.00",
            }],
        )

        self.assertEqual({line.bank_account_id for line in imported}, {compact_id})
        active = XeroStatementLineSnapshot.objects.filter(active=True)
        self.assertEqual(
            set(active.values_list("statement_line_id", flat=True)),
            {"shared-row", "new-row"},
        )
        alias = XeroStatementLineSnapshot.objects.get(
            bank_account_id=hyphenated_id,
            statement_line_id="shared-row",
        )
        self.assertFalse(alias.active)
        self.assertEqual(alias.queue_state, XeroStatementLineSnapshot.QUEUE_INACTIVE)
        old = XeroStatementLineSnapshot.objects.get(statement_line_id="old-row")
        self.assertFalse(old.active)
        self.assertEqual(old.queue_state, XeroStatementLineSnapshot.QUEUE_RECONCILED)

        # Legacy duplicates are also suppressed defensively before the next
        # complete capture gets a chance to retire them.
        alias.active = True
        alias.queue_state = XeroStatementLineSnapshot.QUEUE_ACTIVE
        alias.save(update_fields=["active", "queue_state", "last_seen_at"])
        context = build_statement_reconciliation_context(
            organization=self.organization,
            include_external_evidence=False,
            statement_line_ids={"shared-row", "new-row"},
        )
        self.assertEqual(
            [item["statement_line_id"] for item in context["statement_candidates"]],
            ["shared-row", "new-row"],
        )

    def test_complete_scan_count_mismatch_does_not_change_queue(self):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "existing-row",
                "date": "30 Jun 2026",
                "narration": "Existing",
                "direction": "debit",
                "amount": "10.00",
            }],
        )

        with self.assertRaisesMessage(ValueError, "expected 2 rows but observed 1"):
            import_xero_statement_lines(
                organization=self.organization,
                bank_account_id="bank-1",
                lines=[{
                    "statement_line_id": "replacement-row",
                    "date": "30 Jun 2026",
                    "narration": "Replacement",
                    "direction": "debit",
                    "amount": "20.00",
                }],
                expected_count=2,
            )

        self.assertTrue(
            XeroStatementLineSnapshot.objects.get(statement_line_id="existing-row").active
        )

    def test_statement_context_finds_date_amount_and_merchant_evidence(self):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[
                {
                    "statement_line_id": "jetstar-1",
                    "date": "20 Jul 2026",
                    "narration": "JETSTAR AIRWAYS Card xx1336",
                    "reference": "POS",
                    "direction": "debit",
                    "amount": "362.20",
                    "has_ok": False,
                },
                {
                    "statement_line_id": "city-1",
                    "date": "20 Jul 2026",
                    "narration": "CITY OF MELBOURN",
                    "reference": "MIS",
                    "direction": "credit",
                    "amount": "74.80",
                    "has_ok": False,
                },
                {
                    "statement_line_id": "stone-1",
                    "date": "20 Jul 2026",
                    "narration": "STONE AND CHALK",
                    "reference": "POS",
                    "direction": "debit",
                    "amount": "55.00",
                    "has_ok": False,
                },
            ],
        )
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            organization=self.organization,
            google_email="finance@example.com",
            refresh_token="google-refresh-token",
            scope="gmail.readonly",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-jetstar-receipt",
            gmail_thread_id="thread-jetstar",
            internal_date=datetime(2026, 7, 19, 9, tzinfo=timezone.utc),
            subject="Your Jetstar itinerary and tax invoice",
            from_address="itineraries@jetstar.com",
            snippet="Total paid AUD $362.20 for Melbourne to Sydney.",
        )
        slack_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            organization=self.organization,
            access_token="slack-token",
            external_account_id="workspace-1",
        )
        SlackMessageArtifact.objects.create(
            organization=self.organization,
            connection=slack_connection,
            channel_id="C-EVENTS",
            channel_name="events",
            slack_message_ts="1784455200.000001",
            posted_at=datetime(2026, 7, 18, 10, tzinfo=timezone.utc),
            author_name="Sam",
            text="I booked the Jetstar flight for $362.20 for the Sydney event.",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-unrelated",
            gmail_thread_id="thread-unrelated",
            internal_date=datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
            subject="Unrelated software receipt",
            snippet="Total $362.20",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-generic-melbourne",
            gmail_thread_id="thread-generic-melbourne",
            internal_date=datetime(2026, 7, 20, 11, tzinfo=timezone.utc),
            subject="Melbourne update",
            snippet="The total was $74.80.",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-city-of-melbourne",
            gmail_thread_id="thread-city-of-melbourne",
            internal_date=datetime(2026, 7, 19, 11, tzinfo=timezone.utc),
            subject="City of Melbourne permit refund",
            snippet="Refund total $74.80.",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-stone-and-chalk",
            gmail_thread_id="thread-stone-and-chalk",
            internal_date=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
            subject="Stone & Chalk room booking",
            snippet="The venue booking total was $55.00.",
        )
        SlackMessageArtifact.objects.create(
            organization=self.organization,
            connection=slack_connection,
            channel_id="C-RANDOM",
            channel_name="random",
            slack_message_ts="1784455300.000002",
            posted_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            author_name="Sam",
            text="Catering and $55.00 in supplies.",
        )

        context = build_statement_reconciliation_context(organization=self.organization)
        candidates = {item["statement_line_id"]: item for item in context["statement_candidates"]}
        candidate = candidates["jetstar-1"]
        evidence = candidate["context_evidence"]
        self.assertEqual({item["source_provider"] for item in evidence}, {"gmail", "slack"})
        self.assertNotIn("gmail-unrelated", {item["source_record_id"] for item in evidence})
        gmail_evidence = next(item for item in evidence if item["source_provider"] == "gmail")
        self.assertIn("amount:362.20", gmail_evidence["match_reasons"])
        self.assertIn("Jetstar itinerary", gmail_evidence["summary"])
        self.assertEqual(
            {item["source_record_id"] for item in candidates["city-1"]["context_evidence"]},
            {"gmail-city-of-melbourne"},
        )
        self.assertEqual(
            {item["source_record_id"] for item in candidates["stone-1"]["context_evidence"]},
            {"gmail-stone-and-chalk"},
        )

    def test_statement_context_links_indirect_purchase_to_nearby_event_and_project_context(self):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "uber-watt-1",
                "date": "22 May 2026",
                "narration": "UBER *TRIP HELP.",
                "reference": "POS",
                "direction": "debit",
                "amount": "26.08",
                "has_ok": False,
            }],
        )
        LumaEventSelection.objects.create(
            connection=self.luma_connection,
            user=self.user,
            organization=self.organization,
            event_id="evt-watt",
            event_name="[AI Week] Watt The Hack - Energy & AI Hackathon",
            start_at=datetime(2026, 6, 5, 17, 30, tzinfo=timezone.utc),
        )
        LinearProjectArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            linear_project_id="project-watt",
            name="[AI Week] Watt The Hack",
            target_date=datetime(2026, 6, 5).date(),
        )
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            organization=self.organization,
            google_email="events@example.com",
            refresh_token="google-refresh-token",
            scope="gmail.readonly",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="gmail-watt-prep",
            gmail_thread_id="thread-watt-prep",
            internal_date=datetime(2026, 5, 24, 22, 46, tzinfo=timezone.utc),
            subject="RE: BESP event submission",
            cleaned_text="Watt The Hack - Energy & AI Hackathon at Stone & Chalk on 5 June.",
        )

        context = build_reconciliation_enrichment_context(organization=self.organization)
        candidate = next(
            item for item in context["statement_candidates"]
            if item["statement_line_id"] == "uber-watt-1"
        )

        self.assertEqual(candidate["nearby_events"][0]["source_id"], "evt-watt")
        self.assertEqual(candidate["nearby_projects"][0]["source_id"], "project-watt")
        evidence = candidate["event_project_context_evidence"]
        self.assertEqual([item["source_record_id"] for item in evidence], ["gmail-watt-prep"])
        self.assertEqual(
            {(item["source_type"], item["source_id"]) for item in evidence[0]["matched_entities"]},
            {("luma", "evt-watt"), ("linear", "project-watt")},
        )

    def test_reconciliation_context_includes_direct_linear_project_members(self):
        project = LinearProjectArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            linear_project_id="project-aaron",
            name="[Studio] Aaron AI",
            start_date=datetime(2026, 6, 9).date(),
        )
        LinearProjectMemberArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            project=project,
            linear_user_id="usr-luiz",
            name="Luiz Flavio",
            email="hello@luiz-flavio.com",
        )

        context = build_reconciliation_enrichment_context(organization=self.organization)

        linear_project = next(
            item for item in context["linear_projects"]
            if item["source_id"] == "project-aaron"
        )
        self.assertEqual(linear_project["members"], [{
            "source_type": "linear_user",
            "source_id": "usr-luiz",
            "name": "Luiz Flavio",
            "email": "hello@luiz-flavio.com",
            "membership_source": "direct",
        }])

    def test_humanitix_event_keeps_its_source_provenance_in_statement_and_payout_suggestions(self):
        humanitix_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.HUMANITIX,
            user=self.user,
            organization=self.organization,
            access_token="humanitix-token",
            external_account_id="humanitix-main",
        )
        HumanitixEvent.objects.create(
            organization=self.organization,
            connection=humanitix_connection,
            external_event_id="htx-historical-1",
            event_name="Historical Demo Day",
            start_at=datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc),
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "humanitix-taxi-1",
                "date": "19 Jul 2026",
                "narration": "TAXI FIXTURE",
                "direction": "debit",
                "amount": "24.00",
            }],
        )[0]

        context = build_reconciliation_enrichment_context(organization=self.organization)
        candidate = next(
            item for item in context["statement_candidates"]
            if item["statement_line_id"] == line.statement_line_id
        )
        humanitix_event = next(
            item for item in candidate["nearby_events"]
            if item["source_id"] == "htx-historical-1"
        )
        self.assertEqual(humanitix_event["source_type"], "humanitix")

        statement = save_statement_suggestions(
            organization=self.organization,
            run_id="humanitix-statement-run",
            suggestions=[{
                "statement_line_id": line.statement_line_id,
                "proposed_action": "needs_review",
                "description": "Taxi near Historical Demo Day.",
                "event": {
                    "source_type": "humanitix",
                    "source_id": "htx-historical-1",
                },
                "allocation_confidence": 0.8,
                "evidence": [{
                    "source_provider": "humanitix",
                    "source_record_id": "htx-historical-1",
                }],
            }],
        )[0]
        self.assertEqual(statement.event_source_type, "humanitix")
        self.assertEqual(
            serialize_statement_suggestion(statement)["event"],
            {
                "source_type": "humanitix",
                "source_id": "htx-historical-1",
                "tracking_option_name": "Historical Demo Day",
            },
        )

        payout = StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po_humanitix_stripe",
            source_hash="h" * 64,
            amount_cents=1000,
            currency="AUD",
            report_payload={
                "revenue_groups": [{
                    "source_type": "humanitix_event",
                    "source_id": "htx-historical-1",
                    "source_label": "Historical Demo Day",
                    "gross_cents": 1000,
                    "stripe_fee_cents": 0,
                }],
            },
        )
        suggestion = save_reconciliation_suggestions(
            organization=self.organization,
            run_id="humanitix-payout-run",
            suggestions=[{
                "payout_id": payout.payout_id,
                "source_type": "humanitix_event",
                "source_id": "htx-historical-1",
                "event": {
                    "source_type": "humanitix",
                    "source_id": "htx-historical-1",
                },
                "confidence": 0.9,
                "evidence": [{
                    "source_provider": "humanitix",
                    "source_record_id": "htx-historical-1",
                }],
            }],
        )[0]
        self.assertEqual(suggestion.event_source_type, "humanitix")
        self.assertEqual(suggestion.event_tracking_option_name, "Historical Demo Day")

    def test_verified_party_identity_is_supplied_to_statement_agent(self):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "luiz-identity-520",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
        )[0]
        ReconciliationPartyIdentity.objects.create(
            organization=self.organization,
            bank_narration_key=merchant_key(line.narration),
            direction="debit",
            canonical_name="Luiz Flavio",
            xero_contact_name="Luiz F Oliveira Araujo",
            linear_user_id="usr-luiz",
            linear_name="Luiz Flavio",
            linear_email="hello@luiz-flavio.com",
            status=ReconciliationPartyIdentity.STATUS_VERIFIED,
            confidence=1.0,
            verified_by_slack_id="UADMIN",
        )

        candidate = next(
            item for item in build_statement_reconciliation_context(
                organization=self.organization
            )["statement_candidates"]
            if item["statement_line_id"] == line.statement_line_id
        )

        self.assertEqual(candidate["verified_identity"]["linear_user_id"], "usr-luiz")
        self.assertEqual(candidate["verified_identity"]["canonical_name"], "Luiz Flavio")

    def test_verified_date_bounded_rule_authoritatively_codes_luiz_to_aaron_ai(self):
        project = LinearProjectArtifact.objects.create(
            connection=self.linear_connection,
            organization=self.organization,
            linear_project_id="project-aaron-ai",
            name="[Studio] Aaron AI",
            description="Aaron AI client delivery.",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "luiz-aaron-rule-520",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
        )[0]
        rule = ReconciliationRule.objects.create(
            organization=self.organization,
            name="Luiz contractor payments – Aaron AI",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(line.narration),
            direction="debit",
            effective_from=datetime(2026, 6, 1).date(),
            effective_to=datetime(2026, 7, 31).date(),
            contact_name="Luiz F Oliveira Araujo",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description_template="Contractor work for {project}.",
            project_source_id=project.linear_project_id,
            project_tracking_option_name=project.name,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            verified_by_slack_id="UADMIN",
            verified_at=datetime.now(timezone.utc),
        )

        context = build_statement_reconciliation_context(organization=self.organization)
        candidate = next(
            item for item in context["statement_candidates"]
            if item["statement_line_id"] == line.statement_line_id
        )
        self.assertEqual(candidate["verified_rule"]["id"], rule.id)
        saved = save_statement_suggestions(
            organization=self.organization,
            run_id="run-luiz-rule",
            suggestions=[{
                "statement_line_id": line.statement_line_id,
                "proposed_action": "needs_review",
                "confidence": 0.35,
                "document_confidence": 0.20,
            }],
        )[0]

        self.assertEqual(saved.proposed_action, "create_bank_transaction")
        self.assertEqual(saved.contact_name, "Luiz F Oliveira Araujo")
        self.assertEqual(saved.account_code, "405")
        self.assertEqual(saved.project_source_id, "project-aaron-ai")
        self.assertEqual(saved.description, "Contractor work for [Studio] Aaron AI.")
        self.assertTrue(saved.execution_ready)
        self.assertEqual(saved.identity_confidence, 1.0)
        self.assertEqual(saved.allocation_confidence, 1.0)
        decision = ReconciliationDecision.objects.get(
            suggestion=saved,
            decision_type=ReconciliationDecision.TYPE_RULE_APPLIED,
        )
        self.assertEqual(decision.rule, rule)
        self.assertEqual(decision.actor_type, ReconciliationDecision.ACTOR_SYSTEM)

    def test_equal_priority_verified_rule_conflict_blocks_agent_output(self):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "luiz-conflicting-rules",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "455.00",
            }],
        )[0]
        common = {
            "organization": self.organization,
            "scope": ReconciliationRule.SCOPE_MERCHANT,
            "bank_narration_key": merchant_key(line.narration),
            "direction": "debit",
            "contact_name": "Luiz F Oliveira Araujo",
            "account_code": "405",
            "account_name": "Contractor Expenses",
            "tax_type": "GST Free Expenses",
            "description_template": "Contractor work.",
            "priority": 100,
            "status": ReconciliationRule.STATUS_VERIFIED,
            "active": True,
            "verified_at": datetime.now(timezone.utc),
        }
        ReconciliationRule.objects.create(name="Aaron AI", project_source_id="lin_project_1", **common)
        ReconciliationRule.objects.create(name="Community", project_source_id="lin_event_1", **common)

        saved = save_statement_suggestions(
            organization=self.organization,
            run_id="run-rule-conflict",
            suggestions=[{
                "statement_line_id": line.statement_line_id,
                "proposed_action": "create_bank_transaction",
                "account_code": "405",
                "account_name": "Contractor Expenses",
                "tax_type": "GST Free Expenses",
            }],
        )[0]
        self.assertEqual(saved.proposed_action, XeroStatementSuggestion.ACTION_NEEDS_REVIEW)
        self.assertFalse(saved.execution_ready)
        self.assertTrue(ReconciliationDecision.objects.filter(
            statement_line=line,
            decision_type=ReconciliationDecision.TYPE_RULE_CONFLICT,
        ).exists())

    def test_statement_override_wins_and_merchant_rule_stays_date_bounded(self):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "specific-rule-line",
                "date": "30 Jun 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "100.00",
            }, {
                "statement_line_id": "outside-rule-window",
                "date": "1 Aug 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "120.00",
            }],
        )[0]
        common = {
            "organization": self.organization,
            "contact_name": "Contractor One",
            "account_code": "405",
            "account_name": "Contractor Expenses",
            "tax_type": "GST Free Expenses",
            "description_template": "Contractor work.",
            "status": ReconciliationRule.STATUS_VERIFIED,
            "active": True,
            "verified_at": datetime.now(timezone.utc),
        }
        ReconciliationRule.objects.create(
            name="June/July merchant policy",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(line.narration),
            direction="debit",
            effective_from=datetime(2026, 6, 1).date(),
            effective_to=datetime(2026, 7, 31).date(),
            project_source_id="lin_project_1",
            **common,
        )
        statement_rule = ReconciliationRule.objects.create(
            name="One-line correction",
            scope=ReconciliationRule.SCOPE_STATEMENT_LINE,
            statement_line=line,
            project_source_id="lin_event_1",
            priority=1,
            **common,
        )

        candidates = {
            item["statement_line_id"]: item
            for item in build_statement_reconciliation_context(
                organization=self.organization
            )["statement_candidates"]
        }
        self.assertEqual(candidates[line.statement_line_id]["verified_rule"]["id"], statement_rule.id)
        self.assertIsNone(candidates["outside-rule-window"]["verified_rule"])

    def _statement_suggestion(
        self,
        *,
        line_id="api-uber",
        amount="31.07",
        action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
        matched_bill_id="",
        confidence=0.99,
        direction=XeroStatementLineSnapshot.DIRECTION_DEBIT,
    ):
        line = XeroStatementLineSnapshot.objects.create(
            organization=self.organization,
            bank_account_id="bank-1",
            statement_line_id=line_id,
            transaction_date=datetime(2026, 7, 16).date(),
            narration="UBER *TRIP HELP.",
            reference="POS",
            direction=direction,
            amount=amount,
            currency="AUD",
            source_hash=f"source-{line_id}",
        )
        return XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=line,
            run_id=f"run-{line_id}",
            proposed_action=action,
            contact_name="uber",
            account_code="406",
            account_name="Travel-national",
            tax_type="INPUT",
            description="Uber trip for the Sydney event.",
            matched_xero_bill_id=matched_bill_id,
            confidence=confidence,
            evidence=[{"source_provider": "xero_ui", "source_record_id": "prior-uber"}],
            source_hash=line.source_hash,
        )

    @staticmethod
    def _xero_response(payload):
        response = Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def _event_statement_suggestion(self, *, line_id, option_name="Luma Night"):
        suggestion = self._statement_suggestion(line_id=line_id)
        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_EVENT
        suggestion.event_source_type = "luma"
        suggestion.event_source_id = "evt_1"
        suggestion.event_tracking_option_name = option_name
        suggestion.save(update_fields=[
            "allocation_mode",
            "event_source_type",
            "event_source_id",
            "event_tracking_option_name",
            "updated_at",
        ])
        return suggestion

    def _statement_execute_get_responses(self, *, category_id, options=None):
        no_transactions = self._xero_response({"BankTransactions": []})
        return [
            no_transactions,
            no_transactions,
            self._xero_response({
                "Contacts": [{"ContactID": "contact-uber", "Name": "uber"}],
            }),
            self._xero_response({
                "TrackingCategories": [{
                    "TrackingCategoryID": category_id,
                    "Name": (
                        "Event Name"
                        if category_id == "event-category-1"
                        else "Project Name"
                    ),
                    "Status": "ACTIVE",
                    "Options": options or [],
                }],
            }),
        ]

    def test_statement_bank_transaction_preview_and_post_are_idempotent(self):
        suggestion = self._statement_suggestion()
        preview = build_statement_posting_preview(suggestion)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["operation"], "bank_transaction")
        self.assertEqual(preview["xero_payload"]["Type"], "SPEND")
        self.assertNotIn("IsReconciled", preview["xero_payload"])

        empty = Mock()
        empty.json.return_value = {"BankTransactions": []}
        empty.raise_for_status.return_value = None
        contacts = Mock()
        contacts.json.return_value = {"Contacts": [{"ContactID": "contact-uber", "Name": "uber"}]}
        contacts.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {"BankTransactions": [{"BankTransactionID": "bt-statement-1"}]}
        created.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=[empty, empty, contacts],
        ) as get_mock, patch(
            "integrations.services.xero_statement_posting.http_client.put",
            return_value=created,
        ) as put_mock:
            posting = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")
            again = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")
        self.assertEqual(posting.status, XeroStatementPosting.STATUS_MATCH_READY)
        self.assertEqual(posting.xero_bank_transaction_id, "bt-statement-1")
        self.assertEqual(again.id, posting.id)
        self.assertEqual(get_mock.call_count, 3)
        self.assertEqual(put_mock.call_count, 1)
        body = put_mock.call_args.kwargs["json"]["BankTransactions"][0]
        self.assertEqual(body["Contact"], {"ContactID": "contact-uber"})
        self.assertEqual(body["LineItems"][0]["UnitAmount"], 31.07)

    @patch("integrations.services.reconciliation_bank_accounts.http_client.get")
    def test_statement_preview_uses_the_lines_live_active_bank_account(self, get_mock):
        accounts = Mock()
        accounts.raise_for_status.return_value = None
        accounts.json.return_value = {
            "Accounts": [
                {"AccountID": "bank-1", "Name": "Everyday", "Type": "BANK", "Status": "ACTIVE"},
                {"AccountID": "bank-2", "Name": "Event Receipts", "Type": "BANK", "Status": "ACTIVE"},
                {"AccountID": "bank-old", "Name": "Closed", "Type": "BANK", "Status": "ARCHIVED"},
            ]
        }
        get_mock.return_value = accounts
        suggestion = self._statement_suggestion(line_id="multi-bank-preview")
        suggestion.statement_line.bank_account_id = "bank-2"
        suggestion.statement_line.save(update_fields=["bank_account_id"])

        preview = build_statement_posting_preview(suggestion)

        self.assertTrue(preview["ready"])
        self.assertEqual(
            preview["xero_payload"]["BankAccount"],
            {"AccountID": "bank-2"},
        )

    @patch("integrations.services.reconciliation_bank_accounts.http_client.get")
    def test_statement_preview_rejects_a_non_active_bank_account(self, get_mock):
        accounts = Mock()
        accounts.raise_for_status.return_value = None
        accounts.json.return_value = {
            "Accounts": [
                {"AccountID": "bank-1", "Name": "Everyday", "Type": "BANK", "Status": "ACTIVE"},
            ]
        }
        get_mock.return_value = accounts
        suggestion = self._statement_suggestion(line_id="inactive-bank-preview")
        suggestion.statement_line.bank_account_id = "bank-old"
        suggestion.statement_line.save(update_fields=["bank_account_id"])

        preview = build_statement_posting_preview(suggestion)

        self.assertFalse(preview["ready"])
        self.assertIn(
            "The statement line does not belong to an active Xero BANK account.",
            preview["errors"],
        )

    def test_statement_preview_never_executes_identical_csv_duplicates(self):
        suggestion = self._statement_suggestion(
            line_id=f"csv-{'a' * 40}-1-of-2",
        )

        preview = build_statement_posting_preview(suggestion)

        self.assertFalse(preview["ready"])
        self.assertIn(
            "Identical CSV statement lines cannot be prepared automatically because "
            "the report has no stable per-line identifier.",
            preview["errors"],
        )

    def test_dimension_scores_can_be_executable_when_document_confidence_is_low(self):
        suggestion = self._statement_suggestion(line_id="luiz-aaron", confidence=0.62)
        suggestion.identity_confidence = 0.98
        suggestion.accounting_confidence = 0.95
        suggestion.allocation_confidence = 0.94
        suggestion.document_confidence = 0.40
        suggestion.project_source_id = "project-aaron"
        suggestion.project_tracking_option_name = "[Studio] Aaron AI"
        suggestion.execution_ready = True
        suggestion.save()

        preview = build_statement_posting_preview(suggestion)

        self.assertTrue(preview["ready"])
        self.assertFalse(preview["errors"])

    def test_mandatory_tracking_uses_exactly_one_mlai_core_project(self):
        self.profile.require_statement_tracking = True
        self.profile.default_project_tracking_option_name = "MLAI core"
        self.profile.default_project_tracking_option_id = "project-core"
        self.profile.xero_bank_account_id = "feb39489-f354-4852-88df-266a69b627d7"
        self.profile.save(update_fields=[
            "require_statement_tracking",
            "default_project_tracking_option_name",
            "default_project_tracking_option_id",
            "xero_bank_account_id",
            "updated_at",
        ])
        suggestion = self._statement_suggestion(line_id="core-fallback")
        suggestion.statement_line.bank_account_id = "FEB39489F354485288DF266A69B627D7"
        suggestion.statement_line.save(update_fields=["bank_account_id"])
        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_MLAI_CORE
        suggestion.identity_confidence = 0.99
        suggestion.accounting_confidence = 0.99
        suggestion.allocation_confidence = 1.0
        suggestion.execution_ready = True
        suggestion.save()
        stale = self._statement_suggestion(line_id="core-fallback-stale")
        stale.execution_ready = True
        stale.save(update_fields=["execution_ready", "updated_at"])
        stale.statement_line.active = False
        stale.statement_line.save(update_fields=["active"])

        preview = build_statement_posting_preview(suggestion)

        self.assertTrue(preview["ready"])
        self.assertTrue(preview["tracking_policy_ready"])
        self.assertEqual(preview["untracked_executable_count"], 0)
        self.assertEqual(preview["effective_tracking"]["option_name"], "MLAI core")
        self.assertTrue(preview["effective_tracking"]["default"])
        tracking = preview["xero_payload"]["LineItems"][0]["Tracking"]
        self.assertEqual(len(tracking), 1)
        self.assertEqual(tracking[0]["Name"], "Project Name")
        self.assertEqual(tracking[0]["Option"], "MLAI core")

        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_EVENT
        suggestion.event_source_id = "evt_1"
        suggestion.event_tracking_option_name = "Luma Night"
        suggestion.save(update_fields=[
            "allocation_mode",
            "event_source_id",
            "event_tracking_option_name",
            "updated_at",
        ])
        event_preview = build_statement_posting_preview(suggestion)
        self.assertNotEqual(event_preview["payload_hash"], preview["payload_hash"])
        self.assertEqual(event_preview["effective_tracking"]["kind"], "event")

    def test_mandatory_tracking_rejects_dual_event_and_project(self):
        self.profile.require_statement_tracking = True
        self.profile.default_project_tracking_option_name = "MLAI core"
        self.profile.save(update_fields=[
            "require_statement_tracking",
            "default_project_tracking_option_name",
            "updated_at",
        ])
        suggestion = self._statement_suggestion(line_id="dual-tracking")
        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_EVENT
        suggestion.event_source_id = "evt_1"
        suggestion.event_tracking_option_name = "Luma Night"
        suggestion.project_source_id = "lin_project_1"
        suggestion.project_tracking_option_name = "Community Events"
        suggestion.identity_confidence = 0.99
        suggestion.accounting_confidence = 0.99
        suggestion.allocation_confidence = 0.99
        suggestion.execution_ready = True
        suggestion.save()

        preview = build_statement_posting_preview(suggestion)

        self.assertFalse(preview["ready"])
        self.assertIn(
            "Choose exactly one Event Name or Project Name, not both.",
            preview["errors"],
        )

    def test_statement_tracking_preview_never_creates_a_missing_option(self):
        suggestion = self._event_statement_suggestion(line_id="event-option-preview")

        with patch("integrations.services.xero_statement_posting.http_client.get") as get_mock, patch(
            "integrations.services.xero_statement_posting.http_client.put"
        ) as put_mock:
            preview = build_statement_posting_preview(suggestion)

        self.assertTrue(preview["ready"])
        self.assertEqual(preview["effective_tracking"]["option_name"], "Luma Night")
        get_mock.assert_not_called()
        put_mock.assert_not_called()

    def test_confirmed_statement_execute_creates_missing_canonical_event_option(self):
        suggestion = self._event_statement_suggestion(line_id="event-option-create")
        created_option = self._xero_response({
            "Options": [{
                "TrackingOptionID": "event-option-created",
                "Name": "Luma Night",
                "Status": "ACTIVE",
            }],
        })
        created_transaction = self._xero_response({
            "BankTransactions": [{"BankTransactionID": "bt-event-option"}],
        })

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=self._statement_execute_get_responses(category_id="event-category-1"),
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put",
            side_effect=[created_option, created_transaction],
        ) as put_mock:
            posting = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")

        self.assertEqual(posting.xero_bank_transaction_id, "bt-event-option")
        self.assertEqual(put_mock.call_count, 2)
        self.assertEqual(
            put_mock.call_args_list[0].args[0],
            "https://api.xero.com/api.xro/2.0/TrackingCategories/event-category-1/Options",
        )
        self.assertEqual(
            put_mock.call_args_list[0].kwargs["json"],
            {"Options": [{"Name": "Luma Night"}]},
        )
        tracking = put_mock.call_args_list[1].kwargs["json"]["BankTransactions"][0][
            "LineItems"
        ][0]["Tracking"]
        self.assertEqual(tracking[0]["TrackingOptionID"], "event-option-created")

    def test_statement_execute_rejects_stale_canonical_name_before_catalog_write(self):
        suggestion = self._event_statement_suggestion(
            line_id="event-option-stale",
            option_name="Invented Event Name",
        )

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=self._statement_execute_get_responses(category_id="event-category-1"),
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "name changed or is ambiguous"):
                execute_statement_posting(suggestion, requested_by_slack_id="UFIN")

        put_mock.assert_not_called()

    def test_statement_execute_requires_settings_scope_to_create_canonical_option(self):
        self.connection.scopes = [
            scope for scope in self.connection.scopes if scope != "accounting.settings"
        ]
        self.connection.save(update_fields=["scopes", "updated_at"])
        suggestion = self._event_statement_suggestion(line_id="event-option-no-settings")

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=self._statement_execute_get_responses(category_id="event-category-1"),
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "accounting.settings"):
                execute_statement_posting(suggestion, requested_by_slack_id="UFIN")

        put_mock.assert_not_called()

    def test_execution_resolver_creates_missing_canonical_linear_project_option(self):
        suggestion = self._statement_suggestion(line_id="project-option-create")
        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_PROJECT
        suggestion.project_source_type = "linear"
        suggestion.project_source_id = "lin_project_1"
        suggestion.project_tracking_option_name = "Community Events"
        suggestion.save(update_fields=[
            "allocation_mode",
            "project_source_type",
            "project_source_id",
            "project_tracking_option_name",
            "updated_at",
        ])
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "project-category-1",
                "Name": "Project Name",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })
        created_option = self._xero_response({
            "Options": [{
                "TrackingOptionID": "project-option-created",
                "Name": "Community Events",
                "Status": "ACTIVE",
            }],
        })

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put",
            return_value=created_option,
        ) as put_mock:
            resolved = _resolved_tracking(
                self.connection,
                self.profile,
                suggestion,
            )

        self.assertEqual(resolved[0]["TrackingOptionID"], "project-option-created")
        self.assertEqual(
            put_mock.call_args.args[0],
            "https://api.xero.com/api.xro/2.0/TrackingCategories/project-category-1/Options",
        )

    def test_execution_resolver_never_recreates_missing_xero_sourced_project_option(self):
        suggestion = self._statement_suggestion(line_id="xero-project-option-missing")
        suggestion.allocation_mode = XeroStatementSuggestion.ALLOCATION_PROJECT
        suggestion.project_source_type = "xero_tracking"
        suggestion.project_source_id = "xero-option-missing"
        suggestion.project_tracking_option_id = "xero-option-missing"
        suggestion.project_tracking_option_name = "Archived Project"
        suggestion.save(update_fields=[
            "allocation_mode",
            "project_source_type",
            "project_source_id",
            "project_tracking_option_id",
            "project_tracking_option_name",
            "updated_at",
        ])
        categories = self._xero_response({
            "TrackingCategories": [{
                "TrackingCategoryID": "project-category-1",
                "Name": "Project Name",
                "Status": "ACTIVE",
                "Options": [],
            }],
        })

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            return_value=categories,
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                XeroPostingError,
                "must retain its existing tracking option ID",
            ):
                _resolved_tracking(
                    self.connection,
                    self.profile,
                    suggestion,
                )

        put_mock.assert_not_called()

    @override_settings(XERO_STATEMENT_AUTO_POST_ENABLED=False)
    def test_automatic_statement_execution_gate_blocks_before_tracking_or_transaction_writes(self):
        suggestion = self._event_statement_suggestion(line_id="event-option-auto-disabled")

        with patch("integrations.services.xero_statement_posting.http_client.get") as get_mock, patch(
            "integrations.services.xero_statement_posting.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                ReconciliationValidationError,
                "Automatic statement posting is disabled",
            ):
                execute_statement_posting(
                    suggestion,
                    requested_by_slack_id="monthly-update:test",
                    automatic=True,
                )

        get_mock.assert_not_called()
        put_mock.assert_not_called()

    def test_74_line_context_batches_gmail_and_slack_evidence_queries(self):
        XeroStatementLineSnapshot.objects.bulk_create([
            XeroStatementLineSnapshot(
                organization=self.organization,
                bank_account_id="bank-1",
                statement_line_id=f"batch-{index}",
                transaction_date=date(2026, 7, 1) + timedelta(days=index % 31),
                narration=f"Merchant {index}",
                reference="POS",
                direction=XeroStatementLineSnapshot.DIRECTION_DEBIT,
                amount="10.00",
                currency="AUD",
                source_hash=f"{index:064d}",
            )
            for index in range(74)
        ])

        with CaptureQueriesContext(connection) as queries:
            context = build_statement_reconciliation_context(
                organization=self.organization,
                include_external_evidence=True,
            )

        self.assertEqual(len(context["statement_candidates"]), 74)
        gmail_table = GmailMessageArtifact._meta.db_table.casefold()
        slack_table = SlackMessageArtifact._meta.db_table.casefold()
        gmail_queries = [query for query in queries if gmail_table in query["sql"].casefold()]
        slack_queries = [query for query in queries if slack_table in query["sql"].casefold()]
        self.assertLessEqual(len(gmail_queries), 1)
        self.assertLessEqual(len(slack_queries), 1)

    def test_statement_post_translates_xero_tax_rate_label_to_api_code(self):
        suggestion = self._statement_suggestion(line_id="tax-label")
        suggestion.tax_type = "GST Free Expenses"
        suggestion.save(update_fields=["tax_type", "updated_at"])

        empty = Mock()
        empty.json.return_value = {"BankTransactions": []}
        empty.raise_for_status.return_value = None
        contacts = Mock()
        contacts.json.return_value = {"Contacts": [{"ContactID": "contact-uber", "Name": "uber"}]}
        contacts.raise_for_status.return_value = None
        tax_rates = Mock()
        tax_rates.json.return_value = {"TaxRates": [{
            "Name": "GST Free Expenses",
            "TaxType": "EXEMPTEXPENSES",
            "Status": "ACTIVE",
            "CanApplyToExpenses": True,
            "CanApplyToRevenue": False,
        }]}
        tax_rates.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {"BankTransactions": [{"BankTransactionID": "bt-tax-label"}]}
        created.raise_for_status.return_value = None

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=[empty, empty, contacts, tax_rates],
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put",
            return_value=created,
        ) as put_mock:
            posting = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")

        self.assertEqual(posting.status, XeroStatementPosting.STATUS_MATCH_READY)
        body = put_mock.call_args.kwargs["json"]["BankTransactions"][0]
        self.assertEqual(body["LineItems"][0]["TaxType"], "EXEMPTEXPENSES")

    def test_preview_does_not_reset_an_inflight_posting(self):
        suggestion = self._statement_suggestion(line_id="posting-race")
        first = build_statement_posting_preview(suggestion)
        posting = XeroStatementPosting.objects.get(pk=first["posting_id"])
        posting.status = XeroStatementPosting.STATUS_POSTING
        posting.save(update_fields=["status", "updated_at"])

        second = build_statement_posting_preview(suggestion)

        posting.refresh_from_db()
        self.assertFalse(second["ready"])
        self.assertEqual(posting.status, XeroStatementPosting.STATUS_POSTING)
        self.assertIn("already being posted", second["errors"][0])

    def test_credit_statement_preview_creates_receive_money(self):
        suggestion = self._statement_suggestion(
            line_id="api-receive",
            direction=XeroStatementLineSnapshot.DIRECTION_CREDIT,
        )
        preview = build_statement_posting_preview(suggestion)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["xero_payload"]["Type"], "RECEIVE")

    def test_retry_recovers_existing_xero_transaction_by_stable_reference(self):
        suggestion = self._statement_suggestion(line_id="api-recovery")
        preview = build_statement_posting_preview(suggestion)
        posting = XeroStatementPosting.objects.get(pk=preview["posting_id"])
        posting.status = XeroStatementPosting.STATUS_FAILED
        posting.save(update_fields=["status", "updated_at"])
        existing = Mock()
        existing.json.return_value = {"BankTransactions": [{
            "BankTransactionID": "bt-recovered",
            "Type": "SPEND",
            "Total": 31.07,
            "BankAccount": {"AccountID": "bank-1"},
        }]}
        existing.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            return_value=existing,
        ), patch("integrations.services.xero_statement_posting.http_client.put") as put_mock:
            recovered = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")
        self.assertEqual(recovered.status, XeroStatementPosting.STATUS_MATCH_READY)
        self.assertEqual(recovered.xero_bank_transaction_id, "bt-recovered")
        put_mock.assert_not_called()

    def test_semantic_xero_duplicate_blocks_a_second_bank_transaction(self):
        suggestion = self._statement_suggestion(line_id="semantic-duplicate")
        no_reference = Mock()
        no_reference.json.return_value = {"BankTransactions": []}
        no_reference.raise_for_status.return_value = None
        semantic_match = Mock()
        semantic_match.json.return_value = {"BankTransactions": [{
            "BankTransactionID": "bt-manual-existing",
            "Type": "SPEND",
            "Total": 31.07,
            "CurrencyCode": "AUD",
            "BankAccount": {"AccountID": "bank-1"},
            "Contact": {"Name": "uber"},
        }]}
        semantic_match.raise_for_status.return_value = None

        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=[no_reference, semantic_match],
        ), patch("integrations.services.xero_statement_posting.http_client.put") as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "same bank account"):
                execute_statement_posting(suggestion, requested_by_slack_id="UFIN")

        put_mock.assert_not_called()
        decision = ReconciliationDecision.objects.filter(
            suggestion=suggestion,
            decision_type=ReconciliationDecision.TYPE_PREVIEW_BLOCKED,
            outcome__reason="semantic_duplicate",
        ).first()
        self.assertIsNotNone(decision)
        self.assertEqual(decision.outcome["xero_bank_transaction_ids"], ["bt-manual-existing"])

    def test_existing_bill_creates_payment_not_spend_money(self):
        bill = ExternalFinancialRecord.objects.create(
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            external_record_id="bill-uber",
            external_account_id="tenant-1",
            currency="AUD",
            amount="31.07",
            direction="debit",
            status="AUTHORISED",
            transaction_date=datetime(2026, 7, 10).date(),
            merchant_name="uber",
            class_name="ACCPAY",
        )
        spend_suggestion = self._statement_suggestion(line_id="bill-spend-blocked")
        bill.amount = spend_suggestion.statement_line.amount
        bill.save(update_fields=["amount", "updated_at"])
        blocked = build_statement_posting_preview(spend_suggestion)
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("pay the bill" in error for error in blocked["errors"]))

        bill.delete()
        bill = ExternalFinancialRecord.objects.create(
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            external_record_id="bill-uber",
            external_account_id="tenant-1",
            currency="AUD",
            amount="31.07",
            direction="debit",
            status="AUTHORISED",
            transaction_date=datetime(2026, 7, 10).date(),
            merchant_name="uber",
            class_name="ACCPAY",
        )
        suggestion = self._statement_suggestion(
            line_id="bill-payment",
            action=XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL,
            matched_bill_id=bill.external_record_id,
        )
        preview = build_statement_posting_preview(suggestion)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["operation"], "bill_payment")
        self.assertEqual(
            serialize_statement_suggestion(suggestion)["routing"]["source"],
            "exact_xero_bill",
        )

        no_payment = Mock()
        no_payment.json.return_value = {"Payments": []}
        no_payment.raise_for_status.return_value = None
        live_bill = Mock()
        live_bill.json.return_value = {"Invoices": [{
            "InvoiceID": "bill-uber",
            "Type": "ACCPAY",
            "Status": "AUTHORISED",
            "AmountDue": 31.07,
            "CurrencyCode": "AUD",
        }]}
        live_bill.raise_for_status.return_value = None
        payment = Mock()
        payment.json.return_value = {"Payments": [{"PaymentID": "payment-1"}]}
        payment.raise_for_status.return_value = None
        with patch(
            "integrations.services.xero_statement_posting.http_client.get",
            side_effect=[no_payment, live_bill],
        ), patch(
            "integrations.services.xero_statement_posting.http_client.put",
            return_value=payment,
        ) as put_mock:
            posting = execute_statement_posting(suggestion, requested_by_slack_id="UFIN")
        self.assertEqual(posting.xero_payment_id, "payment-1")
        self.assertEqual(posting.xero_bill_id, "bill-uber")
        self.assertIn("/Payments", put_mock.call_args.args[0])
        body = put_mock.call_args.kwargs["json"]["Payments"][0]
        self.assertEqual(body["Invoice"], {"InvoiceID": "bill-uber"})
        self.assertNotIn("IsReconciled", body)

    def test_xero_sync_keeps_accounts_payable_payments(self):
        count = _upsert_xero_payments(self.connection, [{
            "PaymentID": "ap-payment-1",
            "Status": "AUTHORISED",
            "Amount": 31.07,
            "Date": "2026-07-16",
            "Invoice": {
                "InvoiceID": "bill-1",
                "Type": "ACCPAY",
                "CurrencyCode": "AUD",
                "Contact": {"Name": "Uber"},
            },
        }])
        self.assertEqual(count, 1)
        record = ExternalFinancialRecord.objects.get(external_record_id="ap-payment-1")
        self.assertEqual(record.direction, "debit")
        self.assertEqual(record.category, "bill_payment")
        self.assertEqual(record.class_name, "ACCPAY")


class ReconciliationWorkflowApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="agent@example.com", slack_id="UAGENT")
        PointsAdmin.objects.create(slack_user_id="UADMIN", role="admin", is_active=True)
        self._capture_sequence = 0
        self._real_import_xero_statement_lines = import_xero_statement_lines
        account_catalog_patcher = patch(
            "integrations.services.xero_reconciliation.fetch_xero_accounts",
            side_effect=self._active_accounts_for_current_capture,
        )
        self.account_catalog_mock = account_catalog_patcher.start()
        self.addCleanup(account_catalog_patcher.stop)
        statement_import_patcher = patch(
            "integrations.tests_xero_reconciliation.import_xero_statement_lines",
            side_effect=self._import_complete_account_capture,
        )
        statement_import_patcher.start()
        self.addCleanup(statement_import_patcher.stop)

    def _active_accounts_for_current_capture(self, _profile):
        latest_scan = XeroStatementScan.objects.filter(
            organization=self.organization
        ).order_by("-started_at", "-id").first()
        metadata = (
            latest_scan.capture_metadata
            if latest_scan and isinstance(latest_scan.capture_metadata, dict)
            else {}
        )
        active_ids = metadata.get("active_bank_account_ids") or ["bank-1"]
        labels = {
            canonical_bank_account_id(scan.bank_account_id): str(
                (scan.capture_metadata or {}).get("bank_account_label")
                or scan.bank_account_id
            )
            for scan in XeroStatementScan.objects.filter(
                organization=self.organization,
                capture_metadata__capture_id=metadata.get("capture_id"),
            )
        }
        return [
            {
                "AccountID": account_id,
                "Name": labels.get(canonical_bank_account_id(account_id), "Operating"),
                "Type": "BANK",
                "Status": "ACTIVE",
            }
            for account_id in active_ids
        ]

    def _import_complete_account_capture(self, **kwargs):
        if kwargs.get("capture_metadata") is not None:
            return self._real_import_xero_statement_lines(**kwargs)
        self._capture_sequence += 1
        bank_account_id = str(kwargs.get("bank_account_id") or "bank-1")
        lines = kwargs.get("lines") or []
        complete = kwargs.get("complete_scan", True)
        profile = ReconciliationProfile.objects.filter(
            organization=self.organization
        ).select_related("xero_connection").first()
        if profile is None:
            connection = ExternalServiceConnection.objects.create(
                provider=ExternalServiceProvider.XERO,
                user=self.user,
                organization=self.organization,
                access_token="access-token",
                external_account_id="tenant-test",
                account_label=self.organization.name,
                scopes=["accounting.banktransactions", "accounting.payments"],
            )
            profile = ReconciliationProfile.objects.create(
                organization=self.organization,
                xero_connection=connection,
                xero_bank_account_id=bank_account_id,
            )
        connection = profile.xero_connection if profile else None
        if connection is not None and not connection.account_label:
            connection.account_label = self.organization.name
            connection.save(update_fields=["account_label", "updated_at"])
        account_hash = hashlib.sha256(
            json.dumps(
                lines,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        capture_id = f"api-test-capture-{self._capture_sequence}"
        kwargs["capture_metadata"] = {
            "schema_version": 2,
            "capture_source": STATEMENT_CAPTURE_SOURCE_BROWSER,
            "capture_id": capture_id,
            "scan_id": f"{capture_id}-1",
            "account_source_sha256": account_hash,
            "report_format": "xero_bank_reconciliation_dom",
            "tenant_id": str(
                connection.external_account_id if connection else "tenant-test"
            ),
            "organisation_name": str(
                connection.account_label if connection else self.organization.name
            ),
            "bank_account_label": "Operating",
            "account_position": 1,
            "account_count": 1,
            "active_bank_account_ids": [bank_account_id],
            "all_accounts_requested": True,
            "full_organisation_coverage_confirmed": True,
            "date_range_confirmed": True,
            "derived_complete": complete,
            "blocking_reasons": [] if complete else ["fixture_incomplete"],
        }
        return self._real_import_xero_statement_lines(**kwargs)

    def _agent_run_suggestion(self, *, run_id="xero-agent-api-run", line_id="agent-api-line"):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-agent-api",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
            xero_bank_account_id="bank-1",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": line_id,
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        capture = select_current_statement_capture(self.organization)
        self.assertTrue(capture.all_account_capture)
        run = ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="xero_reconciliation_agent",
            domain=self.organization.domain,
            organization=self.organization,
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={
                "statement_scan_id": line.last_scan_id,
                "statement_scan_ids": [line.last_scan_id],
                "statement_capture_id": capture.capture_id,
                "statement_capture_fingerprint": capture.capture_fingerprint,
                "statement_line_ids": [line.statement_line_id],
                "requested_statement_line_ids": [line.statement_line_id],
                "statement_line_source_hashes": {
                    line.statement_line_id: line.source_hash,
                },
            },
        )
        suggestion = XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=line,
            run_id=run.run_id,
            proposed_action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
            contact_name="Contractor One",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description="Contractor work for the client project.",
            confidence=0.99,
            execution_ready=True,
            source_hash=line.source_hash,
            evidence=[{"source_provider": "xero_ui", "source_record_id": line.statement_line_id}],
        )
        return run, suggestion

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_reconciliation_readiness_reports_missing_prerequisites(self, _permission):
        response = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["ready_to_start"])
        self.assertFalse(response.data["ready_to_execute_bank_transactions"])
        self.assertFalse(response.data["ready_to_execute_bill_payments"])
        self.assertIsNone(response.data["latest_statement_scan"])
        self.assertIsNone(response.data["monthly_context"])
        self.assertIn(
            "Import the current complete all-account Xero bank-feed queue.",
            response.data["blockers"],
        )
        self.assertIn(
            "Reconnect Xero with accounting.payments before paying existing bills.",
            response.data["warnings"],
        )

    @patch("integrations.api_views_reconciliation.fetch_active_xero_bank_accounts")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_bank_account_catalog_is_live_xero_scoped(self, _permission, fetch_accounts):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-catalog",
            account_label="MLAI AU",
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
        )
        fetch_accounts.return_value = [
            {"bank_account_id": "bank-1", "name": "Everyday"},
            {"bank_account_id": "bank-2", "name": "Event Receipts"},
        ]

        response = self.client.get(
            reverse("reconciliation_bank_accounts"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "schema_version": 1,
            "tenant_id": "tenant-catalog",
            "organisation_name": "MLAI AU",
            "accounts": [
                {"bank_account_id": "bank-1", "name": "Everyday"},
                {"bank_account_id": "bank-2", "name": "Event Receipts"},
            ],
        })

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_reconciliation_readiness_reports_catalog_drift(self, _permission):
        initial = build_reconciliation_catalog_status(organization=self.organization)
        ContentFactoryRun.objects.create(
            run_id="catalog-drift-run",
            workflow="xero_reconciliation_agent",
            domain=self.organization.domain,
            organization=self.organization,
            run_request={"catalog_source_hashes": initial["source_hashes"]},
        )
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="linear-catalog-token",
            external_account_id="linear-catalog-drift",
        )
        LinearProjectArtifact.objects.create(
            connection=connection,
            organization=self.organization,
            linear_project_id="catalog-project-new",
            name="New project after run start",
        )

        response = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["catalog_status"]["drift_detected"])
        self.assertEqual(
            response.data["catalog_status"]["changed_catalogs"],
            ["linear_projects"],
        )
        self.assertTrue(
            any("Entity catalogs changed" in item for item in response.data["warnings"])
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_reconciliation_readiness_confirms_fresh_context_and_xero_scopes(self, _permission):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-readiness",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
            xero_bank_account_id="bank-1",
            event_tracking_category_id="event-category",
            project_tracking_category_id="project-category",
            require_statement_tracking=True,
            default_project_tracking_option_name="MLAI core",
            default_project_tracking_option_id="project-core",
        )
        monthly_run = ContentFactoryRun.objects.create(
            run_id="monthly-readiness",
            workflow="startup_monthly_update",
            domain=self.organization.domain,
            organization=self.organization,
            status=ContentFactoryRunStatus.COMPLETED,
        )
        stale_line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "readiness-stale-untracked",
                "date": "19 Jul 2026",
                "narration": "OLD ALREADY RECONCILED ROW",
                "direction": "debit",
                "amount": "10.00",
            }],
        )[0]
        XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=stale_line,
            run_id="old-readiness-run",
            proposed_action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
            allocation_mode=XeroStatementSuggestion.ALLOCATION_UNASSIGNED,
            execution_ready=True,
            status=XeroStatementSuggestion.STATUS_PROPOSED,
            source_hash=stale_line.source_hash,
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "readiness-unreconciled",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]

        response = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["ready_to_start"])
        self.assertTrue(response.data["ready_to_execute_bank_transactions"])
        self.assertTrue(response.data["ready_to_execute_bill_payments"])
        self.assertTrue(response.data["tracking_ready"])
        self.assertTrue(response.data["tracking_policy_ready"])
        self.assertEqual(response.data["untracked_executable_count"], 0)
        stale_line.refresh_from_db()
        self.assertFalse(stale_line.active)
        self.assertEqual(response.data["latest_statement_scan"]["candidate_count"], 1)
        self.assertEqual(
            response.data["latest_statement_scan"]["id"],
            line.last_scan_id,
        )
        self.assertEqual(
            response.data["monthly_context"]["run_id"],
            monthly_run.run_id,
        )
        self.assertEqual(response.data["blockers"], [])
        self.assertEqual(
            response.data["recommended_next_action"],
            "Start Xero reconciliation in preview-only mode.",
        )

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_all_account_capture_drives_one_agent_run_for_every_accounts_candidates(
        self, _permission, notify_valley
    ):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-all-accounts",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
            event_tracking_category_id="event-category",
            project_tracking_category_id="project-category",
        )
        hyphenated_id = "feb39489-f354-4852-88df-266a69b627d7"
        compact_id = "FEB39489F354485288DF266A69B627D7"
        account_ids = [hyphenated_id, "bank-2"]
        # The browser and Xero API can spell the same account UUID
        # differently. A prior compact scan must not break the all-account
        # capture group whose evidence contains the hyphenated API spelling.
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id=compact_id,
            expected_count=0,
            lines=[],
        )

        def metadata(position, label, lines):
            return {
                "schema_version": 2,
                "capture_source": STATEMENT_CAPTURE_SOURCE_BROWSER,
                "capture_id": "browser-all-accounts-fixture",
                "scan_id": f"browser-all-accounts-fixture-{position}",
                "account_source_sha256": hashlib.sha256(
                    json.dumps(
                        lines,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "report_format": "xero_bank_reconciliation_dom",
                "tenant_id": "tenant-all-accounts",
                "organisation_name": "MLAI",
                "bank_account_label": label,
                "account_position": position,
                "account_count": 2,
                "active_bank_account_ids": account_ids,
                "all_accounts_requested": True,
                "full_organisation_coverage_confirmed": True,
                "period_start": "2026-01-01",
                "period_end": "2026-08-31",
                "date_range_confirmed": True,
                "derived_complete": True,
                "blocking_reasons": [],
            }

        first_lines = [{
            "statement_line_id": "all-account-line-1",
            "date": "20 Jul 2026",
            "narration": "Supplier one",
            "direction": "debit",
            "amount": "10.00",
        }]
        first = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id=hyphenated_id,
            expected_count=1,
            requested_by="UADMIN",
            capture_metadata=metadata(1, "Everyday", first_lines),
            lines=first_lines,
        )[0]
        second_lines = [{
            "statement_line_id": "all-account-line-2",
            "date": "21 Jul 2026",
            "narration": "Supplier two",
            "direction": "debit",
            "amount": "20.00",
        }]
        second = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-2",
            expected_count=1,
            requested_by="UADMIN",
            capture_metadata=metadata(2, "Event Receipts", second_lines),
            lines=second_lines,
        )[0]

        readiness = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "analysis_mode": "external_agent",
            },
            format="json",
        )

        self.assertTrue(readiness.data["ready_to_start"])
        self.assertTrue(readiness.data["all_account_capture"])
        self.assertEqual(readiness.data["latest_statement_scan"]["candidate_count"], 2)
        self.assertEqual(
            {item["id"] for item in readiness.data["latest_statement_scans"]},
            {first.last_scan_id, second.last_scan_id},
        )
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(started.data["requested_line_count"], 2)
        notify_valley.assert_not_called()

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_partial_all_account_capture_blocks_readiness(self, _permission):
        lines = [{
            "statement_line_id": "partial-all-account-line-1",
            "date": "20 Jul 2026",
            "narration": "Supplier one",
            "direction": "debit",
            "amount": "10.00",
        }]
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            capture_metadata={
                "schema_version": 2,
                "capture_source": STATEMENT_CAPTURE_SOURCE_BROWSER,
                "capture_id": "browser-partial-fixture",
                "scan_id": "browser-partial-fixture-1",
                "account_source_sha256": hashlib.sha256(
                    json.dumps(
                        lines,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "report_format": "xero_bank_reconciliation_dom",
                "tenant_id": "tenant-partial",
                "organisation_name": "MLAI",
                "bank_account_label": "Everyday",
                "account_position": 1,
                "account_count": 2,
                "active_bank_account_ids": ["bank-1", "bank-2"],
                "all_accounts_requested": True,
                "full_organisation_coverage_confirmed": True,
                "period_start": "2026-01-01",
                "period_end": "2026-08-31",
                "date_range_confirmed": True,
                "derived_complete": True,
                "blocking_reasons": [],
            },
            lines=lines,
        )

        readiness = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertFalse(readiness.data["ready_to_start"])
        self.assertFalse(readiness.data["all_account_capture"])
        self.assertTrue(
            any("partial" in blocker.lower() for blocker in readiness.data["blockers"])
        )

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_latest_incomplete_scan_blocks_readiness_and_agent_start(
        self, _permission, notify_valley
    ):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "complete-before-partial",
                "date": "20 Jul 2026",
                "narration": "Jaycar - Franklin",
                "direction": "debit",
                "amount": "95.05",
            }],
        )
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            complete_scan=False,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "partial-latest",
                "date": "21 Jul 2026",
                "narration": "UBER *TRIP HELP.",
                "direction": "debit",
                "amount": "31.07",
            }],
        )

        readiness = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
            format="json",
        )

        self.assertFalse(readiness.data["ready_to_start"])
        self.assertEqual(
            readiness.data["latest_statement_scan"]["status"],
            XeroStatementScan.STATUS_INCOMPLETE,
        )
        self.assertTrue(
            any("incomplete" in blocker.lower() for blocker in readiness.data["blockers"])
        )
        self.assertEqual(started.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            started.data["error"],
            "The latest Xero statement scan is incomplete.",
        )
        notify_valley.assert_not_called()

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_statement_scan_endpoint_records_explicit_prefill_state(self, _permission):
        response = self.client.post(
            reverse("reconciliation_statement_scans"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "bank_account_id": "bank-1",
                "expected_count": 1,
                "complete": True,
                "capture_metadata": {
                    "schema_version": 1,
                    "scan_id": "scan-redacted-api-001",
                    "source_started_at": "2026-07-18T01:00:00Z",
                    "source_completed_at": "2026-07-18T01:01:00Z",
                    "pages": [{
                        "page_number": 1,
                        "page_count": 1,
                        "observed_count": 1,
                        "has_previous": False,
                        "has_next": False,
                    }],
                    "derived_complete": True,
                    "blocking_reasons": [],
                },
                "lines": [{
                    "statement_line_id": "api-prefilled-luiz",
                    "date": "30 Jun 2026",
                    "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                    "direction": "debit",
                    "amount": "520.00",
                    "contact": "Luiz F Oliveira Araujo",
                    "account": "405 - Contractor Expenses",
                    "description": "Contractor work for Aaron AI.",
                    "project_name": "[Studio] Aaron AI",
                    "tax_type": "GST Free Expenses",
                    "has_ok": True,
                }],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["scan"]["status"], XeroStatementScan.STATUS_COMPLETE)
        self.assertEqual(
            response.data["scan"]["capture_metadata"]["scan_id"],
            "scan-redacted-api-001",
        )
        self.assertTrue(response.data["scan"]["capture_metadata"]["derived_complete"])
        self.assertEqual(
            response.data["statement_lines"][0]["ui_mode"],
            XeroStatementLineSnapshot.UI_CREATE_PREFILLED,
        )

    def test_statement_scan_capture_metadata_rejects_credentials(self):
        with self.assertRaisesMessage(ValueError, "forbidden sensitive field"):
            import_xero_statement_lines(
                organization=self.organization,
                bank_account_id="bank-1",
                lines=[],
                expected_count=None,
                complete_scan=False,
                capture_metadata={"refresh_token": "must-never-be-stored"},
            )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_admin_can_verify_bank_to_xero_party_identity(self, _permission):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "identity-api-luiz",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
        )[0]

        response = self.client.put(
            reverse("reconciliation_party_identities"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_id": line.statement_line_id,
                "canonical_name": "Luiz Flavio",
                "xero_contact_name": "Luiz F Oliveira Araujo",
                "status": "verified",
                "confirm": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["identity"]["status"], "verified")
        self.assertEqual(response.data["identity"]["verified_by_slack_id"], "UADMIN")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_identity_proposal_is_inactive_until_explicit_verification(self, _permission):
        proposed = self.client.put(
            reverse("reconciliation_party_identities"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "bank_narration_key": "redacted contractor",
                "direction": "debit",
                "canonical_name": "Redacted Contractor",
                "status": "proposed",
            },
            format="json",
        )
        unconfirmed = self.client.put(
            reverse("reconciliation_party_identities"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "bank_narration_key": "redacted contractor",
                "direction": "debit",
                "canonical_name": "Redacted Contractor",
                "status": "verified",
            },
            format="json",
        )

        self.assertEqual(proposed.status_code, status.HTTP_200_OK)
        self.assertEqual(proposed.data["identity"]["status"], "proposed")
        self.assertFalse(proposed.data["identity"]["active"])
        self.assertEqual(unconfirmed.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_admin_can_create_confirmed_date_bounded_reconciliation_rule(self, _permission):
        linear_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="linear-token",
            external_account_id="linear-api",
        )
        project = LinearProjectArtifact.objects.create(
            connection=linear_connection,
            organization=self.organization,
            linear_project_id="api-aaron-ai",
            name="[Studio] Aaron AI",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "api-rule-luiz",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
        )[0]
        payload = {
            "slack_user_id": "UADMIN",
            "domain": "mlai.au",
            "scope": "merchant",
            "statement_line_id": line.statement_line_id,
            "name": "Luiz – Aaron AI",
            "effective_from": "2026-06-01",
            "effective_to": "2026-07-31",
            "contact_name": "Luiz F Oliveira Araujo",
            "account_code": "405",
            "account_name": "Contractor Expenses",
            "tax_type": "GST Free Expenses",
            "description_template": "Contractor work for {project}.",
            "project_source_id": project.linear_project_id,
            "status": "verified",
        }
        unconfirmed = self.client.post(
            reverse("reconciliation_rules"),
            payload,
            format="json",
        )
        self.assertEqual(unconfirmed.status_code, status.HTTP_400_BAD_REQUEST)

        confirmed = self.client.post(
            reverse("reconciliation_rules"),
            {**payload, "confirm": True},
            format="json",
        )
        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)
        self.assertEqual(confirmed.data["rule"]["status"], "verified")
        self.assertTrue(confirmed.data["rule"]["active"])
        self.assertEqual(confirmed.data["rule"]["project"]["source_id"], "api-aaron-ai")
        self.assertEqual(confirmed.data["rule"]["verified_by_slack_id"], "UADMIN")

        listed = self.client.get(
            reverse("reconciliation_rules"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listed.data["rules"]), 1)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_requires_approval_then_executes_only_approved_suggestions(self, _permission):
        run, suggestion = self._agent_run_suggestion()

        preview = self.client.get(
            reverse("reconciliation_agent_run_preview", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["ready_count"], 1)
        self.assertEqual(preview.data["approved_count"], 0)

        before_approval = self.client.post(
            reverse("reconciliation_agent_run_execute", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
            format="json",
        )
        self.assertEqual(before_approval.status_code, status.HTTP_200_OK)
        self.assertEqual(before_approval.data["executed_count"], 0)
        self.assertIn("not approval", before_approval.data["results"][0]["error"])

        approval = self.client.post(
            reverse("reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "approve_all_ready": True,
            },
            format="json",
        )
        self.assertEqual(approval.status_code, status.HTTP_200_OK)
        self.assertEqual(approval.data["recorded_count"], 1)
        decision = ReconciliationDecision.objects.get(
            suggestion=suggestion,
            decision_type=ReconciliationDecision.TYPE_ADMIN_APPROVED,
        )
        self.assertTrue(decision.outcome["payload_hash"])

        posting = Mock(
            id=123,
            status=XeroStatementPosting.STATUS_MATCH_READY,
            xero_bank_transaction_id="bt-agent-approved",
            xero_payment_id="",
            xero_bill_id="",
        )
        with patch(
            "integrations.api_views_reconciliation.execute_statement_posting",
            return_value=posting,
        ) as execute_mock:
            executed = self.client.post(
                reverse("reconciliation_agent_run_execute", kwargs={"run_id": run.run_id}),
                {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
                format="json",
            )
        self.assertEqual(executed.status_code, status.HTTP_200_OK)
        self.assertEqual(executed.data["executed_count"], 1)
        self.assertTrue(executed.data["human_reconciliation_required"])
        execute_mock.assert_called_once_with(
            suggestion,
            requested_by_slack_id="UADMIN",
            automatic=False,
            capture_selection=ANY,
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_preview_reuses_one_live_bank_account_catalog(
        self, _permission
    ):
        run, _suggestion = self._agent_run_suggestion(
            run_id="xero-agent-batch-preview",
            line_id="agent-batch-line-1",
        )
        lines = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            requested_by="UADMIN",
            lines=[
                {
                    "statement_line_id": "agent-batch-line-1",
                    "date": "20 Jul 2026",
                    "narration": "Transfer To CONTRACTOR ONE",
                    "direction": "debit",
                    "amount": "845.00",
                },
                {
                    "statement_line_id": "agent-batch-line-2",
                    "date": "21 Jul 2026",
                    "narration": "Transfer To CONTRACTOR TWO",
                    "direction": "debit",
                    "amount": "745.00",
                },
            ],
        )
        second = lines[1]
        XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=second,
            run_id=run.run_id,
            proposed_action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
            contact_name="Contractor Two",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description="Contractor work for the client project.",
            confidence=0.99,
            source_hash=second.source_hash,
            evidence=[{"source_provider": "xero_ui", "source_record_id": second.statement_line_id}],
        )
        capture = select_current_statement_capture(self.organization)
        run.run_request = {
            **run.run_request,
            "statement_scan_id": capture.latest_scan.id,
            "statement_scan_ids": list(capture.scan_ids),
            "statement_capture_id": capture.capture_id,
            "statement_capture_fingerprint": capture.capture_fingerprint,
            "statement_line_ids": [line.statement_line_id for line in lines],
            "requested_statement_line_ids": [
                line.statement_line_id for line in lines
            ],
            "statement_line_source_hashes": {
                line.statement_line_id: line.source_hash for line in lines
            },
        }
        run.save(update_fields=["run_request", "updated_at"])
        catalog_calls_before_preview = self.account_catalog_mock.call_count

        preview = self.client.get(
            reverse("reconciliation_agent_run_preview", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["suggestion_count"], 2)
        self.assertEqual(
            self.account_catalog_mock.call_count,
            catalog_calls_before_preview + 1,
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_execution_blocks_when_approved_payload_changes(self, _permission):
        run, suggestion = self._agent_run_suggestion(
            run_id="xero-agent-stale-approval",
            line_id="agent-stale-line",
        )
        approval = self.client.post(
            reverse("reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "approve_all_ready": True,
            },
            format="json",
        )
        self.assertEqual(approval.data["recorded_count"], 1)
        suggestion.description = "Changed contractor allocation after approval."
        suggestion.save(update_fields=["description", "updated_at"])

        with patch("integrations.api_views_reconciliation.execute_statement_posting") as execute_mock:
            response = self.client.post(
                reverse("reconciliation_agent_run_execute", kwargs={"run_id": run.run_id}),
                {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["executed_count"], 0)
        self.assertIn("Approval is stale", response.data["results"][0]["error"])
        execute_mock.assert_not_called()
        self.assertTrue(ReconciliationDecision.objects.filter(
            suggestion=suggestion,
            decision_type=ReconciliationDecision.TYPE_EXECUTION_BLOCKED,
        ).exists())
        preview = self.client.get(
            reverse("reconciliation_agent_run_preview", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(preview.data["approved_count"], 0)
        self.assertEqual(
            preview.data["results"][0]["suggestion"]["approval"]["status"],
            "stale",
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_execution_blocks_when_the_statement_queue_changes(self, _permission):
        run, suggestion = self._agent_run_suggestion(
            run_id="xero-agent-stale-queue",
            line_id="agent-stale-queue-line",
        )
        approval = self.client.post(
            reverse("reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "approve_all_ready": True,
            },
            format="json",
        )
        self.assertEqual(approval.data["recorded_count"], 1)
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": suggestion.statement_line.statement_line_id,
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )

        with patch("integrations.api_views_reconciliation.execute_statement_posting") as execute_mock:
            response = self.client.post(
                reverse("reconciliation_agent_run_execute", kwargs={"run_id": run.run_id}),
                {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("queue changed", response.data["error"])
        execute_mock.assert_not_called()

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_decision_rejects_hashes_that_differ_from_reviewed_preview(self, _permission):
        run, suggestion = self._agent_run_suggestion(
            run_id="xero-agent-hash-bound",
            line_id="agent-hash-bound-line",
        )
        preview = self.client.get(
            reverse("reconciliation_agent_run_preview", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        ).data["results"][0]

        response = self.client.post(
            reverse("reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "decision_request_id": "reviewed-hash-mismatch",
                "decisions": [{
                    "suggestion_id": suggestion.id,
                    "decision": "approve",
                    "expected_source_hash": preview["suggestion"]["source_hash"],
                    "expected_payload_hash": "f" * 64,
                }],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["recorded_count"], 0)
        self.assertIn("payload changed", response.data["results"][0]["error"].lower())
        self.assertFalse(ReconciliationDecision.objects.filter(
            suggestion=suggestion,
            decision_type=ReconciliationDecision.TYPE_ADMIN_APPROVED,
        ).exists())

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_can_reapprove_after_a_later_rejection(self, _permission):
        run, suggestion = self._agent_run_suggestion(
            run_id="xero-agent-reapproval",
            line_id="agent-reapproval-line",
        )
        endpoint = reverse(
            "reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}
        )
        common = {
            "slack_user_id": "UADMIN",
            "domain": "mlai.au",
            "confirm": True,
        }

        first_approval = self.client.post(
            endpoint,
            {**common, "approve_all_ready": True, "decision_request_id": "approval-1"},
            format="json",
        )
        rejection = self.client.post(
            endpoint,
            {
                **common,
                "decision_request_id": "rejection-1",
                "decisions": [{
                    "suggestion_id": suggestion.id,
                    "decision": "reject",
                    "reason": "Check the allocation.",
                }],
            },
            format="json",
        )
        second_approval = self.client.post(
            endpoint,
            {**common, "approve_all_ready": True, "decision_request_id": "approval-2"},
            format="json",
        )

        self.assertEqual(first_approval.data["recorded_count"], 1)
        self.assertEqual(rejection.data["recorded_count"], 1)
        self.assertEqual(second_approval.data["recorded_count"], 1)
        decisions = list(
            ReconciliationDecision.objects.filter(
                suggestion=suggestion,
                decision_type__in=[
                    ReconciliationDecision.TYPE_ADMIN_APPROVED,
                    ReconciliationDecision.TYPE_ADMIN_REJECTED,
                ],
            ).order_by("id")
        )
        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            decisions[-1].decision_type,
            ReconciliationDecision.TYPE_ADMIN_APPROVED,
        )

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_requires_fresh_scan_and_dispatches_preview_workflow(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.return_value = ValleyHarnessResult(
            ok=True,
            payload={"job_id": "job-agent-1", "status": "queued"},
        )
        missing_scan = self.client.post(
            reverse("reconciliation_agent_runs"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
            format="json",
        )
        self.assertEqual(missing_scan.status_code, status.HTTP_409_CONFLICT)

        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "agent-luiz-520",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
            expected_count=1,
            requested_by="UADMIN",
        )[0]

        response = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "instruction": "Allocate Luiz contractor payments to Aaron AI.",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.workflow, "xero_reconciliation_agent")
        self.assertEqual(run.step_order, ["reconciliation_enrichment"])
        self.assertTrue(run.run_request["dry_run"])
        self.assertEqual(
            set(run.run_request["catalog_source_hashes"]),
            {"luma_events", "humanitix_events", "linear_projects"},
        )
        self.assertIn("startup_memory", run.run_request["input_sources"])
        self.assertIn("humanitix", run.run_request["input_sources"])
        self.assertEqual(run.run_request["statement_line_ids"], [line.statement_line_id])
        self.assertEqual(
            run.run_request["requested_statement_line_ids"],
            [line.statement_line_id],
        )
        self.assertEqual(response.data["deterministic_suggestion_count"], 0)
        self.assertEqual(response.data["agent_line_count"], 1)
        self.assertTrue(response.data["valley_dispatched"])
        notify_valley.assert_called_once_with(run.run_id)

        detail = self.client.get(
            reverse("reconciliation_agent_run_detail", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["suggestions"], [])
        early_approval = self.client.post(
            reverse("reconciliation_agent_run_decisions", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "approve_all_ready": True,
            },
            format="json",
        )
        self.assertEqual(early_approval.status_code, status.HTTP_409_CONFLICT)

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_external_agent_run_owns_submitted_suggestions_and_completes_without_valley(
        self, _permission, notify_valley
    ):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "external-agent-current-line",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            organization=self.organization,
            google_email="treasurer@mlai.au",
            refresh_token="google-refresh-token",
            scope="gmail.readonly",
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=google_connection,
            gmail_message_id="external-agent-contractor-email",
            gmail_thread_id="external-agent-contractor-thread",
            internal_date=datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
            subject="Contractor One invoice",
            from_address="contractor@example.com",
            snippet="Contractor One total AUD 845.00",
        )

        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "analysis_mode": "external_agent",
                "instruction": "Use the treasurer mailbox and monthly context.",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )

        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(started.data["analysis_mode"], "external_agent")
        self.assertEqual(started.data["status"], ContentFactoryRunStatus.RUNNING)
        self.assertFalse(started.data["valley_dispatched"])
        notify_valley.assert_not_called()
        run = ContentFactoryRun.objects.get(run_id=started.data["run_id"])
        self.assertEqual(run.run_request["analysis_mode"], "external_agent")
        self.assertIn("treasurer_mailbox", run.run_request["input_sources"])

        context = self.client.get(
            reverse("reconciliation_enrichment_context"),
            {"domain": "mlai.au", "run_id": run.run_id},
        )
        self.assertEqual(context.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["statement_line_id"] for item in context.data["statement_candidates"]],
            [line.statement_line_id],
        )
        self.assertEqual(
            context.data["statement_external_evidence_source"],
            "client_local_mailboxes",
        )
        self.assertEqual(context.data["statement_candidates"][0]["context_evidence"], [])
        self.assertIn("months", context.data["monthly_timeline"])

        submitted = self.client.post(
            reverse("reconciliation_enrichment_context"),
            {
                "domain": "mlai.au",
                "run_id": run.run_id,
                "model_name": "gpt-reasoner",
                "suggestions": [],
                "statement_suggestions": [{
                    "statement_line_id": line.statement_line_id,
                    "proposed_action": "needs_review",
                    "contact_name": "Contractor One",
                    "review_note": "The treasurer mailbox identifies this as contractor work.",
                    "confidence": 0.99,
                    "identity_confidence": 0.99,
                    "accounting_confidence": 0.0,
                    "allocation_confidence": 0.0,
                    "document_confidence": 0.95,
                    "evidence": [{
                        "source_provider": "gmail",
                        "source_record_id": "treasurer-message-1",
                    }],
                }],
            },
            format="json",
        )

        self.assertEqual(submitted.status_code, status.HTTP_200_OK)
        self.assertEqual(submitted.data["statement_suggestion_count"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.current_step, "")
        suggestion = XeroStatementSuggestion.objects.get(run_id=run.run_id)
        self.assertEqual(suggestion.statement_line, line)
        self.assertEqual(suggestion.model_name, "gpt-reasoner")
        step = ContentFactoryRunStep.objects.get(run=run)
        self.assertEqual(step.status, ContentFactoryStepStatus.COMPLETED)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_external_agent_reports_terminal_failure_idempotently(self, _permission):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "external-agent-failed-line",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "analysis_mode": "external_agent",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )
        run_id = started.data["run_id"]

        failed = self.client.post(
            reverse("reconciliation_agent_run_fail", kwargs={"run_id": run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "failure_kind": "context_length_exceeded",
                "error": "The provider rejected the oversized input.",
            },
            format="json",
        )

        self.assertEqual(failed.status_code, status.HTTP_200_OK)
        self.assertEqual(failed.data["status"], ContentFactoryRunStatus.FAILED)
        self.assertEqual(failed.data["analysis_mode"], "external_agent")
        self.assertFalse(failed.data["idempotent"])
        run = ContentFactoryRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.current_step, "")
        self.assertIn("context_length_exceeded", run.error)
        self.assertEqual(
            run.result["external_agent_failure"]["failure_kind"],
            "context_length_exceeded",
        )
        step = ContentFactoryRunStep.objects.get(run=run)
        self.assertEqual(step.status, ContentFactoryStepStatus.FAILED)

        repeated = self.client.post(
            reverse("reconciliation_agent_run_fail", kwargs={"run_id": run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
                "failure_kind": "context_length_exceeded",
                "error": "The provider rejected the oversized input.",
            },
            format="json",
        )
        self.assertEqual(repeated.status_code, status.HTTP_200_OK)
        self.assertTrue(repeated.data["idempotent"])

        detail = self.client.get(
            reverse("reconciliation_agent_run_detail", kwargs={"run_id": run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(detail.data["analysis_mode"], "external_agent")
        self.assertEqual(detail.data["status"], ContentFactoryRunStatus.FAILED)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_external_agent_submission_rejects_missing_or_historical_lines(self, _permission):
        current_line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "external-agent-required-line",
                "date": "20 Jul 2026",
                "narration": "JAYCAR FRANKLIN",
                "direction": "debit",
                "amount": "95.05",
            }],
        )[0]
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "analysis_mode": "external_agent",
                "statement_line_ids": [current_line.statement_line_id],
            },
            format="json",
        )

        submitted = self.client.post(
            reverse("reconciliation_enrichment_context"),
            {
                "domain": "mlai.au",
                "run_id": started.data["run_id"],
                "model_name": "gpt-reasoner",
                "suggestions": [],
                "statement_suggestions": [],
            },
            format="json",
        )

        self.assertEqual(submitted.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            submitted.data["expected_statement_line_ids"],
            [current_line.statement_line_id],
        )

        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "external-agent-newer-scan-line",
                "date": "21 Jul 2026",
                "narration": "NEWER QUEUE ITEM",
                "direction": "debit",
                "amount": "12.00",
            }],
        )
        stale_submission = self.client.post(
            reverse("reconciliation_enrichment_context"),
            {
                "domain": "mlai.au",
                "run_id": started.data["run_id"],
                "model_name": "gpt-reasoner",
                "suggestions": [],
                "statement_suggestions": [{
                    "statement_line_id": current_line.statement_line_id,
                    "proposed_action": "needs_review",
                    "confidence": 0.0,
                    "identity_confidence": 0.0,
                    "accounting_confidence": 0.0,
                    "allocation_confidence": 0.0,
                    "document_confidence": 0.0,
                    "evidence": [],
                }],
            },
            format="json",
        )
        self.assertEqual(stale_submission.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("queue changed", stale_submission.data["error"])

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_repeated_identical_agent_start_reuses_run_without_duplicate_dispatch(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.return_value = ValleyHarnessResult(
            ok=True,
            payload={"job_id": "job-idempotent", "status": "queued"},
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "agent-idempotent-line",
                "date": "20 Jul 2026",
                "narration": "Jaycar - Franklin",
                "direction": "debit",
                "amount": "95.05",
            }],
        )[0]
        payload = {
            "slack_user_id": "UADMIN",
            "domain": "mlai.au",
            "instruction": "Use monthly context to identify the project.",
            "statement_line_ids": [line.statement_line_id],
        }

        first = self.client.post(
            reverse("reconciliation_agent_runs"),
            payload,
            format="json",
        )
        second = self.client.post(
            reverse("reconciliation_agent_runs"),
            payload,
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(first.data["idempotent"])
        self.assertTrue(second.data["idempotent"])
        self.assertEqual(first.data["run_id"], second.data["run_id"])
        self.assertEqual(
            first.data["request_fingerprint"],
            second.data["request_fingerprint"],
        )
        self.assertEqual(
            ContentFactoryRun.objects.filter(
                workflow="xero_reconciliation_agent"
            ).count(),
            1,
        )
        notify_valley.assert_called_once_with(first.data["run_id"])

        changed = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                **payload,
                "instruction": "Use monthly context and inspect the Aaron AI project.",
            },
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(changed.data["run_id"], first.data["run_id"])
        self.assertEqual(notify_valley.call_count, 2)

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_completes_without_valley_when_verified_rule_resolves_every_line(
        self, _permission, notify_valley
    ):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-deterministic",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
            xero_bank_account_id="bank-1",
            project_tracking_category_id="tracking-projects",
        )
        project = LinearProjectArtifact.objects.create(
            connection=ExternalServiceConnection.objects.create(
                provider=ExternalServiceProvider.LINEAR,
                user=self.user,
                organization=self.organization,
                access_token="linear-token",
                external_account_id="linear-deterministic",
            ),
            organization=self.organization,
            linear_project_id="project-deterministic-aaron",
            name="[Studio] Aaron AI",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "agent-rule-luiz-520",
                "date": "30 Jun 2026",
                "narration": "Transfer To LUIZ F OLIVEIRA ARAUJO",
                "direction": "debit",
                "amount": "520.00",
            }],
        )[0]
        rule = ReconciliationRule.objects.create(
            organization=self.organization,
            name="Luiz – Aaron AI",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(line.narration),
            direction=line.direction,
            effective_from=datetime(2026, 6, 1).date(),
            effective_to=datetime(2026, 7, 31).date(),
            contact_name="Luiz F Oliveira Araujo",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description_template="Contractor work for {project}.",
            project_source_id=project.linear_project_id,
            project_tracking_option_name=project.name,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            verified_by_slack_id="UADMIN",
            verified_at=datetime.now(timezone.utc),
        )

        response = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(response.data["deterministic_suggestion_count"], 1)
        self.assertEqual(response.data["agent_line_count"], 0)
        self.assertFalse(response.data["valley_dispatched"])
        notify_valley.assert_not_called()
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.run_request["statement_line_ids"], [])
        self.assertEqual(
            run.run_request["deterministic_statement_line_ids"],
            [line.statement_line_id],
        )
        step = ContentFactoryRunStep.objects.get(run=run)
        self.assertEqual(step.status, ContentFactoryStepStatus.COMPLETED)
        suggestion = XeroStatementSuggestion.objects.get(
            run_id=run.run_id,
            statement_line=line,
        )
        self.assertEqual(suggestion.model_name, "deterministic_verified_rule")
        self.assertEqual(
            suggestion.proposed_action,
            XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
        )
        self.assertEqual(suggestion.project_source_id, project.linear_project_id)
        self.assertTrue(suggestion.execution_ready)
        self.assertTrue(ReconciliationDecision.objects.filter(
            suggestion=suggestion,
            rule=rule,
            decision_type=ReconciliationDecision.TYPE_RULE_APPLIED,
        ).exists())

        preview = self.client.get(
            reverse("reconciliation_agent_run_preview", kwargs={"run_id": run.run_id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["ready_count"], 1)
        self.assertEqual(
            preview.data["results"][0]["suggestion"]["routing"],
            {
                "source": "verified_rule",
                "verified_rule_id": rule.id,
                "xero_bill_id": None,
                "model_name": "deterministic_verified_rule",
            },
        )
        self.assertEqual(preview.data["routing_counts"], {"verified_rule": 1})
        self.assertEqual(
            preview.data["deterministic_reconciliation"]["deterministic_suggestion_count"],
            1,
        )

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_agent_run_sends_only_unresolved_lines_to_valley(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.return_value = ValleyHarnessResult(
            ok=True,
            payload={"job_id": "job-mixed", "status": "queued"},
        )
        lines = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "agent-rule-contractor",
                "date": "30 Jun 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }, {
                "statement_line_id": "agent-unresolved-jaycar",
                "date": "24 May 2026",
                "narration": "Jaycar - Franklin",
                "direction": "debit",
                "amount": "95.05",
            }],
        )
        rule_line, unresolved_line = lines
        ReconciliationRule.objects.create(
            organization=self.organization,
            name="Contractor One",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(rule_line.narration),
            direction=rule_line.direction,
            contact_name="Contractor One",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description_template="Contractor work.",
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            verified_by_slack_id="UADMIN",
            verified_at=datetime.now(timezone.utc),
        )

        response = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_ids": [
                    rule_line.statement_line_id,
                    unresolved_line.statement_line_id,
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["deterministic_suggestion_count"], 1)
        self.assertEqual(response.data["agent_line_count"], 1)
        self.assertTrue(response.data["valley_dispatched"])
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(
            run.run_request["requested_statement_line_ids"],
            [unresolved_line.statement_line_id, rule_line.statement_line_id],
        )
        self.assertEqual(
            run.run_request["statement_line_ids"],
            [unresolved_line.statement_line_id],
        )
        self.assertTrue(XeroStatementSuggestion.objects.filter(
            run_id=run.run_id,
            statement_line=rule_line,
            model_name="deterministic_verified_rule",
        ).exists())
        notify_valley.assert_called_once_with(run.run_id)

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_failed_valley_dispatch_can_retry_same_run_without_duplicate_suggestions(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.side_effect = [
            ValleyHarnessResult(
                ok=False,
                failure_kind="connection",
                detail="Valley unavailable",
            ),
            ValleyHarnessResult(
                ok=True,
                payload={"job_id": "job-retry", "status": "queued"},
            ),
        ]
        rule_line, unresolved_line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "retry-rule-contractor",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }, {
                "statement_line_id": "retry-unresolved-jaycar",
                "date": "20 Jul 2026",
                "narration": "Jaycar - Franklin",
                "direction": "debit",
                "amount": "95.05",
            }],
        )
        ReconciliationRule.objects.create(
            organization=self.organization,
            name="Contractor default",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(rule_line.narration),
            direction=rule_line.direction,
            contact_name="Contractor One",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description_template="Contractor work.",
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            verified_by_slack_id="UADMIN",
            verified_at=datetime.now(timezone.utc),
        )

        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_ids": [
                    rule_line.statement_line_id,
                    unresolved_line.statement_line_id,
                ],
            },
            format="json",
        )

        self.assertEqual(started.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertTrue(started.data["retryable"])
        run = ContentFactoryRun.objects.get(run_id=started.data["run_id"])
        self.assertTrue(run.resume_available)
        self.assertEqual(
            XeroStatementSuggestion.objects.filter(run_id=run.run_id).count(),
            1,
        )

        retried = self.client.post(
            reverse("reconciliation_agent_run_retry", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
            },
            format="json",
        )

        self.assertEqual(retried.status_code, status.HTTP_200_OK)
        self.assertTrue(retried.data["valley_dispatched"])
        self.assertFalse(retried.data["idempotent"])
        run.refresh_from_db()
        self.assertFalse(run.resume_available)
        self.assertEqual((run.result or {})["_valley_meta"]["dispatch_status"], "queued")
        self.assertEqual(
            XeroStatementSuggestion.objects.filter(run_id=run.run_id).count(),
            1,
        )
        self.assertEqual(notify_valley.call_count, 2)

        already_queued = self.client.post(
            reverse("reconciliation_agent_run_retry", kwargs={"run_id": run.run_id}),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(already_queued.status_code, status.HTTP_200_OK)
        self.assertTrue(already_queued.data["idempotent"])
        self.assertFalse(already_queued.data["valley_dispatched"])
        self.assertEqual(notify_valley.call_count, 2)

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_reconciliation_retry_rejects_changed_statement_queue(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.return_value = ValleyHarnessResult(
            ok=False,
            failure_kind="connection",
            detail="Valley unavailable",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "retry-old-queue",
                "date": "20 Jul 2026",
                "narration": "Jaycar - Franklin",
                "direction": "debit",
                "amount": "95.05",
            }],
        )[0]
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )
        self.assertEqual(started.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "retry-new-queue",
                "date": "21 Jul 2026",
                "narration": "UBER *TRIP HELP.",
                "direction": "debit",
                "amount": "31.07",
            }],
        )

        retried = self.client.post(
            reverse(
                "reconciliation_agent_run_retry",
                kwargs={"run_id": started.data["run_id"]},
            ),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "confirm": True,
            },
            format="json",
        )

        self.assertEqual(retried.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("queue changed", retried.data["error"])
        notify_valley.assert_called_once()

    @patch("integrations.services.valley_harness.notify_valley_run_created")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_exact_outstanding_bill_defers_verified_spend_rule_to_valley(
        self, _permission, notify_valley
    ):
        from integrations.services.valley_harness import ValleyHarnessResult

        notify_valley.return_value = ValleyHarnessResult(
            ok=True,
            payload={"job_id": "job-bill", "status": "queued"},
        )
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-bill-deferral",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "agent-rule-with-bill",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        rule = ReconciliationRule.objects.create(
            organization=self.organization,
            name="Contractor spend default",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key=merchant_key(line.narration),
            direction=line.direction,
            contact_name="Contractor One",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="GST Free Expenses",
            description_template="Contractor work.",
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            verified_by_slack_id="UADMIN",
            verified_at=datetime.now(timezone.utc),
        )
        ExternalFinancialRecord.objects.create(
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
            connection=connection,
            user=self.user,
            organization=self.organization,
            external_record_id="bill-agent-845",
            external_account_id=connection.external_account_id,
            currency="AUD",
            amount=line.amount,
            direction="debit",
            status="AUTHORISED",
            transaction_date=line.transaction_date,
            merchant_name="Contractor One",
            class_name="ACCPAY",
        )

        response = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["deterministic_suggestion_count"], 0)
        self.assertEqual(response.data["deferred_bill_count"], 1)
        self.assertEqual(response.data["agent_line_count"], 1)
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(
            run.run_request["deferred_bill_statement_line_ids"],
            [line.statement_line_id],
        )
        self.assertFalse(XeroStatementSuggestion.objects.filter(
            run_id=run.run_id,
            statement_line=line,
        ).exists())
        context = build_statement_reconciliation_context(
            organization=self.organization,
            include_external_evidence=False,
        )
        candidate = next(
            item for item in context["statement_candidates"]
            if item["statement_line_id"] == line.statement_line_id
        )
        self.assertIsNone(candidate["verified_rule"])
        self.assertEqual(candidate["deferred_verified_rule"]["id"], rule.id)
        self.assertEqual(
            candidate["matching_xero_bills"][0]["xero_bill_id"],
            "bill-agent-845",
        )
        notify_valley.assert_called_once_with(run.run_id)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_complete_empty_scan_confirms_match_ready_posting_and_preserves_pattern(self, _permission):
        run, suggestion = self._agent_run_suggestion(
            run_id="xero-agent-outcome",
            line_id="contractor-outcome-845",
        )
        preview = build_statement_posting_preview(suggestion)
        posting = XeroStatementPosting.objects.get(pk=preview["posting_id"])
        posting.status = XeroStatementPosting.STATUS_MATCH_READY
        posting.xero_bank_transaction_id = "bt-confirmed-845"
        posting.posted_at = datetime.now(timezone.utc)
        posting.save(update_fields=[
            "status", "xero_bank_transaction_id", "posted_at", "updated_at",
        ])
        suggestion.status = XeroStatementSuggestion.STATUS_APPLIED
        suggestion.applied_at = datetime.now(timezone.utc)
        suggestion.save(update_fields=["status", "applied_at", "updated_at"])

        scan_response = self.client.post(
            reverse("reconciliation_statement_scans"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "bank_account_id": "bank-1",
                "expected_count": 0,
                "complete": True,
                "lines": [],
            },
            format="json",
        )

        self.assertEqual(scan_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(scan_response.data["scan"]["observed_count"], 0)
        self.assertEqual(scan_response.data["scan"]["confirmed_reconciled_count"], 1)
        posting.refresh_from_db()
        suggestion.statement_line.refresh_from_db()
        self.assertEqual(posting.status, XeroStatementPosting.STATUS_RECONCILED)
        self.assertIsNotNone(posting.reconciled_at)
        self.assertEqual(posting.reconciled_scan_id, scan_response.data["scan"]["id"])
        post_confirmation_preview = build_statement_posting_preview(suggestion)
        posting.refresh_from_db()
        self.assertFalse(post_confirmation_preview["ready"])
        self.assertEqual(posting.status, XeroStatementPosting.STATUS_RECONCILED)
        self.assertFalse(suggestion.statement_line.active)
        self.assertEqual(
            suggestion.statement_line.queue_state,
            XeroStatementLineSnapshot.QUEUE_RECONCILED,
        )
        self.assertTrue(ReconciliationDecision.objects.filter(
            suggestion=suggestion,
            decision_type=ReconciliationDecision.TYPE_RECONCILED_CONFIRMED,
        ).exists())

        new_line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "contractor-outcome-845-next",
                "date": "21 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        candidate = next(
            item
            for item in build_statement_reconciliation_context(
                organization=self.organization,
                include_external_evidence=False,
            )["statement_candidates"]
            if item["statement_line_id"] == new_line.statement_line_id
        )
        self.assertEqual(
            candidate["allowed_historical_patterns"][0]["outcome_source"],
            "confirmed_api_posting",
        )
        self.assertEqual(
            candidate["allowed_historical_patterns"][0]["account_code"],
            "405",
        )

        outcomes = self.client.get(
            reverse("reconciliation_outcomes"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(outcomes.status_code, status.HTTP_200_OK)
        self.assertEqual(outcomes.data["confirmed_reconciled_count"], 1)
        self.assertEqual(
            outcomes.data["recent_confirmed"][0]["xero_bank_transaction_id"],
            "bt-confirmed-845",
        )

    def test_empty_complete_scan_requires_explicit_zero_expected_count(self):
        with self.assertRaisesRegex(ValueError, "expected_count=0"):
            import_xero_statement_lines(
                organization=self.organization,
                bank_account_id="bank-1",
                lines=[],
                complete_scan=True,
                requested_by="UADMIN",
            )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_learning_candidate_blocks_inconsistent_descriptions(self, _permission):
        common = {
            "narration": "JAYCAR - FRANKLIN",
            "direction": "debit",
            "contact": "Jaycar Franklin",
            "account": "404 - Event supplies",
            "tax_type": "GST on Expenses",
        }
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            requested_by="UADMIN",
            lines=[
                {
                    **common,
                    "statement_line_id": "manual-jaycar-1",
                    "date": "24 May 2026",
                    "amount": "95.05",
                    "description": "Cables for Aaron AI.",
                },
                {
                    **common,
                    "statement_line_id": "manual-jaycar-2",
                    "date": "25 May 2026",
                    "amount": "20.00",
                    "description": "Parts for Present Studio.",
                },
            ],
        )
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=0,
            lines=[],
            requested_by="UADMIN",
        )

        outcomes = self.client.get(
            reverse("reconciliation_outcomes"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(outcomes.status_code, status.HTTP_200_OK)
        self.assertEqual(outcomes.data["rule_review_candidate_count"], 0)
        candidate = outcomes.data["learning_candidates"][0]
        self.assertFalse(candidate["eligible_for_rule_review"])
        self.assertFalse(candidate["eligible_for_promotion"])
        self.assertEqual(candidate["conflicting_pattern_count"], 1)
        self.assertIn("disagree", candidate["blocking_reasons"][0])

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_two_confirmed_manual_patterns_become_rule_review_candidate(self, _permission):
        linear_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="linear-learning-token",
            external_account_id="linear-learning",
        )
        project = LinearProjectArtifact.objects.create(
            connection=linear_connection,
            organization=self.organization,
            linear_project_id="watt-the-hack-project",
            name="[AI Week] Watt The Hack",
        )
        fields = {
            "narration": "UBER *TRIP HELP.",
            "direction": "debit",
            "contact": "Uber",
            "account": "406 - Travel-national",
            "description": "Uber trip for Watt The Hack.",
            "project_name": "[AI Week] Watt The Hack",
            "tax_type": "GST on Expenses",
        }
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=2,
            requested_by="UADMIN",
            lines=[
                {
                    **fields,
                    "statement_line_id": "manual-uber-1",
                    "date": "22 May 2026",
                    "amount": "26.08",
                },
                {
                    **fields,
                    "statement_line_id": "manual-uber-2",
                    "date": "23 May 2026",
                    "amount": "28.40",
                },
            ],
        )
        empty_scan = self.client.post(
            reverse("reconciliation_statement_scans"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "bank_account_id": "bank-1",
                "expected_count": 0,
                "complete": True,
                "lines": [],
            },
            format="json",
        )
        self.assertEqual(empty_scan.status_code, status.HTTP_201_CREATED)

        outcomes = self.client.get(
            reverse("reconciliation_outcomes"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(outcomes.status_code, status.HTTP_200_OK)
        self.assertEqual(outcomes.data["rule_review_candidate_count"], 1)
        candidate = outcomes.data["learning_candidates"][0]
        self.assertTrue(candidate["eligible_for_rule_review"])
        self.assertTrue(candidate["eligible_for_promotion"])
        self.assertEqual(candidate["confirmed_example_count"], 2)
        self.assertEqual(candidate["suggested_rule"]["contact_name"], "Uber")
        self.assertEqual(
            candidate["suggested_rule"]["project_source_id"],
            project.linear_project_id,
        )
        self.assertTrue(candidate["candidate_id"])
        self.assertTrue(candidate["candidate_version"])
        self.assertEqual(
            set(candidate["catalog_source_hashes"]),
            {"luma_events", "humanitix_events", "linear_projects"},
        )
        self.assertFalse(outcomes.data["automatic_rule_creation"])

        candidate_url = reverse(
            "reconciliation_learning_candidate",
            kwargs={"candidate_id": candidate["candidate_id"]},
        )
        preview = self.client.get(
            candidate_url,
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(
            preview.data["candidate"]["candidate_version"],
            candidate["candidate_version"],
        )

        stale = self.client.post(
            candidate_url,
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "decision": "promote",
                "candidate_version": "stale-version",
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(stale.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("changed after preview", stale.data["error"])

        LinearProjectArtifact.objects.create(
            connection=linear_connection,
            organization=self.organization,
            linear_project_id="unrelated-catalog-drift",
            name="Unrelated catalog addition",
        )
        catalog_stale = self.client.post(
            candidate_url,
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "decision": "promote",
                "candidate_version": candidate["candidate_version"],
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(catalog_stale.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("changed after preview", catalog_stale.data["error"])
        refreshed = self.client.get(
            candidate_url,
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        candidate = refreshed.data["candidate"]

        rejected = self.client.post(
            candidate_url,
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "decision": "reject",
                "candidate_version": candidate["candidate_version"],
                "reason": "Wait until the accountant confirms this recurring treatment.",
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, status.HTTP_200_OK)
        self.assertEqual(rejected.data["candidate"]["review_status"], "rejected")
        self.assertTrue(ReconciliationDecision.objects.filter(
            decision_type=ReconciliationDecision.TYPE_LEARNING_RULE_REJECTED,
            outcome__candidate_id=candidate["candidate_id"],
        ).exists())

        promoted = self.client.post(
            candidate_url,
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "decision": "promote",
                "candidate_version": candidate["candidate_version"],
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(promoted.status_code, status.HTTP_201_CREATED)
        self.assertEqual(promoted.data["candidate"]["review_status"], "promoted")
        self.assertFalse(promoted.data["idempotent"])
        rule = ReconciliationRule.objects.get(id=promoted.data["rule"]["id"])
        self.assertTrue(rule.active)
        self.assertEqual(rule.status, ReconciliationRule.STATUS_VERIFIED)
        self.assertEqual(rule.project_source_id, project.linear_project_id)
        self.assertEqual(rule.effective_from.isoformat(), "2026-05-22")
        self.assertEqual(rule.verified_by_slack_id, "UADMIN")
        self.assertTrue(ReconciliationDecision.objects.filter(
            decision_type=ReconciliationDecision.TYPE_LEARNING_RULE_PROMOTED,
            rule=rule,
        ).exists())

        repeated = self.client.post(
            candidate_url,
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "decision": "promote",
                "candidate_version": candidate["candidate_version"],
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(repeated.status_code, status.HTTP_200_OK)
        self.assertTrue(repeated.data["idempotent"])
        self.assertEqual(ReconciliationRule.objects.count(), 1)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_profile_and_mapping_configuration_endpoints(self, _permission):
        profile_response = self.client.get(
            reverse("reconciliation_profile"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(profile_response.status_code, status.HTTP_200_OK)
        self.assertFalse(profile_response.data["profile"]["xero_write_scope"])
        self.assertFalse(
            profile_response.data["profile"][
                "humanitix_profitability_included"
            ]
        )

        unconfirmed_policy = self.client.put(
            reverse("reconciliation_profile"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "humanitix_profitability_included": True,
            },
            format="json",
        )
        self.assertEqual(
            unconfirmed_policy.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        confirmed_policy = self.client.put(
            reverse("reconciliation_profile"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "humanitix_profitability_included": True,
                "confirm": True,
            },
            format="json",
        )
        self.assertEqual(confirmed_policy.status_code, status.HTTP_200_OK)
        self.assertTrue(
            confirmed_policy.data["profile"][
                "humanitix_profitability_included"
            ]
        )
        self.assertEqual(
            confirmed_policy.data["profile"][
                "profitability_policy_verified_by_slack_id"
            ],
            "UADMIN",
        )
        self.assertIsNotNone(
            confirmed_policy.data["profile"][
                "profitability_policy_verified_at"
            ]
        )

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

        mixed_mapping = self.client.put(
            reverse("reconciliation_mappings"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "source_type": "luma_event",
                "source_id": "evt_api",
                "project_source_type": "linear",
                "project_source_id": "project-api",
                "project_tracking_option_name": "API Project",
            },
            format="json",
        )
        self.assertEqual(mixed_mapping.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Event xor Project", mixed_mapping.data["error"])

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
        missing_hash = self.client.post(
            reverse("reconciliation_payout_post", kwargs={"payout_id": "po_api"}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
            format="json",
        )
        self.assertEqual(missing_hash.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payload_hash", missing_hash.data["error"])

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch("integrations.api_views_reconciliation.build_xero_correction_batch")
    def test_payout_correction_preview_is_read_only_and_admin_guarded(
        self,
        build_batch,
        _permission,
    ):
        StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po_correction",
            arrival_date=datetime(2026, 6, 30).date(),
            amount_cents=9200,
            currency="AUD",
        )
        build_batch.return_value = {
            "payout_count": 1,
            "classification_counts": {"legacy_net_only": 1},
            "payouts": [{"payout_id": "po_correction"}],
            "event_revenue": [],
        }
        response = self.client.post(
            reverse("reconciliation_payout_correction_preview"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "since": "2026-01-01",
                "until": "2026-06-30",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertFalse(response.data["xero_writes"])
        records = build_batch.call_args.args[0]
        self.assertEqual([record.payout_id for record in records], ["po_correction"])
        self.assertEqual(
            build_batch.call_args.kwargs["cashflow_period_start"],
            date(2026, 1, 1),
        )
        self.assertEqual(
            build_batch.call_args.kwargs["cashflow_period_end"],
            date(2026, 6, 30),
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch(
        "integrations.api_views_reconciliation."
        "build_humanitix_xero_correction_batch"
    )
    def test_humanitix_correction_preview_is_read_only(
        self,
        build_batch,
        _permission,
    ):
        build_batch.return_value = {
            "payouts": [],
            "summary": {
                "payout_count": 0,
                "safe_action_count": 0,
                "manual_unreconcile_count": 0,
            },
        }

        response = self.client.post(
            reverse("reconciliation_humanitix_payout_correction_preview"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertFalse(response.data["xero_writes"])
        self.assertEqual(response.data["summary"]["payout_count"], 0)
        build_batch.assert_called_once()

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_statement_preview_and_safe_batch_are_admin_guarded(self, _permission):
        connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=connection,
            xero_bank_account_id="bank-1",
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "api-statement-1",
                "date": "16 Jul 2026",
                "narration": "UBER *TRIP HELP.",
                "direction": "debit",
                "amount": "31.07",
                "currency": "AUD",
            }],
        )[0]
        suggestion = XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=line,
            run_id="api-run",
            proposed_action="create_bank_transaction",
            contact_name="uber",
            account_code="406",
            account_name="Travel-national",
            tax_type="INPUT",
            description="Uber trip.",
            confidence=0.99,
            execution_ready=True,
            source_hash=line.source_hash,
        )

        preview = self.client.get(
            reverse("reconciliation_statement_suggestion_preview", kwargs={"suggestion_id": suggestion.id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertTrue(preview.data["preview"]["ready"])

        unconfirmed = self.client.post(
            reverse("reconciliation_statement_suggestion_execute", kwargs={"suggestion_id": suggestion.id}),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": False},
            format="json",
        )
        self.assertEqual(unconfirmed.status_code, status.HTTP_400_BAD_REQUEST)

        batch = self.client.post(
            reverse("reconciliation_statement_safe_batch"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "dry_run": True},
            format="json",
        )
        self.assertEqual(batch.status_code, status.HTTP_200_OK)
        self.assertEqual(batch.data["ready_count"], 1)
        self.assertEqual(batch.data["posted_count"], 0)

        excluded = self.client.post(
            reverse("reconciliation_statement_safe_batch"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "dry_run": True,
                "exclude_statement_line_ids": [line.statement_line_id],
            },
            format="json",
        )
        self.assertEqual(excluded.status_code, status.HTTP_200_OK)
        self.assertEqual(excluded.data["excluded_statement_line_ids"], [line.statement_line_id])
        self.assertEqual(excluded.data["candidate_count"], 0)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_valley_context_submission_and_human_approval_contract(self, _permission):
        ContentFactoryRun.objects.create(
            run_id="monthly-agent",
            workflow="startup_monthly_update",
            domain=self.organization.domain,
            organization=self.organization,
            run_request={
                "startup_memory": {
                    "facts": [{
                        "id": "memory-fixture-1",
                        "summary": "Fixture company relationship.",
                    }]
                }
            },
        )
        luma_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LUMA,
            user=self.user,
            organization=self.organization,
            access_token="luma-token",
            external_account_id="luma-agent",
        )
        linear_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="linear-token",
            external_account_id="linear-agent",
        )
        LumaEventSelection.objects.create(
            connection=luma_connection,
            user=self.user,
            organization=self.organization,
            event_id="evt_agent",
            event_name="Agent Night",
        )
        LinearProjectArtifact.objects.create(
            connection=linear_connection,
            organization=self.organization,
            linear_project_id="lin_agent",
            name="Agent Night",
        )
        LinearProjectArtifact.objects.create(
            connection=linear_connection,
            organization=self.organization,
            linear_project_id="lin_agent_project",
            name="Community Events",
        )
        payout = StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po_agent",
            source_hash="stable-hash",
            amount_cents=9700,
            currency="AUD",
            report_payload={
                "revenue_groups": [{
                    "source_type": "luma_event",
                    "source_id": "evt_agent",
                    "source_label": "Agent Night",
                    "gross_cents": 10000,
                    "stripe_fee_cents": 300,
                }],
                },
            )

        context_response = self.client.get(
            reverse("reconciliation_enrichment_context"),
            {"domain": "mlai.au", "run_id": "monthly-agent"},
        )
        self.assertEqual(context_response.status_code, status.HTTP_200_OK)
        projects = {item["source_id"]: item for item in context_response.data["linear_projects"]}
        self.assertEqual(projects["lin_agent"]["dimension_hint"], "event_mirror")
        self.assertEqual(projects["lin_agent_project"]["dimension_hint"], "project")
        self.assertEqual(
            context_response.data["startup_memory"]["facts"][0]["id"],
            "memory-fixture-1",
        )
        self.assertEqual(
            context_response.data["startup_memory_provenance"],
            {
                "source": "content_factory_run_request",
                "source_run_id": "monthly-agent",
                "present": True,
            },
        )
        self.assertEqual(context_response.data["catalog_status"]["counts"]["luma_events"], 1)
        self.assertFalse(context_response.data["catalog_status"]["drift_detected"])

        submission_response = self.client.post(
            reverse("reconciliation_enrichment_context"),
            {
                "domain": "mlai.au",
                "run_id": "monthly-agent",
                "model_name": "gpt-reasoner",
                "suggestions": [{
                    "payout_id": payout.payout_id,
                    "source_type": "luma_event",
                    "source_id": "evt_agent",
                    "allocation_mode": "event",
                    "event": {"source_type": "luma", "source_id": "evt_agent"},
                    "confidence": 0.97,
                    "review_note": "Agent Night ticket revenue, corroborated by Slack and email planning context.",
                    "evidence": [{"source_provider": "gmail", "source_record_id": "thread-agent"}],
                }],
            },
            format="json",
        )
        self.assertEqual(submission_response.status_code, status.HTTP_200_OK)
        self.assertFalse(submission_response.data["automatic_posting_enabled"])
        self.assertEqual(submission_response.data["automatic_postings"], [])
        suggestion_id = submission_response.data["suggestions"][0]["id"]

        decision_response = self.client.post(
            reverse("reconciliation_suggestion_decision", kwargs={"suggestion_id": suggestion_id}),
            {"domain": "mlai.au", "slack_user_id": "UADMIN", "decision": "approve"},
            format="json",
        )
        self.assertEqual(decision_response.status_code, status.HTTP_200_OK)
        self.assertEqual(decision_response.data["mapping"]["event_tracking_option_name"], "Agent Night")
        self.assertEqual(decision_response.data["mapping"]["project_source_id"], "")
        self.assertEqual(decision_response.data["mapping"]["project_tracking_option_name"], "")
