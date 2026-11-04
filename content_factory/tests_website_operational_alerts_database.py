"""Budget-alert persistence and mocked ops delivery in disposable PostgreSQL CI."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from . import website_goal as goals, website_goal_runner as runner, website_prepare as prepare
from . import website_operational_alerts as alerts, website_operational_alert_delivery as delivery
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import transition_connection
from .website_models import WebsiteConnectionOperation


class BudgetOperationalAlertTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        rollout = override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
            ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False,
            CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000")
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.now = timezone.now()
        self.parent = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="prepare", state="running", attempts=1, idempotency_key="budget-goal",
            payload={"company_id": str(uuid4())})
        goal = goals.new_goal(str(self.parent.pk), goals.scope_for(self.website, self.parent.payload["company_id"]), now=self.now)
        goal["budget_used"] = {"model_cost": 25, "tokens": 100, "provider_body": "must-remain-private"}
        self.parent.receipt = {"goal": goal}
        self.parent.save(update_fields=["receipt", "updated_at"])
        self.parent._claimed_at = self.parent.updated_at
        self.parent._claimed_goal_revision = goal["revision"]
        runner.stop_goal(self.parent, goal, reason="model_cost", now=self.now)

    def enqueue(self):
        self.assertIsNotNone(prepare._persist_prepare(self.parent, now=self.now))
        return WebsiteConnectionOperation.objects.get(action=delivery.ACTION)

    def acknowledgement(self, **changes):
        return {"ok": True, "ts": "1234567890.123456", "channel": "C0000000000", **changes}

    def test_incident_reserves_with_goal_stop_and_keeps_its_first_snapshot(self):
        row = self.enqueue()
        self.parent.receipt["goal"]["budget_used"]["tokens"] = 200
        with transaction.atomic():
            replay = delivery.reserve(self.parent, alerts.budget_alert(self.parent, now=self.now), now=self.now)
        self.assertEqual(replay.pk, row.pk)
        row.refresh_from_db()
        self.assertEqual(row.payload["alert"]["usage"]["tokens"], 100)
        self.assertNotIn("must-remain-private", str(row.payload))
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).count(), 1)

    def test_lost_goal_claim_never_leaves_a_durable_alert(self):
        WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(state="cancelled")
        self.assertIsNone(prepare._persist_prepare(self.parent, now=self.now))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())

    def test_acknowledged_ops_delivery_is_not_replayed_or_sent_under_a_lock(self):
        row = self.enqueue()
        def acknowledged(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs["channel"], "C0000000000")
            self.assertFalse(kwargs["unfurl_links"])
            self.assertNotIn("must-remain-private", kwargs["text"])
            return self.acknowledgement()
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            self.assertEqual(delivery.advance(row.pk, now=self.now), "completed")
            self.assertIsNone(delivery.advance(row.pk, now=self.now))
            self.assertEqual(client.return_value.chat_postMessage.call_count, 1)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.state, "needs_attention")
        row.refresh_from_db()
        self.assertEqual(row.receipt["status"], "sent")

    def test_unconfigured_destination_does_not_send_or_use_a_delivery_attempt(self):
        row = self.enqueue()
        with override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=""), \
                patch("integrations.services.slack.SlackService.get_client") as client:
            self.assertEqual(delivery.advance(row.pk, now=self.now), "needs_attention")
        client.assert_not_called()
        row.refresh_from_db()
        self.assertEqual((row.receipt["status"], row.attempts), ("unconfigured", 0))
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.return_value = self.acknowledgement()
            self.assertEqual(delivery.advance(row.pk, now=self.now), "completed")

    def test_uncertain_send_retries_with_the_same_client_message_identity(self):
        row = self.enqueue()
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = [RuntimeError("must-remain-private"), self.acknowledgement()]
            with self.assertLogs(delivery.logger, level="WARNING") as logs:
                self.assertEqual(delivery.advance(row.pk, now=self.now), "pending")
            self.assertNotIn("must-remain-private", str(logs.output))
            self.assertIsNone(delivery.advance(row.pk, now=self.now + timedelta(minutes=1)))
            self.assertEqual(delivery.advance(row.pk, now=self.now + timedelta(minutes=5)), "completed")
            sent = client.return_value.chat_postMessage.call_args_list
            self.assertEqual(sent[0].kwargs["client_msg_id"], sent[1].kwargs["client_msg_id"])

    def test_invalid_acknowledgements_exhaust_delivery_without_changing_goal_state(self):
        row = self.enqueue()
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.return_value = self.acknowledgement(channel="C9999999999")
            for index, expected in enumerate(("pending", "pending", "needs_attention")):
                self.assertEqual(delivery.advance(row.pk, now=self.now + timedelta(minutes=5 * index)), expected)
            self.assertIsNone(delivery.advance(row.pk, now=self.now + timedelta(hours=1)))
            self.assertEqual(client.return_value.chat_postMessage.call_count, 3)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.state, "needs_attention")

    def test_logical_purge_removes_queued_alerts_and_fences_late_senders(self):
        row = self.enqueue()
        transition_connection(self.config, action="purge", expected=self.binding)
        with patch("integrations.services.slack.SlackService.get_client") as client:
            self.assertIsNone(delivery.advance(row.pk, now=self.now))
        client.assert_not_called()
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())
