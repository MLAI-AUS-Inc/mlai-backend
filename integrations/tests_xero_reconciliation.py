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
    ExternalFinancialRecord,
    ExternalServiceConnection,
    ExternalServiceProvider,
    GoogleConnection,
    ReconciliationMapping,
    ReconciliationProfile,
    ReconciliationSuggestion,
    StripePayoutReconciliation,
    XeroStatementLineSnapshot,
    XeroStatementSuggestion,
)
from startup_updates.models import (
    GmailMessageArtifact,
    LinearProjectArtifact,
    LumaEventSelection,
    SlackMessageArtifact,
)
from integrations.services.reconciliation import (
    DEFAULT_STRIPE_API_VERSION,
    ReconciliationReportService,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
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
from integrations.services.xero_statement_reconciliation import (
    build_statement_reconciliation_context,
    import_xero_statement_lines,
    save_statement_suggestions,
)
from integrations.tests_reconciliation import FakeSession
from roo.models import PointsAdmin
from workflow_runs.models import ContentFactoryRun


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
            scopes=["offline_access", "accounting.banktransactions", "accounting.settings"],
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

    def test_explicit_post_creates_missing_project_tracking_option(self):
        record = persist_report(organization=self.organization, report=self.report, stripe_account_id="acct_main")[0]
        self.mapping.project_source_type = "linear"
        self.mapping.project_source_id = "lin_project_1"
        self.mapping.project_tracking_option_name = "Community Events"
        self.mapping.save(update_fields=[
            "project_source_type",
            "project_source_id",
            "project_tracking_option_name",
            "updated_at",
        ])
        categories = Mock()
        categories.json.return_value = {
            "TrackingCategories": [
                {
                    "TrackingCategoryID": "project-category-1",
                    "Name": "Project Name",
                    "Options": [],
                }
            ]
        }
        categories.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {
            "Options": [{"TrackingOptionID": "project-option-1", "Name": "Community Events"}]
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
                "project": {"source_type": "linear", "source_id": "lin_project_1"},
                "confidence": 0.96,
                "rationale": "The Luma and Linear names match and Slack confirms the event workstream.",
                "review_note": "Ticket revenue for the Luma Night project; confirmed in the event planning thread.",
                "evidence": [{"source_provider": "slack", "source_record_id": "thread-1", "summary": "Event planning"}],
            }],
        )[0]
        self.assertEqual(suggestion.status, ReconciliationSuggestion.STATUS_PROPOSED)
        approved, mapping = approve_reconciliation_suggestion(suggestion, reviewed_by_slack_id="UFIN")
        self.assertEqual(approved.status, ReconciliationSuggestion.STATUS_APPROVED)
        self.assertEqual(mapping.project_source_id, "lin_project_1")
        self.assertEqual(mapping.project_tracking_option_name, "Community Events")

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
        self.assertEqual(retried.project_source_id, "lin_project_1")

        preview = build_xero_preview(record)
        revenue_line = preview["xero_payload"]["LineItems"][0]
        self.assertEqual([item["Name"] for item in revenue_line["Tracking"]], ["Event Name", "Project Name"])
        self.assertIn("Project: Community Events", revenue_line["Description"])
        self.assertIn("confirmed in the event planning thread", revenue_line["Description"])
        self.assertEqual(preview["context_notes"][0]["project_name"], "Community Events")

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

        saved = save_statement_suggestions(
            organization=self.organization,
            run_id="monthly-statement-1",
            suggestions=[
                {
                    "statement_line_id": "blank-uber",
                    "proposed_action": "prefill_create",
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
                    "proposed_action": "match_existing_bill",
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

        with self.assertRaisesMessage(ValueError, "not backed by an exact historical Xero pattern"):
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

    def test_statement_context_finds_date_amount_and_merchant_evidence(self):
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            lines=[{
                "statement_line_id": "jetstar-1",
                "date": "20 Jul 2026",
                "narration": "JETSTAR AIRWAYS Card xx1336",
                "reference": "POS",
                "direction": "debit",
                "amount": "362.20",
                "has_ok": False,
            }],
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

        context = build_statement_reconciliation_context(organization=self.organization)
        candidate = context["statement_candidates"][0]
        evidence = candidate["context_evidence"]
        self.assertEqual({item["source_provider"] for item in evidence}, {"gmail", "slack"})
        self.assertNotIn("gmail-unrelated", {item["source_record_id"] for item in evidence})
        gmail_evidence = next(item for item in evidence if item["source_provider"] == "gmail")
        self.assertIn("amount:362.20", gmail_evidence["match_reasons"])
        self.assertIn("Jetstar itinerary", gmail_evidence["summary"])


class ReconciliationWorkflowApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="agent@example.com", slack_id="UAGENT")
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

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_valley_context_submission_and_human_approval_contract(self, _permission):
        ContentFactoryRun.objects.create(
            run_id="monthly-agent",
            workflow="startup_monthly_update",
            domain=self.organization.domain,
            organization=self.organization,
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
                    "event": {"source_type": "luma", "source_id": "evt_agent"},
                    "project": {"source_type": "linear", "source_id": "lin_agent_project"},
                    "confidence": 0.97,
                    "review_note": "Agent Night ticket revenue, corroborated by Slack and email planning context.",
                    "evidence": [{"source_provider": "gmail", "source_record_id": "thread-agent"}],
                }],
            },
            format="json",
        )
        self.assertEqual(submission_response.status_code, status.HTTP_200_OK)
        suggestion_id = submission_response.data["suggestions"][0]["id"]

        decision_response = self.client.post(
            reverse("reconciliation_suggestion_decision", kwargs={"suggestion_id": suggestion_id}),
            {"domain": "mlai.au", "slack_user_id": "UADMIN", "decision": "approve"},
            format="json",
        )
        self.assertEqual(decision_response.status_code, status.HTTP_200_OK)
        self.assertEqual(decision_response.data["mapping"]["project_source_id"], "lin_agent_project")
        self.assertEqual(decision_response.data["mapping"]["project_tracking_option_name"], "Community Events")
