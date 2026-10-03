"""No-I/O contention regressions for shared, demand-aware request pacing.

The transaction double exercises admission decisions, not PostgreSQL locking.
Database-backed scheduler tests remain a separate release requirement.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.message_sync import budgets, telemetry
from integrations.services.message_sync.request_priority import current_priority, request_priority
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.message_sync.slack_client import budgeted_client


class BudgetStore:
    def __init__(self):
        self.rows = {}
        self.clock = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.mutex = RLock()
        self.selected = None

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    @contextmanager
    def row(self, app, workspace, method):
        with self.mutex:
            original = deepcopy(self.rows)
            key = (app, workspace, method)
            row = self.rows.setdefault(key, [key, self.clock, None])
            try:
                yield self, list(row), self.clock
            except Exception:
                self.rows = original
                raise

    def execute(self, sql, values):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT"):
            app, workspace, method, now, demand, _ = values
            key = (app, workspace, method)
            row = self.rows.setdefault(key, [key, now, None])
            row[2] = max(row[2] or demand, demand)
        elif normalized.startswith("SELECT"):
            self.selected = self.rows.get(tuple(values))
        elif normalized.startswith("UPDATE bridge_api_budget SET next_admitted_at"):
            admitted, _, key = values
            self.rows[key][1] = admitted
        elif normalized.startswith("UPDATE bridge_api_budget SET cooldown_until"):
            cooldown, _, key = values
            self.rows[key][2] = cooldown
        else:
            raise AssertionError("Unexpected budget SQL in transaction double")

    def fetchone(self):
        return list(self.selected) if self.selected else None


class PriorityBudgetTests(SimpleTestCase):
    def setUp(self):
        self.store = BudgetStore()
        self.gate = patch.object(budgets, "budget_row", self.store.row)
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def admit(self, priority, *, app="A1", workspace="T1", method="conversations.history", interval=1.2):
        budgets.admit_request(app_id=app, workspace_id=workspace, method=method,
                             interval_seconds=interval, priority=priority)

    def test_idle_background_borrows_the_full_existing_allowance(self):
        for _ in range(50):
            self.admit("background")
            self.store.advance(1.2)
        self.assertEqual(len(self.store.rows), 1)

    def test_interactive_deferral_commits_demand_and_reserves_next_free_turn(self):
        self.admit("background")
        self.store.advance(.1)
        with self.assertRaises(BudgetDeferred):
            self.admit("foreground")
        self.assertIn(("A1", "T1", "priority:conversations.history"), self.store.rows)
        self.store.advance(1.1)
        self.admit("background")
        self.store.advance(1.2)
        with self.assertRaises(BudgetDeferred):
            self.admit("background")
        self.admit("foreground")

    def test_inactive_demand_expires_without_a_worker_reset(self):
        self.admit("foreground")
        self.store.advance(31)
        for _ in range(10):
            self.admit("background")
            self.store.advance(1.2)

    def test_demand_is_separate_for_each_app_workspace_and_method(self):
        self.admit("foreground")
        self.store.advance(1.2)
        self.admit("background")
        self.store.advance(1.2)
        with self.assertRaises(BudgetDeferred):
            self.admit("background")
        self.admit("background", app="A2")
        self.admit("background", workspace="T2")
        self.admit("background", method="conversations.replies")

    def test_provider_cooldown_wins_for_every_class(self):
        self.admit("foreground")
        self.store.rows[("A1", "T1", "conversations.history")][2] = self.store.clock + timedelta(seconds=90)
        for priority in ("background", "foreground"):
            with self.assertRaises(BudgetDeferred) as error:
                self.admit(priority)
            self.assertEqual(error.exception.retry_after, 90)
            self.assertEqual(error.exception.before_request_method, "conversations.history")

    def test_500_contending_workers_do_not_multiply_provider_capacity(self):
        def attempt(index):
            try:
                self.admit("foreground" if index % 2 else "background")
                return 1
            except BudgetDeferred:
                return 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            self.assertEqual(sum(pool.map(attempt, range(500))), 1)

    def test_sustained_bulk_race_leaves_foreground_turns_without_raising_global_quota(self):
        """Bulk callers race first at every turn, including those they cannot use."""
        admitted = {"foreground": [], "background": []}

        def attempt(priority):
            try:
                self.admit(priority)
                admitted[priority].append(self.store.clock)
            except BudgetDeferred:
                pass

        attempt("foreground")
        for _ in range(99):  # 100 provider turns in the half-open [0,120s) window.
            self.store.advance(1.2)
            for _ in range(300):
                attempt("background")
            attempt("foreground")
        self.assertEqual(len(admitted["foreground"]), 50)
        self.assertEqual(len(admitted["background"]), 50)
        all_turns = sorted(admitted["foreground"] + admitted["background"])
        self.assertTrue(all((later - earlier).total_seconds() >= 1.2
                            for earlier, later in zip(all_turns, all_turns[1:])))

    def test_continuous_foreground_can_starve_background_until_demand_subsides(self):
        """The bulk cap protects UI; it does not promise an import reservation."""
        for _ in range(50):
            self.admit("foreground")
            with self.assertRaises(BudgetDeferred):
                self.admit("background")
            self.store.advance(1.2)
        self.store.advance(budgets.FOREGROUND_DEMAND_SECONDS)
        for _ in range(50):
            self.admit("background")
            self.store.advance(1.2)

    def test_retry_after_survives_mixed_demand_and_a_later_shorter_response(self):
        self.admit("foreground")
        budgets.record_cooldown(app_id="A1", workspace_id="T1", method="conversations.history", retry_after=90)
        self.store.advance(30)
        budgets.record_cooldown(app_id="A1", workspace_id="T1", method="conversations.history", retry_after=1)
        for remaining in range(60, 0, -1):
            for priority in ("background", "foreground"):
                with self.assertRaises(BudgetDeferred) as error:
                    self.admit(priority)
                self.assertEqual(error.exception.retry_after, remaining)
            self.store.advance(1)
        self.admit("foreground")
        with self.assertRaises(BudgetDeferred):
            self.admit("background")

    def test_restricted_history_never_receives_tier_three_throughput(self):
        self.admit("foreground", interval=60)
        self.store.advance(1.2)
        with self.assertRaises(BudgetDeferred):
            self.admit("background", interval=60)

    def test_restricted_history_keeps_demand_through_the_next_provider_turn(self):
        self.admit("background", interval=60)
        self.store.advance(.1)
        with self.assertRaises(BudgetDeferred):
            self.admit("foreground", interval=60)
        self.store.advance(59.9)
        self.admit("background", interval=60)
        self.store.advance(60)
        with self.assertRaises(BudgetDeferred):
            self.admit("background", interval=60)
        self.admit("foreground", interval=60)

    def test_priority_does_not_delay_methods_that_do_not_compete_with_history(self):
        self.admit("background", method="conversations.mark")
        self.store.advance(1.2)
        self.admit("foreground", method="conversations.mark")
        self.assertNotIn(("A1", "T1", "priority:conversations.mark"), self.store.rows)


class RequestPriorityTests(SimpleTestCase):
    def test_nested_exception_restores_task_priority(self):
        self.assertEqual(current_priority(), "foreground")
        with request_priority("background"):
            with self.assertRaises(RuntimeError), request_priority("foreground"):
                raise RuntimeError("synthetic")
            self.assertEqual(current_priority(), "background")
        self.assertEqual(current_priority(), "foreground")

    @override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_SLACK_APP_ID="ATEST")
    def test_client_priority_is_read_at_call_time_not_wrapper_creation(self):
        wrapped = budgeted_client(MagicMock(), workspace_id="TTEST")
        with patch("integrations.services.message_sync.slack_client.admit_request") as admit, patch.object(telemetry, "record"):
            with request_priority("background"):
                wrapped.api_call("conversations.history")
            wrapped.api_call("conversations.history")
        self.assertEqual([call.kwargs["priority"] for call in admit.call_args_list], ["background", "foreground"])


class HourlyTelemetryTests(SimpleTestCase):
    def setUp(self):
        telemetry.cache.clear()
        sink = patch.object(telemetry, "_enqueue", side_effect=lambda delta: telemetry._write_batch([delta]))
        sink.start()
        self.addCleanup(sink.stop)

    def test_completed_hours_survive_minute_window_and_report_missing_coverage(self):
        with patch.object(telemetry, "time", return_value=3601):
            telemetry.record("synthetic", "admitted", 10)
        result = telemetry.hourly_snapshot(["synthetic", "missing"], hours=3, now=4 * 3600)
        self.assertEqual(result["scopes"]["synthetic"]["admitted"], 10)
        self.assertEqual(result["observed_hours"]["synthetic"], 1)
        self.assertIsNone(result["scopes"]["missing"])
        self.assertIsNone(telemetry.hourly_snapshot(["synthetic"], hours=1, now=3601)["scopes"]["synthetic"])

    def test_hourly_window_is_bounded(self):
        for hours in (0, 169):
            with self.assertRaises(ValueError):
                telemetry.hourly_snapshot([], hours=hours)
