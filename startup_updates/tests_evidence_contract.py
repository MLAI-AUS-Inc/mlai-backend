"""Pure reporting-contract regression tests; no Django or database construction."""
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from startup_updates.evidence_contract import (
    content_hash, customer_receipt, reporting_period, financial_snapshot_from_metrics,
    render_metric_claims, stripe_paid_invoice_sales_minor, validate_generated_metric_claims,
    health_assessment,
)


class EvidenceContractTests(unittest.TestCase):
    def test_supplier_payment_is_not_customer_receipt(self):
        record = SimpleNamespace(direction="debit", status="AUTHORISED", category="bill_payment", raw_payload={"Invoice": {"Type": "ACCPAY"}})
        self.assertFalse(customer_receipt(record))
        record.direction = "credit"
        self.assertFalse(customer_receipt(record))

    def test_deleted_receipt_excluded(self):
        record = SimpleNamespace(direction="credit", status="DELETED", raw_payload={"Invoice": {"Type": "ACCREC"}})
        self.assertFalse(customer_receipt(record))
        record.status = "AUTHORISED"
        self.assertTrue(customer_receipt(record))

    def test_unpaid_invoice_not_revenue(self):
        self.assertIsNone(stripe_paid_invoice_sales_minor({"status": "open", "amount_due": 11000, "total": 11000}))

    def test_paid_invoice_excludes_tax(self):
        self.assertEqual(stripe_paid_invoice_sales_minor({"status": "paid", "amount_paid": 11000, "total": 11000, "total_excluding_tax": 10000}), Decimal(10000))

    def test_credit_tax_allocation_is_not_invented(self):
        self.assertIsNone(stripe_paid_invoice_sales_minor({"status": "paid", "amount_paid": 11000, "total": 11000, "total_excluding_tax": 10000, "post_payment_credit_notes_amount": 2000}))

    def test_missing_tax_or_payment_is_unknown(self):
        self.assertIsNone(stripe_paid_invoice_sales_minor({"status": "paid", "total": 11000}))
        self.assertIsNone(stripe_paid_invoice_sales_minor({"status": "paid", "amount_paid": 11000, "total": 11000}))

    def test_zero_stays_zero(self):
        self.assertEqual(stripe_paid_invoice_sales_minor({"status": "paid", "amount_paid": 0, "total": 0}), Decimal(0))

    def test_partial_payment_not_assumed_complete(self):
        self.assertIsNone(stripe_paid_invoice_sales_minor({"status": "paid", "amount_paid": 5000, "total": 11000, "total_excluding_tax": 10000}))

    def test_timezone_and_dst_month_boundaries(self):
        period = reporting_period(date(2026, 10, 1), "Australia/Melbourne", as_of=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertTrue(period["start"].endswith("+10:00"))
        self.assertTrue(period["end_exclusive"].endswith("+11:00"))
        self.assertFalse(period["is_partial"])

    def test_partial_month_and_future_rejected(self):
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        self.assertTrue(reporting_period(date(2026, 9, 1), as_of=now)["is_partial"])
        with self.assertRaises(ValueError):
            reporting_period(date(2026, 10, 1), as_of=now)

    def test_snapshot_is_a_deep_copy(self):
        metrics = [{"key": "revenue", "display_value": "AUD 100", "value": "100", "metadata": {"id": 1}}]
        snapshot = financial_snapshot_from_metrics({"month": "2026-09-01"}, metrics)
        metrics[0]["metadata"]["id"] = 2
        self.assertEqual(snapshot["metrics"][0]["metadata"]["id"], 1)
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_prose_tokens_use_card_value(self):
        metrics = [{"key": "revenue", "display_value": "AUD 1,250.00"}]
        memo = render_metric_claims({"highlights": ["Revenue was {{metric:revenue}}."]}, metrics)
        self.assertEqual(memo["highlights"], ["Revenue was AUD 1,250.00."])
        with self.assertRaises(ValueError):
            render_metric_claims({"summary": "{{metric:unknown}}"}, metrics)

    def test_unbound_financial_claim_rejected(self):
        with self.assertRaises(ValueError):
            validate_generated_metric_claims({"summary": "We made $90000 in revenue."})
        validate_generated_metric_claims({"summary": "Revenue was {{metric:revenue}}."})

    def test_founder_assertion_is_available_and_needs_confirmation(self):
        health = health_assessment({"metrics": [{"key": "revenue", "display_value": "100", "value": None, "quality": "founder_asserted"}]})
        self.assertNotIn("not enough", health["summary"])
        self.assertEqual(health["attention"][0]["metric_key"], "revenue")

if __name__ == '__main__':
    unittest.main()
