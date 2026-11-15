"""Offline invariants. No database or provider calls."""
from copy import deepcopy
import unittest

from integrations.services.studio_payment_rules import quantity, review, ReviewError, claim_fingerprint


def work(hours=1, **extra):
    return {"key": "work-1", "issue": "STU-1", "builder": "builder", "project": "acquire",
            "start": "2026-10-12", "end": "2026-10-12", "units": quantity(hours),
            "basis": "actual", "pool": "customer", **extra}


def fund(hours=80, **extra):
    return {"key": "INV-0084:labour", "start": "2026-10-11", "end": "2026-10-25",
            "units": quantity(hours), "cents": 1000000, "currency": "AUD", "basis": "actual", "pool": "customer", **extra}


def claim(hours=1, **extra):
    units = quantity(hours)
    return {"builder": "builder", "number": "48352", "currency": "AUD", "cents": round(hours*6500),
            "lines": [{"issue": "STU-1", "project": "acquire", "start": "2026-10-11", "end": "2026-10-25",
                       "basis": "actual", "units": units, "cents": round(hours*6500)}], **extra}


def check(claims, works, funds=None, ledger=None, verified=True):
    return review(claims, works, funds or [fund()], ledger or {}, opening_verified=verified)


class PaymentRulesTests(unittest.TestCase):
    def test_twenty_minutes_are_exact_and_estimates_have_no_approved_entry(self):
        self.assertEqual(quantity("0.333333"), 1200)
        request = claim(1/3)
        self.assertEqual(check([request], [work(1/3)])["ready_cents"], 2167)
        self.assertEqual(check([request], [])["invoices"][0]["status"], "needs_review")

    def test_october_101_hours_need_21_more_even_with_sufficient_money(self):
        result = check([claim(101)], [work(101)])
        self.assertEqual(result["invoices"][0]["lines"][0]["uncovered_units"], 21*3600)
        self.assertEqual(result["ready_cents"], 0)
        self.assertEqual(result["allocations"], [])

    def test_money_and_units_must_both_fit(self):
        self.assertEqual(check([claim()], [work()], [fund(cents=6499)])["ready_cents"], 0)
        self.assertEqual(check([claim()], [work()], [fund(.5)])["ready_cents"], 0)

    def test_two_builders_draw_on_the_same_pool(self):
        first, second = claim(), claim(builder="second", number="other")
        rows = [work(), work(key="work-2", builder="second")]
        result = check([first, second], rows, [fund(1)])
        self.assertEqual([r["status"] for r in result["invoices"]], ["ready", "needs_review"])
        self.assertEqual(sum(a["units"] for a in result["allocations"]), 3600)

    def test_money_can_be_split_across_eligible_funding_lines(self):
        result = check([claim()], [work()], [fund(1,cents=3250), fund(1,key="other-line",cents=3250)])
        self.assertEqual(result["ready_cents"], 6500)
        self.assertEqual(sum(a["cents"] for a in result["allocations"]), 6500)

    def test_reservation_and_manual_paid_work_cannot_be_reused(self):
        one = check([claim()], [work()], [fund(1)])
        for status in ("reserved", "unknown", "paid"):
            ledger = {"existing": {"invoices": ["builder:48352"], "status": status, "allocations": one["allocations"]}}
            repeated = check([claim()], [work()], [fund(1)], ledger)
            self.assertEqual(repeated["ready_cents"], 0)
            renamed = check([claim(number="renamed")], [work()], [fund(1)], ledger)
            self.assertEqual(renamed["ready_cents"], 0)

    def test_a_revision_of_a_paid_invoice_is_never_silently_marked_paid(self):
        original = claim()
        checked = check([original], [work()])
        ledger = {"paid": {"invoices": ["builder:48352"], "status": "paid",
                           "claim_hash": claim_fingerprint(original), "allocations": checked["allocations"]}}
        self.assertEqual(check([original], [work()], ledger=ledger)["invoices"][0]["status"], "already_paid")
        revised = claim(2)
        result = check([revised], [work(2)], ledger=ledger)
        self.assertEqual(result["invoices"][0]["status"], "needs_review")
        self.assertEqual(result["ready_cents"], 0)

    def test_reversed_customer_invoice_keeps_old_consumption_visible(self):
        one = check([claim()], [work()])
        ledger = {"old": {"invoices": ["builder:48352"], "status": "paid", "allocations": one["allocations"]}}
        result = check([claim(number="new")], [work(key="different")], [fund(.5)], ledger)
        self.assertEqual(result["ready_cents"], 0)

    def test_opening_balance_and_unknown_period_are_reviewed(self):
        self.assertEqual(check([claim()], [work()], verified=False)["ready_cents"], 0)
        request = claim()
        request["lines"][0].pop("issue")
        self.assertEqual(check([request], [work(2)])["invoices"][0]["lines"][0]["code"], "period_allocation_needed")

    def test_basis_currency_person_and_project_do_not_cross(self):
        for changed in ({"basis": "size"}, {"builder": "another"}, {"project": "another"}):
            self.assertEqual(check([claim()], [work(**changed)])["ready_cents"], 0)
        self.assertEqual(check([claim(currency="USD")], [work()])["ready_cents"], 0)

    def test_preview_is_pure_and_renamed_duplicate_cannot_claim_again(self):
        dataset = [[claim(), claim(number="second")], [work()], [fund()], {}]
        before = deepcopy(dataset)
        result = check(*dataset)
        self.assertEqual(dataset, before)
        self.assertEqual(result["ready_cents"], 6500)

    def test_printed_quantity_precision_is_bounded(self):
        for value in ("NaN", "Infinity", "-1", "0", "0.3333"):
            with self.assertRaises(ReviewError):
                quantity(value)
