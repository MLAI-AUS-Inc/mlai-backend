"""Accounting retries do not stop goals, bypass a lost CAS, or dispatch work."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_cost_recovery as recovery, website_goal_runner as runner, website_prepare as prepare
from . import tests_website_goal_unit as goal_fixtures
from .tests_website_usage_settlement_unit import report


class AccountingRecoveryTests(SimpleTestCase):
    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=False)
    def test_feature_off_does_not_reserve_or_process_recovery(self):
        operation = goal_fixtures.GoalRunnerRegressions().operation()
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.filter") as rows:
            self.assertEqual(recovery.pending_for(operation), [])
            self.assertIsNone(recovery.reserve(operation, now=timezone.now()))
            self.assertIsNone(recovery.advance("unused"))
        rows.assert_not_called()

    def test_recovery_references_are_bounded_and_never_coerced(self):
        self.assertEqual(recovery.identifiers(["second", "first", "second"]), ["first", "second"])
        for invalid in (None, {}, "run", [True], [""], ["x" * 101], [str(value) for value in range(65)]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recovery.identifiers(invalid)

    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
    def test_failure_keeps_a_durable_reference_and_success_clears_it_without_changing_usage(self):
        operation = SimpleNamespace(connection_id="connection", generation=2,
            connection=SimpleNamespace(organization_id=4, repository_id=42), receipt={"setup_run_id": "meter-run"})
        goal = goal_fixtures.GoalRunnerRegressions().operation().receipt["goal"]
        child = SimpleNamespace(status="completed", result={"usage": report()},
            run_request={"website_connection_id": "connection", "connection_generation": 2,
                "repository_id": 42, "deletion_epoch": 3})
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs, \
                patch("content_factory.website_operations.deletion_epoch", return_value=3), \
                patch("content_factory.website_cost_ledger.observe", side_effect=[False, True]):
            runs.return_value.first.return_value = child
            failed = runner.account_child_usage(operation, goal)
            self.assertEqual(failed["cost_pending_run_ids"], ["meter-run"])
            recovered = runner.account_child_usage(operation, failed)
        self.assertEqual(recovered["cost_pending_run_ids"], [])
        self.assertEqual(recovered["budget_used"], failed["budget_used"])
        self.assertNotIn("usage_invalid", recovered)

    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True, ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False)
    def test_only_a_successful_goal_compare_and_set_can_reserve_recovery(self):
        operation = goal_fixtures.GoalRunnerRegressions().operation()
        operation.receipt["goal"]["cost_pending_run_ids"] = ["meter-run"]
        now = timezone.now()
        with patch.object(prepare.transaction, "atomic", return_value=nullcontext()), \
                patch.object(recovery, "lock_connection", return_value=True), \
                patch.object(prepare, "_persist_prepare_state", return_value=None) as persist, \
                patch.object(recovery, "reserve") as reserve:
            self.assertIsNone(prepare._persist_prepare(operation, now=now))
            reserve.assert_not_called()
            persist.return_value = operation
            self.assertIs(prepare._persist_prepare(operation, now=now), operation)
            reserve.assert_called_once_with(operation, now=now)

    @override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True, ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False)
    def test_erased_or_replaced_connection_cannot_commit_pending_accounting_work(self):
        operation = goal_fixtures.GoalRunnerRegressions().operation()
        operation.receipt["goal"]["cost_pending_run_ids"] = ["meter-run"]
        with patch.object(prepare.transaction, "atomic", return_value=nullcontext()), \
                patch.object(recovery, "lock_connection", return_value=False), \
                patch.object(prepare, "_persist_prepare_state") as persist, patch.object(recovery, "reserve") as reserve:
            self.assertIsNone(prepare._persist_prepare(operation, now=timezone.now()))
        persist.assert_not_called()
        reserve.assert_not_called()
