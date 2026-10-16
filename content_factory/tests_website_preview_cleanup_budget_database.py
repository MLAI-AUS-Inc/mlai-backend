"""Preview-stop budgets and incident atomicity in the existing disposable PostgreSQL CI lane."""
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .tests_website_connections import WebsiteDatabaseFixture
from .website_models import WebsiteConnectionOperation
from . import website_preview_cleanup_budget as policy, website_operational_alert_delivery as delivery
from .website_reconciliation import process_website_connection_operations


class PreviewCleanupBudgetDatabaseTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        rollout = override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
            ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False,
            ARTICLES_COST_ACCOUNTING_ENABLED=False, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000")
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.now = timezone.now()
        self.op = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="rotate", state="pending", idempotency_key="synthetic-preview-cleanup",
            payload={"previous_generation": self.website.generation, "stop_preview_run_ids": ["synthetic-preview"],
                     "cancel_run_ids": [], "disable_auto_merge_prs": []})

    @contextmanager
    def remote(self, response_status=202, *, body=None):
        with ExitStack() as stack:
            stack.enter_context(patch("content_factory.website_support.reconcile_support_verifications"))
            stack.enter_context(patch("content_factory.website_reconciliation.monitor_cleanup_pull_requests"))
            stack.enter_context(patch("content_factory.website_tokens.revoke_generation_tokens", return_value={"revoked": 0, "pending": 0}))
            stack.enter_context(patch("content_factory.website_reconciliation.disable_pending_native_auto_merge", return_value=[]))
            stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "https://synthetic-worker.invalid"}))
            stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={}))
            stack.enter_context(patch.object(delivery, "advance", return_value=None))

            def response(*args, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                # The clock must already be durable if transport dies here.
                saved = WebsiteConnectionOperation.objects.get(pk=self.op.pk)
                self.assertIn(policy.KEY, saved.receipt)
                return Mock(status_code=response_status, json=Mock(return_value=body or {"cleanup_pending": True}))

            sent = stack.enter_context(patch("integrations.http_client.post", side_effect=response))
            yield sent

    def expire(self):
        policy.start(self.op, now=self.now)
        self.op.receipt = dict(self.op.receipt)
        with self.assertRaises(policy.PreviewCleanupBudgetReached):
            policy.begin(self.op, now=self.now + policy.WALL_TIME)
        self.op.attempts = 1
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(attempts=1)
        self.op.refresh_from_db(fields=["updated_at"])
        return self.op.updated_at

    def test_repeated_202_stops_at_the_original_deadline_and_enqueues_one_incident(self):
        with self.remote() as sent:
            for at in (self.now, self.now + timedelta(hours=1), self.now + timedelta(hours=23)):
                process_website_connection_operations(now=at, connection_id=self.website.pk)
            self.assertEqual(sent.call_count, 3)
            self.op.refresh_from_db()
            window = self.op.receipt[policy.KEY]
            self.assertEqual(window["started_at"], self.now.isoformat())
            process_website_connection_operations(now=self.now + policy.WALL_TIME, connection_id=self.website.pk)
            process_website_connection_operations(now=self.now + timedelta(days=2), connection_id=self.website.pk)
            self.assertEqual(sent.call_count, 3)
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertEqual(self.op.receipt["status"], "preview_cleanup_abandoned")
        self.assertEqual(self.op.payload["stop_preview_run_ids"], ["synthetic-preview"])
        self.assertNotIn("user_action", self.op.receipt)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).count(), 1)

    def test_confirmed_cleanup_before_the_deadline_needs_no_incident(self):
        with self.remote(200, body={"cleanup_success": True, "cleanup_pending": False}) as sent:
            process_website_connection_operations(now=self.now, connection_id=self.website.pk)
            self.assertEqual(sent.call_count, 1)
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "completed")
        self.assertEqual(self.op.receipt[policy.KEY]["status"], "completed")
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())

    def test_a_lost_lifecycle_claim_cannot_reserve_a_deadline_incident(self):
        claim = self.expire()
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(state="cancelled")
        self.assertEqual(policy.persist(self.op, claimed_at=claim, now=self.now + policy.WALL_TIME), 0)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "cancelled")

    def test_reservation_failure_rolls_back_the_cleanup_transition(self):
        claim = self.expire()
        with patch.object(delivery, "reserve_cleanup", side_effect=RuntimeError("synthetic reservation failure")):
            with self.assertRaises(RuntimeError):
                policy.persist(self.op, claimed_at=claim, now=self.now + policy.WALL_TIME)
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "pending")
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())

    def test_cleanup_incident_delivery_is_acknowledged_once_outside_database_locks(self):
        claim = self.expire()
        self.assertEqual(policy.persist(self.op, claimed_at=claim, now=self.now + policy.WALL_TIME), 1)
        row = WebsiteConnectionOperation.objects.get(action=delivery.ACTION)
        self.assertIsNone(row.payload["alert"]["goal_id"])

        def acknowledge(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertIn("preview_cleanup_abandoned", kwargs["text"])
            self.assertNotIn("synthetic-preview", kwargs["text"])
            return {"ok": True, "channel": "C0000000000", "ts": "1234567890.123456"}

        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = acknowledge
            self.assertEqual(delivery.advance(row.pk, now=self.now + policy.WALL_TIME), "completed")
            self.assertIsNone(delivery.advance(row.pk, now=self.now + policy.WALL_TIME))
            self.assertEqual(client.return_value.chat_postMessage.call_count, 1)
