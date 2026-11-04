"""Reservation settlement is valid usage, while budget ceilings stay conservative."""
from django.test import SimpleTestCase
from django.utils import timezone
from types import SimpleNamespace
from unittest.mock import patch
from . import website_goal as goals, website_goal_budget as budget
from .tests_website_goal_unit import scope

BOUNDS = "reservation_upper_bounds_v1"


def report(*, state=1, **changes):
    return {"version": 1, "run_id": "meter-run", "generation": 1, "state_version": state,
        "counter_semantics": BOUNDS, "model_calls": 1, "tokens": 35_000,
        "provider_seconds": 600, "active_seconds": 600, "model_usd": 0.35,
        "builds": 1, "previews": 0, **changes}


class UsageSettlementTests(SimpleTestCase):
    def setUp(self):
        self.goal = goals.new_goal("goal", scope(), now=timezone.now())

    def accept(self, goal, value):
        return budget.account_usage(goal, value, run_id=value["run_id"])

    def settled(self, state=2):
        return report(state=state, tokens=17, provider_seconds=0.2, active_seconds=3, model_usd=0.00017)

    def test_settled_reservation_keeps_peak_budget_without_invalidating_a_real_run(self):
        reserved = self.accept(self.goal, report())
        settled = self.accept(reserved, self.settled())
        self.assertEqual(settled["usage_reports"]["meter-run"]["tokens"], 17)
        self.assertEqual(settled["budget_used"]["tokens"], 35_000)
        self.assertEqual(settled["budget_used"]["provider_seconds"], 600)
        self.assertEqual(settled["budget_used"]["active_seconds"], 600)
        self.assertEqual(settled["budget_used"]["model_cost"], 0.35)
        self.assertEqual(self.accept(settled, self.settled()), settled)

    def test_legacy_or_changed_semantics_do_not_authorize_decreasing_counters(self):
        first, second = report(), self.settled()
        first.pop("counter_semantics")
        second.pop("counter_semantics")
        with self.assertRaises(ValueError):
            self.accept(self.accept(self.goal, first), second)
        with self.assertRaises(ValueError):
            self.accept(self.accept(self.goal, first), self.settled())

    def test_admissions_and_same_snapshot_still_refuse_backwards_values(self):
        reserved = self.accept(self.goal, report())
        for value in (report(state=2, model_calls=0), report(state=2, builds=0), self.settled(state=1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.accept(reserved, value)

    def test_unknown_bounds_stay_unknown_and_cannot_erase_the_recorded_peak(self):
        reserved = self.accept(self.goal, report())
        unknown = self.accept(reserved, report(state=2, tokens=None, model_usd=None,
            provider_seconds=None, active_seconds=None))
        self.assertIsNone(unknown["budget_used"]["tokens"])
        self.assertIsNone(unknown["budget_used"]["model_cost"])
        known = self.accept(unknown, self.settled(state=3))
        self.assertEqual(known["budget_used"]["tokens"], 35_000)
        self.assertEqual(known["budget_used"]["model_cost"], 0.35)

    def test_a_null_gap_does_not_hide_an_admission_counter_reset(self):
        reserved = self.accept(self.goal, report())
        missing = self.accept(reserved, report(state=2, model_calls=None))
        with self.assertRaises(ValueError):
            self.accept(missing, report(state=3, model_calls=0))

    def test_cumulative_growth_consumes_the_peak_and_compacts_auxiliary_state(self):
        settled = self.accept(self.accept(self.goal, report()), self.settled())
        grown = self.accept(settled, report(state=3, tokens=40_000, model_usd=0.4,
            provider_seconds=700, active_seconds=710))
        self.assertEqual(grown["budget_used"]["tokens"], 40_000)
        self.assertEqual(grown["budget_used"]["model_cost"], 0.4)
        self.assertFalse(grown.get("usage_high_water"))

    def test_unrecognized_counter_semantics_fail_closed(self):
        for invalid in ("actual", [], True, 2):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.accept(self.goal, report(counter_semantics=invalid))

    def test_peaks_are_per_original_run_and_do_not_turn_unknown_totals_into_zero(self):
        settled = self.accept(self.accept(self.goal, report()), self.settled())
        second = report(run_id="other-run", tokens=100, model_usd=None)
        combined = self.accept(settled, second)
        self.assertEqual(combined["budget_used"]["tokens"], 35_100)
        self.assertIsNone(combined["budget_used"]["model_cost"])
        self.assertEqual(combined["usage_reports"]["meter-run"]["tokens"], 17)
        self.assertEqual(combined["usage_reports"]["other-run"]["tokens"], 100)

    def test_corrupt_stored_journals_are_refused_before_replay_or_aggregation(self):
        accepted = self.accept(self.goal, report())
        corruptions = ({"usage_reports": []}, {"usage_reports": {"foreign": {}}},
            {"usage_high_water": {"foreign": {"tokens": 1}}},
            {"usage_high_water": {"meter-run": {"tokens": True}}},
            {"usage_high_water": {"meter-run": {"tokens": float("nan")}}})
        for change in corruptions:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.accept({**accepted, **change}, report())

    def test_real_goal_runner_accepts_settlement_without_marking_usage_invalid(self):
        from . import website_goal_runner as runner
        operation = SimpleNamespace(connection_id="connection", generation=2,
            connection=SimpleNamespace(organization_id=4, repository_id=42), receipt={"setup_run_id": "meter-run"})
        child = SimpleNamespace(status="running", result={"usage": report()},
            run_request={"website_connection_id": "connection", "connection_generation": 2,
                "repository_id": 42, "deletion_epoch": 3})
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs, \
                patch("content_factory.website_operations.deletion_epoch", return_value=3):
            runs.return_value.first.return_value = child
            reserved = runner.account_child_usage(operation, self.goal)
            child.status, child.result = "completed", {"usage": self.settled()}
            settled = runner.account_child_usage(operation, reserved)
        self.assertNotIn("usage_invalid", settled)
        self.assertFalse(operation._goal_has_active_child)
        self.assertEqual(settled["budget_used"]["model_cost"], 0.35)
