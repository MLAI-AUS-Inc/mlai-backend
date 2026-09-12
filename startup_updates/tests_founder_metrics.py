"""Pure contract checks: python3.11 -m unittest startup_updates.tests_founder_metrics."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from startup_updates.founder_metrics import founder_metric_changes, FinancialMetricEditError, snapshot_for_founder_edit


class FounderMetricTests(unittest.TestCase):
    def setUp(self):
        self.metrics = [
            {"key": "revenue", "label": "Revenue", "value": "1234.50", "display_value": "AUD 1234.50", "unit": "AUD", "source_provider": "xero"},
            {"key": "monthlyCosts", "value": "0", "display_value": "AUD 0", "unit": "AUD", "source_provider": "xero"},
            {"key": "activeUsers", "value": "3", "display_value": "3", "source_provider": "google_analytics"},
        ]

    def test_equivalent_financial_displays_retain_evidence(self):
        for value in ["AUD 1234.50", "1234.5", "1,234.50", 1234.5]:
            self.assertEqual(founder_metric_changes({"revenue": value}, self.metrics), {})

    def test_changed_cleared_and_wrong_currency_are_rejected(self):
        for value in ["1234.51", "", None, "USD 1234.50", "claimed 1234.50", "NaN", "Infinity"]:
            with self.subTest(value=value), self.assertRaises(FinancialMetricEditError):
                founder_metric_changes({"revenue": value}, self.metrics)

    def test_omitted_metrics_and_zero_are_preserved(self):
        self.assertEqual(founder_metric_changes({}, self.metrics), {})
        self.assertEqual(founder_metric_changes({"monthlyCosts": "0"}, self.metrics), {})
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"monthlyCosts": ""}, self.metrics)

    def test_unknown_is_not_zero(self):
        unknown = [{"key": "revenue", "value": None, "display_value": None}]
        self.assertEqual(founder_metric_changes({"revenue": ""}, unknown), {})
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"revenue": "0"}, unknown)
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"revenue": "100"}, [])

    def test_manual_nonfinancial_changes_and_removal(self):
        self.assertEqual(founder_metric_changes({"activeUsers": "4", "interviews": "8"}, self.metrics), {"activeUsers": "4", "interviews": "8"})
        self.assertEqual(founder_metric_changes({"activeUsers": ""}, self.metrics), {"activeUsers": ""})

    def test_custom_imported_financial_metric_is_locked(self):
        metric = [{"key": "ticketSales", "value": "5", "display_value": "AUD 5", "unit": "AUD", "source_provider": "financial"}]
        with self.assertRaisesRegex(FinancialMetricEditError, "Stripe"):
            founder_metric_changes({"ticketSales": "6"}, metric)

    def test_legacy_assertion_is_preserved_without_relabelling(self):
        legacy = [{"key": "revenue", "value": "10", "display_value": "10", "source_provider": "founder", "quality": "founder_asserted"}]
        original = copy.deepcopy(legacy)
        self.assertEqual(founder_metric_changes({"revenue": "10"}, legacy), {})
        self.assertEqual(legacy, original)
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"revenue": "11"}, legacy)

    def test_no_provider_or_previous_value_can_override_canonical_snapshot(self):
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"revenue": "9"}, self.metrics, {"revenue": "9"})


class FounderSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(payload={"metrics": [{"key": "revenue", "display_value": "AUD 10", "value": "10", "unit": "AUD", "source_provider": "xero"}], "period": {"cutoff": "2026-09-11T01:02:00Z"}, "charts": {"performance": [{"net": 10, "expenses": None}]}})
        self.draft = SimpleNamespace(current_revision_id=4, current_revision=SimpleNamespace(snapshot=self.snapshot))
        self.capture = Mock()

    def save(self, incoming):
        return snapshot_for_founder_edit(organization="org", month="2026-09", draft=self.draft, incoming=incoming, previous={}, capture=self.capture)

    def test_writing_cover_and_equivalent_figures_reuse_exact_snapshot(self):
        for incoming in ({}, {"revenue": "10"}, {"revenue": "AUD 10"}):
            snapshot, changes = self.save(incoming)
            self.assertIs(snapshot, self.snapshot)
            self.assertEqual(changes, {})
        self.capture.assert_not_called()

    def test_financial_tampering_cannot_create_a_snapshot(self):
        with self.assertRaises(FinancialMetricEditError):
            self.save({"revenue": "11"})
        self.capture.assert_not_called()

    def test_nonfinancial_edit_passes_the_frozen_base_to_capture(self):
        result, changes = self.save({"customerInterviews": "12"})
        self.assertIs(result, self.capture.return_value)
        self.capture.assert_called_once_with("org", "2026-09", manual_metrics={"customerInterviews": "12"}, base_snapshot=self.snapshot)

    def test_first_save_uses_server_observations(self):
        self.draft.current_revision_id = None
        self.capture.return_value = self.snapshot
        result, _ = self.save({"revenue": "10"})
        self.assertIs(result, self.snapshot)
        self.capture.assert_called_once_with("org", "2026-09")
