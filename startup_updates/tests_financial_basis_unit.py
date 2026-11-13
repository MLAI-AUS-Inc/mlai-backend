"""Cash/accrual basis and Stripe merge contracts: no database or provider calls."""
import calendar
import copy
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from startup_updates.evidence_contract import content_hash
from startup_updates.financial_history import (
    build_snapshot, history_chart_context, history_labels, history_payload, normalize_display, recompute_report_values,
)
from startup_updates.financial_refresh import request_scope
from startup_updates.financial_refresh_sources import (
    apply_stage, build_candidate_evidence, fetch_stage, month_slots, next_stage,
)
from startup_updates.services import _parse_xero_bank_summary_report, _parse_xero_profit_and_loss_report
from startup_updates.tests_financial_history_unit import CAPTURED, MONTH, evidence_payload


def pnl(*rows):
    return {"Reports": [{"ReportID": "ProfitAndLoss", "Rows": [
        {"RowType": "Section", "Title": title, "Rows": [
            {"RowType": "SummaryRow", "Cells": [{"Value": label}, {"Value": str(amount)}]}]}
        if title else {"RowType": "Section", "Rows": [{"RowType": "Row", "Cells": [{"Value": label}, {"Value": str(amount)}]}]}
        for title, label, amount in rows]}]}


def bank_summary(*accounts, total=True):
    header = {"RowType": "Header", "Cells": [{"Value": "Bank Accounts"}, {"Value": "Opening Balance"},
        {"Value": "Cash Received"}, {"Value": "Cash Spent"}, {"Value": "Closing Balance"}]}
    rows = [{"RowType": "Row", "Cells": [{"Value": name}, *({"Value": str(value)} for value in values)]}
        for name, *values in accounts]
    if total:
        sums = [sum(Decimal(str(account[index])) for account in accounts) for index in range(1, 5)]
        rows.append({"RowType": "SummaryRow", "Cells": [{"Value": "Total"}, *({"Value": str(value)} for value in sums)]})
    return {"Reports": [{"ReportID": "BankSummary", "Rows": [header, {"RowType": "Section", "Rows": rows}]}]}


def cash_rows(payload, values):
    """Add verified Bank Summary rows (cash in, cash out) for each chart month."""
    charts = payload["charts"]
    charts["cash"] = []
    for row, (cash_in, cash_out) in zip(charts["performance"], values):
        report = bank_summary(("Everyday", 1000, cash_in, cash_out, 1000 + cash_in - cash_out))
        digest = content_hash(report)
        charts["source_reports"][digest] = report
        evidence = {**row["metric_evidence"]["income"], "accounting_basis": "cash", "report_hash": digest}
        charts["cash"].append({"month": row["month"], "cash_in": str(cash_in), "cash_out": str(cash_out),
            "metric_evidence": {"cash_in": {**evidence, "source_metric": "xero_bank_summary_cash_in"},
                "cash_out": {**evidence, "source_metric": "xero_bank_summary_cash_out"}}})
    return payload


def stripe_totals(payload, months):
    payload["charts"]["stripe"] = {"unit": "AUD", "coverage_end": "2026-08-31", "connection_id": 9,
        "months": {month: {key: str(value) for key, value in values.items()} for month, values in months.items()}}
    return payload


def chart(payload, *, basis="cash", stripe_in_xero=True, share=True):
    return history_payload({"enabled": True, "shareAmounts": share,
        "snapshot": build_snapshot(payload, MONTH, captured_at=CAPTURED),
        "display": {"basis": basis, "stripeInXero": stripe_in_xero}})


class ProfitAndLossTotalsTests(unittest.TestCase):
    def test_other_income_is_revenue_and_costs_reconcile_to_net_profit(self):
        parsed = _parse_xero_profit_and_loss_report(pnl(
            ("Income", "Total Income", "28000"), ("Less Cost of Sales", "Total Cost of Sales", "8000"),
            ("Plus Other Income", "Total Other Income", "5000"),
            ("Less Operating Expenses", "Total Operating Expenses", "40000"),
            ("Less Other Expenses", "Total Other Expenses", "800"), (None, "Net Profit", "-15800")))
        self.assertEqual(parsed["revenue"]["amount"], Decimal("33000"))
        self.assertEqual(parsed["revenue"]["component_labels"], ["Total Income", "Total Other Income"])
        # An unfamiliar section (Other Expenses) is still a cost: revenue - costs = net.
        self.assertEqual(parsed["monthly_costs"]["amount"], Decimal("48800"))
        self.assertTrue(parsed["monthly_costs"]["reconciled_to_net_profit"])

    def test_trading_income_label_and_profit_without_other_income(self):
        parsed = _parse_xero_profit_and_loss_report(pnl(
            ("Trading Income", "Total Trading Income", "100"), ("Operating Expenses", "Total Operating Expenses", "60"),
            (None, "Net Profit", "40")))
        self.assertEqual(parsed["revenue"]["amount"], Decimal("100"))
        self.assertEqual(parsed["monthly_costs"]["amount"], Decimal("60"))

    def test_pre_revenue_month_has_zero_revenue_not_unknown(self):
        parsed = _parse_xero_profit_and_loss_report(pnl(
            ("Less Operating Expenses", "Total Operating Expenses", "900"), (None, "Net Profit", "-900")))
        self.assertEqual(parsed["revenue"]["amount"], Decimal("0"))
        self.assertEqual(parsed["monthly_costs"]["amount"], Decimal("900"))

    def test_report_without_net_profit_keeps_explicit_costs(self):
        parsed = _parse_xero_profit_and_loss_report(pnl(
            ("Income", "Total Income", "100"), ("Less Cost of Sales", "Total Cost of Sales", "20"),
            ("Less Operating Expenses", "Total Operating Expenses", "50")))
        self.assertEqual(parsed["monthly_costs"]["amount"], Decimal("70"))


class BankSummaryTests(unittest.TestCase):
    def test_totals_across_accounts(self):
        parsed = _parse_xero_bank_summary_report(bank_summary(
            ("Everyday", "10000.00", "58000.00", "70000.00", "-2000.00"),
            ("Savings", "50000.00", "349.73", "1626.69", "48723.04")))
        self.assertEqual(parsed["cash_in"], Decimal("58349.73"))
        self.assertEqual(parsed["cash_out"], Decimal("71626.69"))
        self.assertEqual(parsed["accounts"], ["Everyday", "Savings"])

    def test_unreconciled_report_is_a_gap(self):
        parsed = _parse_xero_bank_summary_report(bank_summary(("Everyday", "10", "5", "1", "999")))
        self.assertIsNone(parsed["cash_in"])
        self.assertIsNone(parsed["cash_out"])

    def test_missing_total_row_sums_accounts_and_bad_payload_is_empty(self):
        parsed = _parse_xero_bank_summary_report(bank_summary(("A", 0, 10, 4, 6), ("B", 1, 1, 0, 2), total=False))
        self.assertEqual((parsed["cash_in"], parsed["cash_out"]), (Decimal("11"), Decimal("4")))
        self.assertIsNone(_parse_xero_bank_summary_report({"Reports": []})["cash_in"])
        self.assertIsNone(_parse_xero_bank_summary_report("not a report")["cash_in"])


class BasisDisplayTests(unittest.TestCase):
    def setUp(self):
        self.payload = cash_rows(evidence_payload(), [(index * 10 + 7, index * 5 + 3) for index in range(1, 7)])

    def test_cash_is_the_default_and_accrual_is_a_setting(self):
        self.assertEqual(normalize_display(None), {"basis": "cash", "stripeInXero": True})
        cash = chart(self.payload)
        self.assertEqual(cash["basis"], "cash")
        self.assertIsNone(cash["basisFallback"])
        self.assertEqual((cash["points"][-1]["revenue"], cash["points"][-1]["costs"]), (67, 33))
        accrual = chart(self.payload, basis="accrual")
        self.assertEqual((accrual["points"][-1]["revenue"], accrual["points"][-1]["costs"]), (600, 300))
        self.assertEqual(history_labels(cash)["in"], "Cash in")
        self.assertEqual(history_labels(accrual)["out"], "Costs")

    def test_one_saved_snapshot_serves_both_bases(self):
        snapshot = build_snapshot(self.payload, MONTH, captured_at=CAPTURED)
        history = {"enabled": True, "shareAmounts": True, "snapshot": snapshot}
        before = copy.deepcopy(snapshot)
        self.assertEqual(history_payload({**history, "display": {"basis": "cash"}})["points"][0]["revenue"], 17)
        self.assertEqual(history_payload({**history, "display": {"basis": "accrual"}})["points"][0]["revenue"], 100)
        self.assertEqual(snapshot, before)

    def test_cash_falls_back_to_accrual_with_a_reason(self):
        legacy = history_payload({"enabled": True, "shareAmounts": True,
            "snapshot": {**build_snapshot(evidence_payload(), MONTH, captured_at=CAPTURED), "schemaVersion": 1}})
        self.assertEqual((legacy["basis"], legacy["basisFallback"]), ("accrual", "legacy"))
        payload = evidence_payload()
        payload["charts"]["cash_unavailable"] = "scope"
        scoped = chart(payload)
        self.assertEqual((scoped["basis"], scoped["basisFallback"]), ("accrual", "scope"))

    def test_tampered_cash_total_is_a_gap(self):
        self.payload["charts"]["cash"][0]["cash_in"] = "99999"
        self.assertIsNone(chart(self.payload)["points"][0]["revenue"])
        self.assertEqual(chart(self.payload)["points"][0]["costs"], 8)

    def test_stripe_within_xero_is_shown_not_added(self):
        payload = stripe_totals(self.payload, {"2026-08-01": {"sales": 50, "refunds": 5, "fees": 2, "payouts": 40}})
        cash = chart(payload)
        self.assertEqual(cash["stripe"], "withinXero")
        self.assertEqual((cash["points"][-1]["revenue"], cash["points"][-1]["stripe"]), (67, 40))
        accrual = chart(payload, basis="accrual")
        self.assertEqual((accrual["points"][-1]["revenue"], accrual["points"][-1]["stripe"]), (600, 45))

    def test_stripe_outside_xero_is_added_once(self):
        payload = stripe_totals(self.payload, {"2026-08-01": {"sales": 50, "refunds": 5, "fees": 2, "payouts": 40}})
        cash = chart(payload, stripe_in_xero=False)
        self.assertEqual((cash["stripe"], cash["points"][-1]["revenue"], cash["points"][-1]["costs"]), ("added", 107, 33))
        accrual = chart(payload, basis="accrual", stripe_in_xero=False)
        self.assertEqual((accrual["points"][-1]["revenue"], accrual["points"][-1]["costs"]), (645, 302))
        self.assertIsNone(accrual["points"][0]["stripe"])

    def test_stripe_only_startup_never_reports_costs(self):
        payload = evidence_payload()
        payload["charts"]["performance"] = []
        stripe_totals(payload, {"2026-07-01": {"sales": 30, "refunds": 0, "fees": 1, "payouts": 20},
            "2026-08-01": {"sales": 50, "refunds": 5, "fees": 2, "payouts": 40}})
        accrual = chart(payload, basis="accrual")
        self.assertEqual(accrual["stripe"], "only")
        self.assertEqual((accrual["points"][-1]["revenue"], accrual["points"][-1]["costs"]), (45, None))
        cash = chart(payload)
        self.assertIsNone(cash["basisFallback"])
        self.assertEqual(cash["points"][-1]["revenue"], 40)
        self.assertIn("Stripe", history_labels(cash)["source"])

    def test_relative_scale_includes_stripe_and_hides_amounts(self):
        payload = stripe_totals(self.payload, {"2026-08-01": {"sales": 900, "refunds": 0, "fees": 2, "payouts": 900}})
        relative = chart(payload, share=False)
        values = [abs(point[key]) for point in relative["points"] for key in ("revenue", "costs", "stripe") if point[key] is not None]
        self.assertLessEqual(max(values), 100)
        self.assertNotIn("900", str(relative))

    def test_public_chart_uses_basis_labels(self):
        context = history_chart_context(chart(self.payload))
        self.assertEqual((context["inLabel"], context["outLabel"], context["title"]), ("Cash in", "Cash out", "Cash in and out"))

    def test_saved_totals_are_recomputed_from_their_own_reports(self):
        payload = evidence_payload()
        row = payload["charts"]["performance"][-1]
        report = pnl(("Income", "Total Income", "600"), ("Plus Other Income", "Total Other Income", "50"),
            ("Less Operating Expenses", "Total Operating Expenses", "300"), (None, "Net Profit", "350"))
        digest = content_hash(report)
        payload["charts"]["source_reports"][digest] = report
        for evidence in row["metric_evidence"].values():
            evidence["report_hash"] = digest
        # The saved value predates Other Income, so it no longer verifies...
        self.assertIsNone(chart(payload, basis="accrual")["points"][-1]["revenue"])
        upgraded = recompute_report_values(payload)
        # ...and is recomputed from the unchanged report without a provider call.
        self.assertEqual(chart(upgraded, basis="accrual")["points"][-1]["revenue"], 650)
        self.assertEqual(payload["charts"]["performance"][-1]["income"], 600)


class RefreshCollectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
        profile = SimpleNamespace(default_currency="AUD", reporting_timezone="Australia/Melbourne", reporting_config_version=1)
        data = {"requestId": str(uuid4()), "creationKey": str(uuid4()), "targetMonth": "2026-08-01", "inputSources": ["stripe", "xero"]}
        self.request = {**request_scope(SimpleNamespace(pk=uuid4(), organization_id=4), SimpleNamespace(pk=7), data, profile, now=self.now),
            "connections": {"xero": {"id": 3}, "stripe": {"id": 9}}}
        self.months = [month.isoformat() for month in month_slots(date(2026, 8, 1))]

    def test_cash_reports_follow_profit_and_loss_and_scope_gaps_keep_accrual(self):
        progress = {"months": self.months, "sources": {"xero": {"status": "refreshing"}},
            "xero": {"currency": "AUD", "reports": {month: {} for month in self.months}}}
        self.assertEqual(next_stage(progress, ["xero"]), ("xero", "cash:2026-03-01"))
        narrow = SimpleNamespace(access_token="synthetic", scopes=["accounting.reports.profitandloss.read"])
        with patch("startup_updates.financial_refresh_sources.fetch_xero_accounting_report") as fetch:
            receipt = fetch_stage("xero", "cash:2026-03-01", narrow, self.request, progress)
        fetch.assert_not_called()
        updated = apply_stage(progress, "xero", "cash:2026-03-01", receipt, now=self.now)
        self.assertEqual(updated["sources"]["xero"]["status"], "ready")
        self.assertIn("Reconnect Xero to show cash", updated["sources"]["xero"]["detail"])

    def test_bank_summary_uses_the_month_window(self):
        connection = SimpleNamespace(access_token="synthetic", scopes=["accounting.reports.banksummary.read"])
        with patch("startup_updates.financial_refresh_sources.fetch_xero_accounting_report",
                return_value=bank_summary(("A", 0, 10, 4, 6))) as fetch:
            receipt = fetch_stage("xero", "cash:2026-08-01", connection, self.request, {"months": self.months})
        self.assertEqual(fetch.call_args.args[1], "BankSummary")
        self.assertEqual(fetch.call_args.kwargs["params"], {"fromDate": "2026-08-01", "toDate": "2026-08-31"})
        self.assertEqual(receipt["end"], "2026-08-31")

    def test_stripe_balance_page_keeps_only_monthly_totals(self):
        august = int(datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp())
        rows = [
            {"id": "txn_1", "created": august, "currency": "aud", "reporting_category": "charge", "amount": 5000, "fee": 175,
                "description": "Customer secret", "source": "ch_private"},
            {"id": "txn_2", "created": august, "currency": "aud", "reporting_category": "refund", "amount": -500, "fee": 0},
            {"id": "txn_3", "created": august, "currency": "aud", "reporting_category": "payout", "amount": -4000, "fee": 0},
            {"id": "txn_4", "created": august, "currency": "usd", "reporting_category": "charge", "amount": 999, "fee": 0},
        ]
        response = Mock()
        response.json.return_value = {"data": rows, "has_more": False}
        with patch("startup_updates.financial_refresh_sources.requests.get", return_value=response) as get:
            receipt = fetch_stage("stripe", "balance", SimpleNamespace(access_token="synthetic"), self.request, {"months": self.months})
        self.assertTrue(get.call_args.args[0].endswith("/v1/balance_transactions"))
        self.assertEqual(receipt["months"], {"2026-08-01": {"sales": "50", "refunds": "5", "fees": "1.75", "payouts": "40"}})
        self.assertEqual(receipt["otherCurrency"], 1)
        self.assertNotIn("secret", str(receipt))
        self.assertNotIn("ch_private", str(receipt))

    def test_candidate_carries_cash_and_stripe_into_one_snapshot(self):
        progress = {"months": self.months, "sources": {"xero": {"status": "ready"}, "stripe": {"status": "ready"}},
            "xero": {"currency": "AUD", "reports": {}, "cash": {}},
            "stripe": {"months": {"2026-08-01": {"sales": "50", "refunds": "5", "fees": "1.75", "payouts": "40"}}, "pageHashes": []}}
        for month in self.months:
            start = date.fromisoformat(month)
            end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]).isoformat()
            progress["xero"]["reports"][month] = {"end": end, "report": pnl(("Income", "Total Income", "100"),
                ("Plus Other Income", "Total Other Income", "10"), ("Less Operating Expenses", "Total Operating Expenses", "70"),
                (None, "Net Profit", "40"))}
            progress["xero"]["cash"][month] = {"end": end, "report": bank_summary(("A", 0, 90, 50, 40))}
        payload = build_candidate_evidence(self.request, progress)
        snapshot = build_snapshot(payload, date(2026, 8, 1), captured_at=self.now.isoformat())
        history = {"enabled": True, "shareAmounts": True, "snapshot": snapshot}
        cash = history_payload({**history, "display": {"basis": "cash"}})
        self.assertEqual((cash["points"][-1]["revenue"], cash["points"][-1]["costs"], cash["points"][-1]["stripe"]), (90, 50, 40))
        accrual = history_payload({**history, "display": {"basis": "accrual"}})
        self.assertEqual((accrual["points"][-1]["revenue"], accrual["points"][-1]["costs"]), (110, 70))
        self.assertNotIn("months", payload["financial_refresh"]["stripe"])


class LegacyChartUpgradeTests(unittest.TestCase):
    """Charts saved before cash basis read as today's chart without being rewritten."""

    def legacy_history(self, payload):
        snapshot = {**build_snapshot(payload, MONTH, captured_at=CAPTURED), "schemaVersion": 1}
        for key in ("cash", "cashUnavailable", "stripe"):
            snapshot.pop(key)
        return {"enabled": True, "shareAmounts": True, "snapshot": snapshot,
            "_source": {"snapshotId": 41, "snapshotHash": "source-hash"}}

    def read(self, history, source_payload, supplement=None, display=None):
        from startup_updates import financial_history
        financial_history._LEGACY_CACHE.clear()
        source = SimpleNamespace(payload=source_payload)
        with patch("startup_updates.financial_history._cash_supplement", return_value=supplement), \
                patch("startup_updates.models.MonthlyEvidenceSnapshot.objects") as objects:
            objects.filter.return_value.first.return_value = source
            return financial_history.reader_history(history, organization_id=4, month=MONTH,
                display=display or {"basis": "cash"})

    def test_backfilled_cash_supplement_switches_an_old_chart_to_cash(self):
        payload = evidence_payload()
        history = self.legacy_history(payload)
        supplement_payload = cash_rows(copy.deepcopy(payload), [(index * 10, index) for index in range(1, 7)])
        supplement = SimpleNamespace(content_hash="supplement-hash", payload={"charts": {
            "cash": supplement_payload["charts"]["cash"], "source_reports": supplement_payload["charts"]["source_reports"]}})
        before = copy.deepcopy(history)
        upgraded = history_payload(self.read(history, payload, supplement))
        self.assertEqual((upgraded["basis"], upgraded["points"][-1]["revenue"]), ("cash", 60))
        self.assertEqual(history, before)

    def test_without_supplement_old_chart_shows_accrual_with_reason(self):
        payload = evidence_payload()
        shown = history_payload(self.read(self.legacy_history(payload), payload))
        self.assertEqual((shown["basis"], shown["basisFallback"]), ("accrual", "unavailable"))
        self.assertEqual(shown["points"][-1]["revenue"], 600)

    def test_unverifiable_source_keeps_the_saved_chart(self):
        history = self.legacy_history(evidence_payload())
        shown = history_payload(self.read(history, {"charts": {}}))
        self.assertEqual((shown["basis"], shown["basisFallback"]), ("accrual", "legacy"))
        self.assertEqual(shown["points"][-1]["revenue"], 600)

    def test_display_is_never_persisted_on_resave(self):
        from startup_updates.financial_history import history_for_revision
        history = {**self.legacy_history(evidence_payload()), "display": {"basis": "accrual"}}
        current = SimpleNamespace(structured_memo={"financial_history": history})
        kept = history_for_revision(SimpleNamespace(month=MONTH), {}, current, None, captured_at=CAPTURED)
        self.assertNotIn("display", kept)


if __name__ == "__main__":
    unittest.main()
