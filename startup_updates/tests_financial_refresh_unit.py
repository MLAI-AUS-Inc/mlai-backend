"""Financial refresh contracts without a database or real provider requests."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from rest_framework.exceptions import ValidationError

from startup_updates.financial_refresh import _equivalent_scope, request_scope
from startup_updates.financial_refresh_sources import (
    apply_stage, build_candidate_evidence, failure_details, fetch_stage, month_slots, next_stage,
)
from startup_updates.financial_history import build_snapshot, history_owner_preview, history_for_revision
from startup_updates.tests_financial_history_unit import evidence_payload, report_payload


class FinancialRefreshUnitTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
        self.company = SimpleNamespace(pk=uuid4(), organization_id=4)
        self.user = SimpleNamespace(pk=7)
        self.profile = SimpleNamespace(default_currency="AUD", reporting_timezone="Australia/Melbourne", reporting_config_version=1)
        self.data = {"requestId": str(uuid4()), "creationKey": str(uuid4()), "targetMonth": "2026-08-01", "inputSources": ["stripe", "xero"]}

    def scope(self, **changes):
        return request_scope(self.company, self.user, {**self.data, **changes}, self.profile, now=self.now)

    def test_pinned_month_window_currency_and_timezone(self):
        request = self.scope()
        self.assertEqual(request["coverageEnd"], "2026-08-31")
        self.assertEqual(request["period"]["cutoff"], "2026-09-01T00:00:00+10:00")
        self.assertEqual(request["currency"], "AUD")
        self.assertEqual([month.isoformat() for month in month_slots(date(2026, 1, 1))],
            ["2025-08-01", "2025-09-01", "2025-10-01", "2025-11-01", "2025-12-01", "2026-01-01"])

    def test_current_month_keeps_partial_cutoff(self):
        request = self.scope(targetMonth="2026-11-01")
        self.assertTrue(request["period"]["is_partial"])
        self.assertEqual(request["coverageEnd"], "2026-11-03")

    def test_rejects_bad_month_identity_revision_and_sources(self):
        for changes in ({"targetMonth": "2026-12-01"}, {"targetMonth": "2026-08-02"},
                {"requestId": "bad"}, {"creationKey": None}, {"updateId": True},
                {"expectedRevision": 8}, {"inputSources": ["stripe"]}, {"inputSources": ["xero", "gmail"]},
                {"inputSources": [None]}, {"inputSources": "xero"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.scope(**changes)

    def test_equivalent_clicks_ignore_time_but_not_scope_or_connections(self):
        original = self.scope()
        duplicate = {**original, "requestId": str(uuid4()), "period": {"later": True}}
        self.assertTrue(_equivalent_scope(original, duplicate))
        for key, value in (("userId", 9), ("targetMonth", "2026-07-01"), ("expectedRevision", 8),
                ("connections", {"xero": {"id": 3}}), ("currency", "USD")):
            self.assertFalse(_equivalent_scope(original, {**duplicate, key: value}))

    def test_xero_read_uses_accrual_and_selected_month_end(self):
        connection = SimpleNamespace(access_token="synthetic", scopes=["accounting.reports.profitandloss.read"])
        with patch("startup_updates.financial_refresh_sources.fetch_xero_accounting_report", return_value=report_payload(100, 50)) as fetch:
            receipt = fetch_stage("xero", "2026-03-01", connection, self.scope(), {})
        self.assertEqual(receipt["end"], "2026-03-31")
        self.assertEqual(fetch.call_args.kwargs["params"], {"fromDate": "2026-03-01", "toDate": "2026-03-31",
            "paymentsOnly": "false", "standardLayout": "true"})

    def test_currency_mismatch_cannot_be_relabelled(self):
        connection = SimpleNamespace(access_token="synthetic", scopes=["accounting.reports.read"])
        with patch("startup_updates.financial_refresh_sources.fetch_xero_base_currency", return_value="USD"), self.assertRaises(ValueError):
            fetch_stage("xero", "currency", connection, self.scope(), {})

    def test_incomplete_stripe_pagination_fails_without_repeating_cursor(self):
        connection = SimpleNamespace(access_token="synthetic")
        response = Mock()
        response.json.return_value = {"data": [{"id": "in_same"}], "has_more": True}
        with patch("startup_updates.financial_refresh_sources.requests.get", return_value=response), self.assertRaises(ValueError):
            fetch_stage("stripe", "invoices", connection, self.scope(), {"months": ["2026-03-01"], "stripe": {"cursor": "in_same"}})

    def test_stripe_receipts_do_not_retain_invoice_customer_data(self):
        progress = {"sources": {"stripe": {"status": "pending"}}}
        updated = apply_stage(progress, "stripe", "invoices", {"count": 2, "hash": "receipt-hash", "cursor": "in_next", "more": False}, now=self.now)
        self.assertEqual(updated["stripe"], {"count": 2, "pages": 1, "cursor": "in_next", "pageHashes": ["receipt-hash"]})
        self.assertEqual(updated["sources"]["stripe"]["status"], "ready")
        self.assertEqual(progress, {"sources": {"stripe": {"status": "pending"}}})

    def test_stage_selection_prioritizes_accounting_and_resumes_completed_reports(self):
        progress = {"sources": {"stripe": {"status": "pending"}, "xero": {"status": "refreshing"}},
            "months": ["2026-03-01", "2026-04-01"], "xero": {"currency": "AUD", "reports": {"2026-03-01": {}}}}
        self.assertEqual(next_stage(progress, ["stripe", "xero"]), ("xero", "2026-04-01"))

    def test_reports_preserve_direct_costs_and_never_add_stripe(self):
        request = {**self.scope(), "connections": {"xero": {"id": 3}}}
        progress = {"months": [month.isoformat() for month in month_slots(date(2026, 8, 1))],
            "sources": {"xero": {"status": "ready"}, "stripe": {"status": "ready"}}, "xero": {"reports": {}},
            "stripe": {"count": 99999}}
        for month, old in zip(progress["months"], evidence_payload()["charts"]["performance"]):
            report = report_payload(100, 50)
            report["Reports"][0]["Rows"][1]["Cells"][0]["Value"] = "Total Operating Expenses"
            report["Reports"][0]["Rows"].append({"RowType": "SummaryRow", "Cells": [{"Value": "Total Cost of Sales"}, {"Value": "20"}]})
            progress["xero"]["reports"][month] = {"report": report, "end": old["metric_evidence"]["income"]["report_end_date"]}
        payload = build_candidate_evidence(request, progress)
        chart = build_snapshot(payload, date(2026, 8, 1), captured_at=self.now.isoformat())
        self.assertEqual(chart["points"][-1]["revenue"], 100)
        self.assertEqual(chart["points"][-1]["costs"], 70)

    def test_separate_candidate_preserves_exact_preview_and_disabled_owner_access(self):
        captured = "2026-11-03T06:00:00+00:00"
        payload = evidence_payload()
        payload["financial_refresh"] = {"capturedAt": captured}
        candidate = SimpleNamespace(pk=8, content_hash="hash", payload=payload)
        memo = {"summary": "My unsaved words", "_financial_history_selection": {"enabled": False, "shareAmounts": False}}
        history = history_for_revision(SimpleNamespace(month=date(2026, 8, 1)), memo, None, None,
            captured_at="2026-11-04T06:00:00+00:00", financial_history_snapshot=candidate)
        self.assertFalse(history["enabled"])
        self.assertEqual(history_owner_preview(history)["capturedAt"], captured)
        self.assertEqual(history_owner_preview(history)["points"][-1]["revenue"], 600)
        self.assertIsNone(history_owner_preview(history, share_amounts=False)["currency"])
        self.assertEqual(memo["summary"], "My unsaved words")

    def test_provider_failure_never_echoes_secrets(self):
        from integrations.services.external_connectors import ConnectorOAuthError
        failure, retryable, _delay = failure_details(ConnectorOAuthError("secret-token-and-data"), "stripe")
        self.assertEqual(failure["status"], "reconnect_required")
        self.assertFalse(retryable)
        self.assertNotIn("secret", str(failure))

    def test_temporary_connector_failure_and_quota_are_retryable_without_reconnect(self):
        from integrations.services.external_connectors import ConnectorRateLimitError, ConnectorTemporarilyUnavailable
        for error in (ConnectorRateLimitError(120), ConnectorTemporarilyUnavailable("xero", "upstream")):
            failure, retryable, delay = failure_details(error, "xero")
            self.assertTrue(retryable)
            self.assertEqual(failure["status"], "failed")
            self.assertNotIn("Reconnect", failure["detail"])
            if isinstance(error, ConnectorRateLimitError):
                self.assertEqual(delay, 120)

    def test_daily_quota_fails_fast_with_local_deadline(self):
        from integrations.services.external_connectors import ConnectorRateLimitError
        now = datetime(2026, 11, 6, 10, 46, tzinfo=timezone.utc)
        with patch("startup_updates.financial_refresh_sources.timezone.now", return_value=now):
            failure, retryable, wait = failure_details(
                ConnectorRateLimitError(8000), "xero", tz_name="Australia/Sydney", now=now)
        self.assertFalse(retryable)
        self.assertEqual(wait, 8000)
        self.assertEqual(failure["reason"], "rate_limited")
        self.assertEqual(failure["retryAt"], "2026-11-06T12:59:20+00:00")
        self.assertIn("daily API limit", failure["detail"])
        self.assertIn("Try again after 11:59 pm.", failure["detail"])

    def test_quota_429_response_uses_retry_after_and_names_next_day(self):
        response = Mock(status_code=429, headers={"Retry-After": "7200"})
        now = datetime(2026, 11, 6, 12, 30, tzinfo=timezone.utc)
        with patch("startup_updates.financial_refresh_sources.timezone.now", return_value=now):
            failure, retryable, _wait = failure_details(
                requests_error(response), "xero", tz_name="Australia/Sydney", now=now)
        self.assertFalse(retryable)
        self.assertIn("Try again after 1:30 am on Sat 7 Nov.", failure["detail"])

    def test_short_quota_still_retries(self):
        response = Mock(status_code=429, headers={"Retry-After": "45"})
        failure, retryable, delay = failure_details(requests_error(response), "xero")
        self.assertTrue(retryable)
        self.assertEqual(delay, 45)
        self.assertNotIn("reason", failure)


def requests_error(response):
    from integrations import http_client
    return http_client.HTTPError(response=response)
