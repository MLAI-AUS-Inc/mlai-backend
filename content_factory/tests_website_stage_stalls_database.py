"""Causal stage alerts in the existing disposable PostgreSQL CI lane only."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from . import website_prepare as prepare, website_goal_runner as runner
from . import website_operational_alerts as alerts, website_operational_alert_delivery as delivery
from .tests_website_connections import WebsiteDatabaseFixture
from .tests_website_cost_database import seed
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


class StageStallPersistenceTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        rollout = override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
            ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False,
            CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000")
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.now = timezone.now()
        self.parent, self.run = seed(self.website, str(uuid4()), "stage-stall-original")
        self.parent.state, self.parent.attempts = "running", 1
        self.parent.save(update_fields=["state", "attempts", "updated_at"])
        self.parent._claimed_at = self.parent.updated_at
        self.parent._claimed_goal_revision = self.parent.receipt["goal"]["revision"]

    def expire(self):
        self.parent.receipt["transient_started_at"] = (self.now - timedelta(hours=24)).isoformat()
        prepare.prepare_failure(self.parent, "setup", WebsiteAuthorityError("github_unavailable", "private-provider-body"), now=self.now)

    def test_normal_retry_cannot_enqueue_and_accepted_expiry_keeps_the_first_snapshot(self):
        prepare.prepare_failure(self.parent, "setup", WebsiteAuthorityError("github_unavailable", "private-provider-body"), now=self.now)
        self.assertIsNone(alerts.stall_alert(self.parent, now=self.now))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())
        self.expire()
        self.assertIsNotNone(prepare._persist_prepare(self.parent, now=self.now))
        first = WebsiteConnectionOperation.objects.get(action=delivery.ACTION)
        with transaction.atomic():
            repeated = delivery.reserve(self.parent, alerts.stall_alert(self.parent, now=self.now + timedelta(minutes=5)), now=self.now)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(first.payload["alert"]["stage"], "setup")
        self.assertNotIn("private-provider", str(first.payload))

    def test_lost_goal_claim_cannot_leave_a_stall_incident(self):
        self.expire()
        WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(state="cancelled")
        self.assertIsNone(prepare._persist_prepare(self.parent, now=self.now))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())

    def test_budget_stop_and_policy_expiry_are_separate_deduplicated_causes(self):
        self.expire()
        runner.stop_goal(self.parent, self.parent.receipt["goal"], reason="wall_time", now=self.now)
        self.assertIsNotNone(prepare._persist_prepare(self.parent, now=self.now))
        rows = list(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION))
        self.assertEqual({row.payload["alert"]["kind"] for row in rows}, {"budget", "stall"})
        self.assertEqual(len(rows), 2)

    def test_stall_delivery_has_a_single_mocked_acknowledgement_outside_locks(self):
        self.expire()
        prepare._persist_prepare(self.parent, now=self.now)
        row = WebsiteConnectionOperation.objects.get(action=delivery.ACTION)
        def acknowledged(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertIn("Stage: setup", kwargs["text"])
            self.assertIn("transient_policy_deadline", kwargs["text"])
            self.assertNotIn("private-provider", kwargs["text"])
            return {"ok": True, "ts": "1234567890.123456", "channel": "C0000000000"}
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            self.assertEqual(delivery.advance(row.pk, now=self.now), "completed")
            self.assertIsNone(delivery.advance(row.pk, now=self.now))
            self.assertEqual(client.return_value.chat_postMessage.call_count, 1)
