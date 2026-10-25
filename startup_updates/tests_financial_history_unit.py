"""Pure financial-history contracts: no database, credentials or provider calls."""
import copy
import unittest
from datetime import date
from types import SimpleNamespace

from startup_updates.evidence_contract import content_hash
from startup_updates.financial_history import (
    FinancialHistoryError, build_snapshot, generated_history_selection, history_chart_context, history_for_revision,
    history_payload, history_selection, validate_selection,
)
from startup_updates.disclosure import shared_update


MONTH = date(2026, 8, 1)
CAPTURED = "2026-10-25T03:12:00+00:00"


def report_payload(revenue, costs):
    return {"Reports": [{"ReportID": "ProfitAndLoss", "Rows": [
        {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": str(revenue)}]},
        {"RowType": "SummaryRow", "Cells": [{"Value": "Total Expenses"}, {"Value": str(costs)}]},
    ]}]}


def evidence_payload():
    rows = []
    reports = {}
    ends = ("2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31")
    for index, end in enumerate(ends):
        start = end[:8] + "01"
        report = report_payload((index + 1) * 100, (index + 1) * 50)
        report_hash = content_hash(report)
        reports[report_hash] = report
        evidence = {"source_provider": "xero", "unit": "AUD", "accounting_basis": "accrual",
            "connection_id": 7, "report_hash": report_hash, "report_start_date": start,
            "report_end_date": end, "timezone": "Australia/Melbourne"}
        rows.append({"month": start, "income": (index + 1) * 100, "expenses": (index + 1) * 50,
            "metric_evidence": {"income": {**evidence, "source_metric": "xero_profit_and_loss_revenue"},
                "expenses": {**evidence, "source_metric": "xero_profit_and_loss_monthly_costs"}}})
    return {"period": {"month": "2026-08-01", "timezone": "Australia/Melbourne",
        "cutoff": "2026-09-01T00:00:00+10:00", "is_partial": False}, "metrics": [],
        "charts": {"schema_version": 2, "target_month": "2026-08-01", "currency": "AUD",
            "source_reports": reports, "performance": rows, "data_quality": {"warnings": []}}}


def replace_totals(payload, index, revenue, costs):
    row = payload["charts"]["performance"][index]
    row["income"], row["expenses"] = revenue, costs
    report = report_payload(revenue, costs)
    report_hash = content_hash(report)
    payload["charts"]["source_reports"][report_hash] = report
    for evidence in row["metric_evidence"].values():
        evidence["report_hash"] = report_hash


def frozen(payload=None, share=False):
    return {"enabled": True, "shareAmounts": share,
        "snapshot": build_snapshot(payload or evidence_payload(), MONTH, captured_at=CAPTURED)}


class FinancialHistoryUnitTests(unittest.TestCase):
    def test_exact_six_calendar_months_reporting_month_and_full_month_cutoff(self):
        chart = history_payload(frozen())
        self.assertEqual(chart["reportingMonth"], "2026-08-01")
        self.assertEqual(chart["rangeStart"], "2026-03-01")
        self.assertEqual(chart["rangeEnd"], "2026-08-31")
        self.assertEqual(chart["cutoff"], "2026-08-31")
        self.assertEqual(chart["capturedAt"], CAPTURED)
        self.assertEqual([point["month"] for point in chart["points"]], [f"2026-{month:02}-01" for month in range(3, 9)])
        self.assertTrue(all(not point["partial"] for point in chart["points"]))

    def test_default_relative_uses_one_scale_for_all_months_and_both_series(self):
        chart = history_payload(frozen())
        self.assertIsNone(chart["currency"])
        self.assertFalse(chart["shareAmounts"])
        self.assertEqual(chart["points"][-1]["revenue"], 100)
        self.assertEqual(chart["points"][-1]["costs"], 50)
        self.assertAlmostEqual(chart["points"][0]["revenue"], 100 / 6)
        self.assertNotIn("600", str(chart))
        self.assertEqual(history_payload(frozen(share=True))["points"][-1]["revenue"], 600)
        self.assertEqual(history_payload(frozen(share=True))["currency"], "AUD")

    def test_zero_signed_values_and_missing_months_are_not_replaced_or_added(self):
        payload = evidence_payload()
        replace_totals(payload, 0, -1200, 50)
        replace_totals(payload, 1, 200, 0)
        payload["charts"]["performance"].pop(2)
        chart = history_payload(frozen(payload))
        self.assertEqual(chart["points"][0]["revenue"], -100)
        self.assertEqual(chart["points"][1]["costs"], 0)
        self.assertIsNone(chart["points"][2]["revenue"])
        self.assertIsNone(chart["points"][2]["costs"])
        self.assertIsNone(chart["points"][2]["periodEnd"])
        self.assertFalse(chart["points"][2]["partial"])
        for index, row in enumerate(payload["charts"]["performance"]):
            replace_totals(payload, index, 0, 0)
        self.assertEqual(history_payload(frozen(payload))["points"][-1]["revenue"], 0)

    def test_partial_final_month_and_exclusive_cutoff_keep_exact_coverage(self):
        payload = evidence_payload()
        payload["period"]["cutoff"] = "2026-08-18T13:00:00+10:00"
        for evidence in payload["charts"]["performance"][-1]["metric_evidence"].values():
            evidence["report_end_date"] = "2026-08-18"
        chart = history_payload(frozen(payload))
        self.assertEqual(chart["rangeEnd"], "2026-08-31")
        self.assertEqual(chart["cutoff"], "2026-08-18")
        self.assertTrue(chart["points"][-1]["partial"])
        self.assertEqual(chart["points"][-1]["periodEnd"], "2026-08-18")
        payload["period"]["cutoff"] = "2026-09-01T00:00:00+10:00"
        self.assertEqual(history_payload(frozen(payload))["cutoff"], "2026-08-18")
        payload["period"]["cutoff"] = "2026-08-18T00:00:00+10:00"
        chart = history_payload(frozen(payload))
        self.assertIsNone(chart["points"][-1]["revenue"])
        self.assertEqual(chart["cutoff"], "2026-08-17")

    def test_incompatible_account_currency_basis_coverage_and_unverified_sources_are_gaps(self):
        for key, value in (("connection_id", 8), ("unit", "USD"), ("accounting_basis", "cash"),
                ("report_end_date", "2026-03-18"), ("report_hash", "tampered"), ("source_provider", "financial")):
            payload = evidence_payload()
            payload["charts"]["performance"][0]["metric_evidence"]["income"][key] = value
            chart = history_payload(frozen(payload))
            self.assertIsNone(chart["points"][0]["revenue"], (key, value))
        payload = evidence_payload()
        for row in payload["charts"]["performance"]:
            for evidence in row["metric_evidence"].values():
                evidence.pop("connection_id")
        with self.assertRaisesRegex(FinancialHistoryError, "Refresh"):
            frozen(payload)

    def test_current_private_metric_quality_cannot_reintroduce_stale_or_unverified_values(self):
        payload = evidence_payload()
        metadata = payload["charts"]["performance"][-1]["metric_evidence"]["income"]
        payload["metrics"] = [{"key": "revenue", "value": "99999", "quality": "stale",
            "source_provider": "xero", "unit": "AUD", "metadata": metadata}]
        chart = history_payload(frozen(payload, share=True))
        self.assertIsNone(chart["points"][-1]["revenue"])
        self.assertEqual(chart["points"][-1]["costs"], 300)
        payload["metrics"][0]["quality"] = "source_reported"
        # A valid hash and trusted-looking quality cannot endorse a wrong total.
        self.assertIsNone(history_payload(frozen(payload, share=True))["points"][-1]["revenue"])
        payload["metrics"][0]["value"] = "600"
        self.assertEqual(history_payload(frozen(payload, share=True))["points"][-1]["revenue"], 600)
        payload["metrics"][0]["metadata"] = {**metadata, "source_metric": "contractor_revenue"}
        self.assertIsNone(history_payload(frozen(payload, share=True))["points"][-1]["revenue"])

    def test_client_numbers_are_ignored_and_legacy_omission_stays_without_chart(self):
        memo = {"financial_history": frozen(share=True)}
        self.assertIsNone(history_for_revision(None, memo, None, None, captured_at=CAPTURED))
        self.assertNotIn("financial_history", memo)
        self.assertIsNone(history_payload(None))
        self.assertIsNone(history_selection(None))
        self.assertIsNone(shared_update({"financialHistory": history_payload(frozen(share=True))})["financialHistory"])

    def test_strict_explicit_opt_in_rejects_coercion_and_unknown_payloads(self):
        invalid = (None, {}, {"enabled": True}, {"enabled": "true", "shareAmounts": False},
            {"enabled": 1, "shareAmounts": False}, {"enabled": True, "shareAmounts": "false"},
            {"enabled": False, "shareAmounts": True}, {"enabled": True, "shareAmounts": False, "revenue": 10},
            {"enabled": True, "shareAmounts": False, "refresh": 1},
            {"enabled": False, "shareAmounts": False, "refresh": True})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(FinancialHistoryError):
                validate_selection(value)
        self.assertEqual(validate_selection({"enabled": True, "shareAmounts": False}),
            {"enabled": True, "shareAmounts": False, "refresh": False})

    def test_text_edits_amount_toggles_disable_and_reenable_keep_frozen_figures(self):
        original = frozen()
        current = SimpleNamespace(structured_memo={"financial_history": original})
        changed = evidence_payload()
        replace_totals(changed, -1, 9000, 300)
        snapshot = SimpleNamespace(payload=changed)
        draft = SimpleNamespace(month=MONTH, update_date=date(2026, 10, 25))
        inherited = history_for_revision(draft, {}, current, snapshot, captured_at=CAPTURED)
        self.assertEqual(inherited, original)
        memo = {"_financial_history_selection": {"enabled": True, "shareAmounts": True}}
        amounts = history_for_revision(draft, memo, current, snapshot, captured_at=CAPTURED)
        self.assertEqual(history_payload(amounts)["points"][-1]["revenue"], 600)
        self.assertEqual(amounts["snapshot"], original["snapshot"])
        disabled = history_for_revision(draft, {"_financial_history_selection": {"enabled": False, "shareAmounts": False}}, current, snapshot, captured_at=CAPTURED)
        self.assertIsNone(history_payload(disabled))
        self.assertEqual(history_selection(disabled), {"enabled": False, "shareAmounts": False})
        self.assertEqual(disabled["snapshot"], original["snapshot"])
        refreshed = history_for_revision(draft, {"_financial_history_selection": {"enabled": True, "shareAmounts": True, "refresh": True}}, current, snapshot, captured_at=CAPTURED)
        self.assertEqual(history_payload(refreshed)["points"][-1]["revenue"], 9000)
        self.assertEqual(history_payload(original)["points"][-1]["revenue"], 100)

    def test_shared_allowlist_never_exposes_internal_metadata_and_keeps_money_filter(self):
        history = frozen(share=True)
        history["snapshot"]["private"] = {"connection_id": 123, "report_payload": {"secret": True}}
        history["snapshot"]["points"][0]["report_hash"] = "private"
        shared = shared_update({"metrics": {"invoice": 999}, "summary": "Revenue was AUD 999. We shipped.",
            "evidenceSnapshot": {"private": True}}, financial_history=history)
        self.assertEqual(shared["financialHistory"]["points"][-1]["revenue"], 600)
        self.assertEqual(shared["summary"], "We shipped.")
        self.assertEqual(shared["metrics"], {})
        self.assertNotIn("private", str(shared))
        self.assertNotIn("report_hash", str(shared))
        self.assertNotIn("financialHistorySelection", shared)

    def test_malformed_frozen_contract_is_not_shared(self):
        changes = (("schemaVersion", 2), ("reportingMonth", "2026-08-02"), ("cutoff", "2026-09-01"),
            ("rangeStart", "2026-01-01"), ("currency", "XYZ"), ("capturedAt", "2026-01-01"), ("points", []))
        for key, value in changes:
            history = frozen()
            history["snapshot"][key] = value
            with self.subTest(key=key):
                self.assertIsNone(history_payload(history))
        for key, value in (("revenue", float("nan")), ("revenue", True), ("partial", "false"),
                ("month", "2026-08-01"), ("periodEnd", "2026-04-01")):
            history = frozen()
            history["snapshot"]["points"][0][key] = value
            with self.subTest(key=key):
                self.assertIsNone(history_payload(history))

    def test_public_chart_highlights_last_slot_and_does_not_connect_missing_values(self):
        payload = evidence_payload()
        payload["charts"]["performance"].pop(2)
        context = history_chart_context(history_payload(frozen(payload)))
        self.assertEqual(context["reportingMonth"], "August 2026")
        self.assertEqual([point["highlighted"] for point in context["points"]], [False] * 5 + [True])
        self.assertEqual(len(context["segments"]), 6)
        self.assertEqual(context["points"][2]["revenueLabel"], "Unavailable")

    def test_generator_cannot_opt_in_or_supply_numeric_snapshot_but_honors_pinned_founder_choice(self):
        malicious = {"summary": "We shipped.", "financial_history": frozen(share=True),
            "_financial_history_selection": {"enabled": True, "shareAmounts": True},
            "financialHistory": {"revenue": 999}, "financialHistorySelection": {"enabled": True, "shareAmounts": True}}
        self.assertEqual(generated_history_selection(malicious, {}), {"summary": "We shipped."})
        choice = {"enabled": True, "shareAmounts": False}
        safe = generated_history_selection(malicious, {"financial_history_selection": choice})
        self.assertEqual(safe, {"summary": "We shipped.", "_financial_history_selection": {**choice, "refresh": False}})
        self.assertIn("financial_history", malicious)

    def test_save_serializer_requires_strict_booleans_and_cannot_accept_numbers(self):
        from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer
        for selection, valid in (({"enabled": True, "shareAmounts": False}, True),
                ({"enabled": True, "shareAmounts": True}, True),
                ({"enabled": "false", "shareAmounts": False}, False),
                ({"enabled": True, "shareAmounts": True, "points": []}, False)):
            serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={"month": "August", "year": 2026,
                "financialHistorySelection": selection})
            self.assertEqual(serializer.is_valid(), valid, serializer.errors)

    def test_correct_report_hash_cannot_promote_wrong_total_or_wrong_source_metric(self):
        for field in ("income", "expenses"):
            payload = evidence_payload()
            payload["charts"]["performance"][0][field] = 99999
            self.assertIsNone(history_payload(frozen(payload))["points"][0]["revenue" if field == "income" else "costs"])
        payload = evidence_payload()
        payload["charts"]["performance"][0]["metric_evidence"]["expenses"]["source_metric"] = "contractor_expenses"
        self.assertIsNone(history_payload(frozen(payload))["points"][0]["costs"])

    def test_shared_markdown_exposes_only_opted_in_history_money_and_preserves_old_posts(self):
        from startup_updates.services import render_monthly_update_markdown
        memo = {"title": "August update", "topline": "We shipped. Revenue was AUD 999.",
            "highlights": ["The pilot launched."], "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "unit": "AUD", "value": "AUD 888"}],
            "financial_performance": [{"label": "Monthly revenue", "value": "AUD 999"}],
            "progress_charts": [{"series": [{"label": "Private chart", "source": "Xero", "unit": "AUD", "timezone": "UTC", "definition": "Private totals", "points": [{"date": "2026-08-01", "value": 777, "partial": False}]}], "spec": {}}],
            "_audience_visibility": ["community"], "financial_history": frozen()}
        relative = render_monthly_update_markdown(memo)
        self.assertIn("Relative scale", relative)
        self.assertIn("This update", relative)
        self.assertIn("We shipped.", relative)
        self.assertNotIn("AUD", relative)
        self.assertNotIn("888", relative)
        self.assertNotIn("999", relative)
        self.assertNotIn("777", relative)
        memo["financial_history"]["shareAmounts"] = True
        amounts = render_monthly_update_markdown(memo)
        self.assertIn("Revenue 600.00", amounts)
        self.assertNotIn("888", amounts)
        self.assertNotIn("999", amounts)
        self.assertNotIn("777", amounts)
        memo["_audience_visibility"] = ["just_me"]
        self.assertIn("AUD 999", render_monthly_update_markdown(memo))
        memo["_audience_visibility"] = ["community"]
        memo.pop("financial_history")
        self.assertIn("AUD 999", render_monthly_update_markdown(memo))


if __name__ == "__main__":
    unittest.main()
