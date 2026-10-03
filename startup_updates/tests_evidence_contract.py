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
from startup_updates.disclosure import financial_chart, shared_narrative, shared_update


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
        with self.assertRaises(ValueError):
            validate_generated_metric_claims({"summary": "Revenue was {{metric:revenue}}."})

    def test_custom_financial_tokens_and_line_items_rejected(self):
        for text in ('Invoice: {{metric:sydney_invoice_total}}.', 'Our program budget is 14384.', 'Monthly costs: {{metric:monthlyCosts}}.'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_generated_metric_claims({'highlights': [text]})
        with self.assertRaises(ValueError):
            validate_generated_metric_claims({'summary': '{{metric:studio_target}}.'}, [{'key': 'studio_target', 'unit': 'AUD'}])
        validate_generated_metric_claims({'summary': 'We welcomed {{metric:activeUsers}} active users and {{metric:pilotCount}} pilots.'})
        validate_generated_metric_claims({'summary': 'Revenue improved.', 'kpi_snapshot': [{'value': 'AUD 100'}]})

    def test_founder_assertion_is_available_and_needs_confirmation(self):
        health = health_assessment({"metrics": [{"key": "revenue", "display_value": "100", "value": None, "quality": "founder_asserted"}]})
        self.assertNotIn("not enough", health["summary"])
        self.assertEqual(health["attention"][0]["metric_key"], "revenue")


class FinancialDisclosureTests(unittest.TestCase):
    def update(self, revenue='AUD 4,000', costs='AUD 2,000'):
        return {'metrics': {'revenue': revenue, 'monthlyCosts': costs, 'venue_invoice': 'AUD 960'},
            'metricEvidence': {
                'revenue': {'quality': 'source_reported', 'unit': 'AUD', 'source_provider': 'xero', 'basis': 'xero_profit_and_loss_revenue'},
                'monthlyCosts': {'quality': 'source_reported', 'unit': 'AUD', 'source_provider': 'xero', 'basis': 'xero_profit_and_loss_monthly_costs'},
            }, 'displayConfig': {'fullMetricKeys': ['revenue', 'monthlyCosts', 'venue_invoice']}, 'evidenceStatus': 'snapshot'}

    def test_shared_chart_contains_only_normalized_aggregate_values(self):
        value = self.update()
        value.update(evidenceSnapshot={'private': 960}, financialSnapshot={'private': 960},
            conciseAnalysis={'private': 'AUD 960'}, manualDocuments=[{'private': 960}],
            sourceUrl='https://private.invalid', sourceNotes=['AUD 960'], agentSources=['private'])
        result = shared_update(value)
        self.assertEqual(result['financialChart'], {'revenue': 1.0, 'costs': 0.5})
        self.assertEqual(result['metrics'], {})
        self.assertEqual(result['metricEvidence'], {})
        self.assertEqual(result['displayConfig'], {'snippetMetricKeys': [], 'fullMetricKeys': []})
        self.assertEqual(value['metrics']['venue_invoice'], 'AUD 960')
        for key in ('evidenceSnapshot', 'financialSnapshot', 'conciseAnalysis', 'manualDocuments', 'sourceUrl', 'sourceNotes', 'agentSources'):
            self.assertNotIn(key, result)
        value['displayConfig']['fullMetricKeys'] = ['venue_invoice']
        self.assertIsNone(shared_update(value)['financialChart'])

    def test_zero_is_a_chart_value_and_missing_values_are_unknown(self):
        self.assertEqual(financial_chart(self.update('0', '0')), {'revenue': 0.0, 'costs': 0.0})
        self.assertEqual(financial_chart(self.update('0', 'AUD 1')), {'revenue': 0.0, 'costs': 1.0})
        for revenue in ('', 'NaN', 'Infinity', '-100', '100K', '100 per invoice', '12,34', 'EUR 100', '£100'):
            with self.subTest(revenue=revenue):
                self.assertIsNone(financial_chart(self.update(revenue)))

    def test_only_canonical_totals_are_used_and_conflicting_aliases_rejected(self):
        value = self.update()
        value['metrics']['monthly_costs'] = 'AUD 3,000'
        value['metricEvidence']['monthly_costs'] = value['metricEvidence']['monthlyCosts'].copy()
        self.assertIsNone(financial_chart(value))
        value['metrics']['monthly_costs'] = 'AUD 2,000'
        self.assertEqual(financial_chart(value), {'revenue': 1.0, 'costs': 0.5})
        value['metrics'] = {'event_revenue_subtotal': 'AUD 4,000', 'monthlyCosts': 'AUD 2,000'}
        self.assertIsNone(financial_chart(value))

    def test_duplicate_frozen_kpis_cannot_hide_conflicting_values(self):
        value = self.update()
        items = [{'metric_key': key, 'value': amount, **value['metricEvidence'].get(key, {})}
            for key, amount in value['metrics'].items()]
        items.append({**items[0], 'value': 'AUD 1,000'})
        self.assertIsNone(financial_chart(value, metric_items=items))

    def test_unknown_partial_legacy_mismatched_currency_provider_and_basis_rejected(self):
        for detail in ({'quality': 'unknown'}, {'quality': 'partial'}, {'quality': 'disputed'},
            {'unit': 'USD'}, {'unit': 'XYZ'}, {'source_provider': 'stripe'}, {'basis': 'invoice_cash'}):
            with self.subTest(detail=detail):
                value = self.update()
                value['metricEvidence']['revenue'].update(detail)
                self.assertIsNone(financial_chart(value))
        value = self.update()
        value['validation'] = {'legacy_unverified': True}
        self.assertIsNone(financial_chart(value))

    def test_mixed_story_sentences_and_bullets_keep_nonfinancial_story(self):
        value = self.update()
        value.update(summary='We launched. Revenue was AUD 4,000. We welcomed 100 members.',
            highlights='- Delivered the event.\n- Venue invoice: $960.\n- Volunteers helped.',
            challenges='The 14384 program budget is confirmed. delivery needs more help.',
            asks='Introduce project managers. Revenue was {{metric:revenue}}. Connect us with mentors.')
        result = shared_update(value)
        self.assertEqual(result['summary'], 'We launched. We welcomed 100 members.')
        self.assertEqual(result['highlights'], '- Delivered the event.\n- Volunteers helped.')
        self.assertEqual(result['challenges'], 'delivery needs more help.')
        self.assertEqual(result['asks'], 'Introduce project managers. Connect us with mentors.')

    def test_plural_cash_valuation_and_formatted_amounts_cannot_leak(self):
        examples = (
            'We received 20000 in payments.', 'Invoices paid this month totalled 960.',
            'Our cash balance reached 20000.', 'Our valuation is 1000000.',
            'Profits totalled 10k.', 'Budgets were 900.', 'Subtotals reached 900.',
            'Our income reached twenty thousand.', 'We closed ₹20000 in sponsorships.',
            'We closed INR 20000 in sponsorships.', 'We closed CNY 20000 in sponsorships.',
            'We closed **USD** **900** in sponsorships.', 'Our **payments** totalled **20k**.',
            'We received 20K dollars.', 'We received 20k INR.',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(shared_narrative(text), '')
                self.assertEqual(shared_narrative(f'We launched. {text} Volunteers helped.'), 'We launched. Volunteers helped.')
                with self.assertRaises(ValueError):
                    validate_generated_metric_claims({'highlights': [text]})
        self.assertEqual(shared_narrative('We welcomed 100 members. We helped 20 volunteers.'),
            'We welcomed 100 members. We helped 20 volunteers.')

if __name__ == '__main__':
    unittest.main()
