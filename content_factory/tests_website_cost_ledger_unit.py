"""Accounting rollout, read scope and error isolation without DB or network."""
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory
from . import website_cost_ledger as ledger
from .website_views import WebsiteCostUsageView, WebsiteConnectionOperationView


class LedgerBoundaryTests(SimpleTestCase):
    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=False)
    def test_feature_off_observation_and_read_do_not_touch_the_ledger(self):
        context = SimpleNamespace(company=SimpleNamespace(pk="company"), organization=SimpleNamespace(pk=7))
        with patch.object(ledger, "record") as record, patch.object(ledger, "read") as read, \
                patch("content_factory.website_views._context", return_value=(context, None, None)):
            ledger.observe(SimpleNamespace())
            result = WebsiteCostUsageView().get(SimpleNamespace(query_params={}))
        self.assertEqual(result.status_code, 404)
        record.assert_not_called()
        read.assert_not_called()

    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
    def test_accounting_failure_cannot_stop_the_goal_or_log_provider_text(self):
        with patch.object(ledger, "record", side_effect=ValueError("private provider details")), \
                self.assertLogs(ledger.logger, level="WARNING") as logs:
            ledger.observe(SimpleNamespace())
        self.assertEqual(logs.output, ["WARNING:content_factory.website_cost_ledger:articles_usage_observation_incomplete"])

    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
    def test_detail_read_has_explicit_scope_and_rejects_malformed_goal(self):
        context = SimpleNamespace(company=SimpleNamespace(pk="company"), organization=SimpleNamespace(pk=7))
        with patch("content_factory.website_views._context", return_value=(context, None, None)), \
                patch.object(ledger, "read", return_value={"recordedRuns": 0}) as read:
            request = SimpleNamespace(query_params={})
            result = WebsiteCostUsageView().get(request)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result["Cache-Control"], "private, no-store")
            read.assert_called_once_with(company_id="company", organization_id=7, goal_id=None)
            read.reset_mock()
            for invalid in ("foreign-secret", "1", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"):
                request.query_params = {"goal_id": invalid}
                self.assertEqual(WebsiteCostUsageView().get(request).status_code, 400)
            read.assert_not_called()

    def test_context_denial_precedes_any_accounting_read(self):
        from rest_framework.response import Response
        with patch("content_factory.website_views._context", return_value=(None, None, Response(status=404))), \
                patch.object(ledger, "read") as read:
            self.assertEqual(WebsiteCostUsageView().get(APIRequestFactory().get("/")).status_code, 404)
        read.assert_not_called()

    def test_internal_rows_cannot_be_read_through_generic_operation_receipts(self):
        context = SimpleNamespace(organization=SimpleNamespace(pk=7))
        with patch("content_factory.website_views._context", return_value=(context, None, None)), \
                patch("content_factory.website_models.WebsiteConnectionOperation.objects.filter") as rows:
            for action in ("goal-usage", "goal-alert"):
                rows.return_value.first.return_value = SimpleNamespace(action=action)
                self.assertEqual(WebsiteConnectionOperationView().get(None, "internal").status_code, 404)
