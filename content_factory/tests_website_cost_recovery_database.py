"""Synthetic accounting recovery in disposable PostgreSQL CI; no local migrations."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from workflow_runs.models import ContentFactoryRun
from . import website_cost_ledger as ledger, website_cost_recovery as recovery
from . import website_goal_runner as runner, website_prepare as prepare
from .tests_website_connections import WebsiteDatabaseFixture
from .tests_website_cost_database import seed
from .website_connections import transition_connection
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True, ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False)
class AccountingRecoveryPersistenceTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.company_id = str(uuid4())
        self.parent, self.run = seed(self.website, self.company_id, "recover-cost-run")
        self.now = timezone.now()

    def read(self):
        return ledger.read(company_id=self.company_id, organization_id=self.org.pk)

    def enqueue(self, *, lose_claim=False):
        self.parent.state, self.parent.attempts = "running", 1
        self.parent.receipt["setup_run_id"] = self.run.run_id
        self.parent.save(update_fields=["state", "attempts", "receipt", "updated_at"])
        self.parent._claimed_at = self.parent.updated_at
        self.parent._claimed_goal_revision = self.parent.receipt["goal"]["revision"]
        with patch.object(ledger, "record", side_effect=ValueError("synthetic storage failure")), \
                self.assertLogs(ledger.logger, level="WARNING"):
            goal = runner.account_child_usage(self.parent, self.parent.receipt["goal"])
        self.parent.receipt["goal"] = goal
        self.parent.state = "completed"
        if lose_claim:
            WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(state="cancelled")
        result = prepare._persist_prepare(self.parent, now=self.now)
        if lose_claim:
            self.assertIsNone(result)
            return None
        self.assertIsNotNone(result)
        return WebsiteConnectionOperation.objects.get(action=recovery.ACTION)

    def test_observation_outage_reserves_with_goal_completion_then_recovers_without_redispatch(self):
        row = self.enqueue()
        pending = self.read()
        self.assertEqual(pending["recordedRuns"], 1)
        self.assertEqual(pending["pendingObservations"], 1)
        self.assertTrue(pending["coverageIncomplete"])
        self.assertIsNone(pending["metrics"]["tokens"]["total"])
        self.assertEqual(recovery.advance(row.pk, now=self.now), "completed")
        self.assertIsNone(recovery.advance(row.pk, now=self.now))
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 17)
        self.assertEqual(self.read()["pendingObservations"], 0)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.state, "completed")
        self.assertEqual(self.parent.receipt["goal"]["cost_pending_run_ids"], [])
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action="workflow").exists())

    def test_lost_goal_claim_cannot_leave_an_accounting_retry(self):
        self.enqueue(lose_claim=True)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=recovery.ACTION).exists())

    def test_pending_new_snapshot_does_not_present_a_stale_ledger_total_as_complete(self):
        ledger.record(self.run)
        self.run.result["usage"]["state_version"] = 2
        self.run.result["usage"]["tokens"] = 20
        self.run.result["usage"]["accounting"]["tokens"].update(total=20, known_subtotal=20)
        self.run.save(update_fields=["result", "updated_at"])
        row = self.enqueue()
        self.assertIsNone(self.read()["metrics"]["tokens"]["total"])
        recovery.advance(row.pk, now=self.now)
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 20)
        self.assertEqual(self.read()["recordedRuns"], 1)

    def test_retry_backoff_and_budget_exhaustion_never_stop_or_restart_the_goal(self):
        row = self.enqueue()
        with override_settings(ARTICLES_GOAL_TRANSIENT_ATTEMPTS=2), \
                patch.object(ledger, "record", side_effect=ValueError("unavailable")):
            self.assertEqual(recovery.advance(row.pk, now=self.now), "pending")
            self.assertIsNone(recovery.advance(row.pk, now=self.now))
            self.assertEqual(recovery.advance(row.pk, now=self.now + timedelta(seconds=31)), "needs_attention")
            self.assertIsNone(recovery.advance(row.pk, now=self.now + timedelta(hours=1)))
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.state, "completed")
        self.assertIsNone(self.read()["metrics"]["tokens"]["total"])
        row.refresh_from_db()
        self.assertEqual(row.receipt["status"], "accounting_recovery_exhausted")
        self.assertIsNone(row.next_attempt_at)

    def test_recovery_completing_during_a_read_cannot_make_an_old_total_look_complete(self):
        ledger.record(self.run)
        self.run.result["usage"]["state_version"] = 2
        self.run.result["usage"]["tokens"] = 20
        self.run.result["usage"]["accounting"]["tokens"].update(total=20, known_subtotal=20)
        self.run.save(update_fields=["result", "updated_at"])
        row = self.enqueue()
        completed = False
        def complete_after_ledger_snapshot(execute, sql, params, many, context):
            nonlocal completed
            result = execute(sql, params, many, context)
            if not completed and sql.lstrip().startswith("SELECT") and ledger.ACTION in (params or ()):
                completed = True
                self.assertEqual(recovery.advance(row.pk, now=self.now), "completed")
            return result
        with connection.execute_wrapper(complete_after_ledger_snapshot):
            during = self.read()
        self.assertTrue(completed)
        self.assertIsNone(during["metrics"]["tokens"]["total"])
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 20)

    def test_new_pending_batch_invalidates_an_old_recovery_workers_completion(self):
        row = self.enqueue()
        original_record = ledger.record
        def replace_claim(run, **kwargs):
            result = original_record(run, **kwargs)
            self.parent.refresh_from_db()
            self.parent.receipt["goal"]["cost_pending_run_ids"] = ["later-cost-run"]
            self.parent.save(update_fields=["receipt", "updated_at"])
            with transaction.atomic():
                recovery.reserve(self.parent, now=self.now + timedelta(seconds=1))
            return result
        with patch.object(ledger, "record", side_effect=replace_claim):
            self.assertIsNone(recovery.advance(row.pk, now=self.now))
        row.refresh_from_db()
        self.assertEqual(row.state, "pending")
        self.assertEqual(row.payload["run_ids"], ["later-cost-run"])

    @override_settings(ARTICLES_GOAL_TRANSIENT_ATTEMPTS=1)
    def test_expired_crashed_worker_consumes_the_retry_budget(self):
        row = self.enqueue()
        row.state, row.next_attempt_at = "running", self.now - timedelta(seconds=1)
        row.save(update_fields=["state", "next_attempt_at"])
        with patch.object(ledger, "record") as record:
            self.assertEqual(recovery.advance(row.pk, now=self.now), "needs_attention")
        record.assert_not_called()

    def test_disconnect_retains_read_only_recovery_and_purge_does_not_recreate_it(self):
        row = self.enqueue()
        transition_connection(self.config, action="disconnect", expected=self.binding)
        row.refresh_from_db()
        self.assertEqual(row.state, "pending")
        self.assertEqual(recovery.advance(row.pk, now=self.now), "completed")
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 17)
        identifier = row.pk
        self.website.delete()
        self.assertIsNone(recovery.advance(identifier, now=self.now))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=recovery.ACTION).exists())
