"""Financial repair input checks; no database, credentials, or network."""
from datetime import date
from decimal import Decimal
from unittest import TestCase

from scripts.refresh_startup_dashboard_financials import verified_totals


class DashboardFinancialRefreshTests(TestCase):
    def test_full_month_canonical_totals_preserve_zero_and_decimals(self):
        report = {"Reports": [{"ReportTitles": ["1 September 2026 to 30 September 2026"]}]}
        totals = verified_totals(report, date(2026, 9, 1), {
            "revenue": {"amount": Decimal("0")},
            "monthly_costs": {"amount": Decimal("123.45")}})
        self.assertEqual(totals, {"revenue": Decimal("0"), "monthlyCosts": Decimal("123.45")})

    def test_wrong_or_partial_period_cannot_be_approved(self):
        for titles in ([], ["1 September 2026 to 18 September 2026"], ["1 August 2026 to 31 August 2026"]):
            with self.subTest(titles=titles), self.assertRaises(ValueError):
                verified_totals({"Reports": [{"ReportTitles": titles}]}, date(2026, 9, 1), {})

    def test_missing_or_nonfinite_totals_fail_closed(self):
        report = {"Reports": [{"ReportTitles": ["1 September 2026 to 30 September 2026"]}]}
        for value in (None, Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verified_totals(report, date(2026, 9, 1), {
                    "revenue": None if value is None else {"amount": value},
                    "monthly_costs": {"amount": Decimal("10")}})
