"""Goal policy invariants: rung fences, replay accounting and precise budget edges."""
from copy import deepcopy
from datetime import timedelta
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_goal as goals, website_strategy as strategy, website_goal_budget as budget
from . import tests_website_goal_unit as fixtures

@override_settings(ARTICLES_WEBSITE_STRATEGY_ENABLED=True)
class StrategyTests(SimpleTestCase):
    def setUp(self):
        self.now, self.source = timezone.now(), "a" * 40
        self.goal = goals.refresh_goal(goals.new_goal("goal", fixtures.scope(), now=self.now), fixtures.proof_set(self.now),
            scope=fixtures.scope(), source_sha=self.source, now=self.now)

    def test_inventory_selection_and_every_rung_are_monotonic(self):
        for journey, rung in (({"discovery": {"resolution": {"existence_state": "confirmed"}}}, "adopt"),
                              ({"discovery": {"resolution": {"content_source": {"path": "content"}}}}, "native_collection"),
                              ({"repository": {"framework": "astro"}}, "deterministic"), ({}, "model_adapter")):
            with self.subTest(rung=rung):
                self.assertEqual(strategy.ensure_pin(self.goal, journey, source=self.source, now=self.now)["strategy_pin"]["rung"], rung)
        goal = strategy.ensure_pin(self.goal, {"discovery": {"resolution": {"existence_state": "confirmed"}}}, source=self.source, now=self.now)
        for rung in strategy.RUNGS:
            self.assertEqual(goal["strategy_pin"]["rung"], rung)
            self.assertEqual(strategy.ensure_pin(goal, {}, source=self.source, now=self.now), goal)
            goal = strategy.advance(goal, source=self.source, now=self.now, reason="strategy_exhausted")
        self.assertEqual(len(goal["strategy_history"]), 6)

    def test_exhaustion_cannot_cross_goal_source_or_pin_revision(self):
        goal = strategy.ensure_pin(self.goal, {}, source=self.source, now=self.now)
        pin = goal["strategy_pin"]
        observed = {"version":1,"goal_id":goal["goal_id"],"rung":pin["rung"],"revision":pin["revision"],"binding":pin["binding"],"evidence_digest":"f"*64}
        self.assertTrue(strategy.valid_exhaustion(goal,observed))
        for change in ({"goal_id":"foreign"},{"revision":True},{"revision":pin["revision"]+1},{"binding":"a"*64},{"rung":"agent"}):
            self.assertFalse(strategy.valid_exhaustion(goal,{**observed,**change}))

    def test_changed_source_records_rebinding_without_silent_downgrade(self):
        goal = strategy.ensure_pin(self.goal, {}, source=self.source, now=self.now)
        changed = strategy.ensure_pin(goal, {"discovery": {"resolution": {"existence_state": "confirmed"}}}, source="b" * 40, now=self.now)
        self.assertEqual(changed["strategy_pin"]["rung"], "model_adapter")
        self.assertEqual(changed["strategy_history"][-1]["reason"], "source_changed")
        with self.assertRaises(ValueError):
            strategy.advance(goal, source="b" * 40, now=self.now, reason="old_failure")
        with self.assertRaises(ValueError):
            strategy.set_pin(changed, 0, source="b" * 40, fingerprint="f" * 64, now=self.now, reason="hidden_downgrade")


class BudgetTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.goal = goals.new_goal("goal", fixtures.scope(), now=self.now)

    def report(self, **changes):
        return {"version": 1, "run_id": "child", "generation": 1, "state_version": 2, "model_calls": 1,
            "tokens": 100, "provider_seconds": 3, "active_seconds": 5, "model_usd": 0.2, "builds": 1, "previews": 1, **changes}

    def test_cumulative_usage_replay_and_old_or_contradictory_reports(self):
        goal = budget.account_usage(self.goal, self.report(), run_id="child")
        self.assertEqual(budget.account_usage(goal, self.report(), run_id="child"), goal)
        self.assertEqual(budget.account_usage(goal, self.report(state_version=1), run_id="child"), goal)
        with self.assertRaises(ValueError):
            budget.account_usage(goal, self.report(active_seconds=6), run_id="child")
        with self.assertRaises(ValueError):
            budget.account_usage(goal, self.report(state_version=3, active_seconds=4), run_id="child")
        updated = budget.account_usage(goal, self.report(state_version=3, active_seconds=8), run_id="child")
        self.assertEqual(updated["budget_used"]["active_seconds"], 8)
        self.assertEqual(updated["budget_used"]["model_cost"], 0.2)

    def test_same_fingerprint_twice_escalates_once_and_waiting_does_not_count(self):
        goal, effective, fresh = budget.record_policy_occurrence(self.goal, occurrence="one", fingerprint="f" * 64, classification="repairable")
        self.assertEqual((effective, fresh), ("repairable", True))
        goal, effective, fresh = budget.record_policy_occurrence(goal, occurrence="two", fingerprint="f" * 64, classification="repairable")
        self.assertEqual((effective, fresh), ("strategy", True))
        replay, effective, fresh = budget.record_policy_occurrence(goal, occurrence="two", fingerprint="f" * 64, classification="repairable")
        self.assertFalse(fresh)
        self.assertEqual(replay, goal)
        goal, effective, _ = budget.record_policy_occurrence(goal, occurrence="three", fingerprint="f" * 64, classification="strategy")
        self.assertEqual(effective, "unsupported")
        before = deepcopy(goal["fingerprint_counts"])
        goal, _, _ = budget.record_policy_occurrence(goal, occurrence="wait", fingerprint="f" * 64, classification="waiting")
        self.assertEqual(goal["fingerprint_counts"], before)

    def test_every_budget_stops_at_its_exact_limit(self):
        self.assertIsNone(budget.exhaustion(self.goal, now=self.now))
        self.assertEqual(budget.exhaustion(self.goal, now=self.now + timedelta(hours=48)), "wall_time")
        for key, value, reason in (("active_seconds",7200,"active_compute"),("model_cost",25,"model_cost")):
            goal = {**self.goal, "budget_used": {key:value}}
            self.assertEqual(budget.exhaustion(goal, now=self.now), reason)
        for name, limit in (("transient",20),("repairable",6)):
            goal = deepcopy(self.goal)
            goal["attempts_by_class"][name] = limit
            self.assertEqual(budget.exhaustion(goal, now=self.now), name+"_attempts")

    def test_unknown_cost_stays_unknown_and_prevents_more_model_work(self):
        goal = budget.account_usage(self.goal, self.report(model_usd=None), run_id="child")
        self.assertIsNone(goal["budget_used"]["model_cost"])
        goal["strategy_pin"] = {"rung":"agent"}
        self.assertEqual(budget.exhaustion(goal, now=self.now, action="setup"), "usage_unknown")
        for changes in ({"tokens":True},{"active_seconds":float("nan")},{"model_usd":-1},{"run_id":"foreign"}):
            with self.assertRaises(ValueError):
                budget.account_usage(self.goal, self.report(**changes), run_id="child")

    def test_repeated_transient_escalates_and_alternating_failures_remain_bounded(self):
        goal = self.goal
        for number, expected in enumerate(("transient", "repairable", "strategy", "unsupported"), 1):
            goal, effective, fresh = budget.record_policy_occurrence(goal, occurrence=f"same-{number}", fingerprint="f"*64, classification="transient")
            self.assertEqual(effective, expected)
            self.assertTrue(fresh)
        goal = self.goal
        for number in range(20):
            goal = goals.record_failure(goal, failure_class="transient", occurrence=f"alternating-{number}", fingerprint=f"{number:064x}")
        self.assertEqual(budget.exhaustion(goal, now=self.now), "transient_attempts")


@override_settings(ARTICLES_WEBSITE_STRATEGY_ENABLED=True, ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True)
class RunnerPolicyTests(SimpleTestCase):
    def test_failure_replay_does_not_advance_pin_twice_and_repair_keeps_its_rung(self):
        from . import website_goal_runner as runner
        op = fixtures.GoalRunnerRegressions().operation()
        now = timezone.now()
        op.receipt["goal"]["current_source_sha"] = "a"*40
        op.receipt["goal"] = strategy.set_pin(op.receipt["goal"],0,source="a"*40,fingerprint="f"*64,now=now,reason="fixture_inventory")
        op.receipt.update(failure={"failure_class":"repairable","failure_fingerprint":"sha256:"+"f"*64},failure_occurrence_id="first", current_step="setup",setup_run_id="old",child_run_id="old")
        runner.finish_goal(op,now=now)
        self.assertEqual(op.state,"pending")
        self.assertEqual(op.receipt["goal"]["strategy_pin"]["rung"],"adopt")
        epoch = op.payload["retry_epoch"]
        runner.finish_goal(op,now=now)
        self.assertEqual(op.payload["retry_epoch"],epoch)
        op.receipt["failure_occurrence_id"] = "second"
        runner.finish_goal(op,now=now)
        self.assertEqual(op.receipt["goal"]["strategy_pin"]["rung"],"native_collection")
        self.assertTrue(op.receipt["goal"]["strategy_requires_candidate"])
        self.assertNotEqual(op.payload["retry_epoch"],epoch)
        pin = deepcopy(op.receipt["goal"]["strategy_pin"])
        runner.finish_goal(op,now=now)
        self.assertEqual(op.receipt["goal"]["strategy_pin"],pin)
