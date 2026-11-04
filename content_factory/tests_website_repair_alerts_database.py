"""Repair incident storage/lineage in the existing disposable PostgreSQL CI lane."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from . import website_operational_alert_delivery as delivery
from .tests_website_connections import WebsiteDatabaseFixture
from .tests_website_cost_database import seed
from .tests_setup_delivery_unit import envelope, signed
from .website_models import WebsiteConnectionOperation
from workflow_runs.models import ContentFactoryRun


class RepairOperationalAlertTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        rollout = override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
            ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)],
            CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="C0000000000")
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.parent, self.run = seed(self.website, str(uuid4()), "repair-alert-run")
        child = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="workflow", state="running", idempotency_key="repair-alert-child", payload={"attempt": 1})
        self.snapshot = envelope(kind="run_snapshot", status="completed", event_id="repair-alert-snapshot")
        self.snapshot["run_id"] = self.run.run_id
        self.snapshot["scope"].update(domain=self.org.domain, github_repo=self.website.github_repo,
            website_connection_id=str(self.website.pk), connection_generation=self.website.generation,
            repository_id=self.website.repository_id, operation_id=str(child.pk), client_request_id="repair-alert-dispatch")
        signed(self.snapshot)
        self.run.run_request = {**self.run.run_request, **self.snapshot["scope"]}
        self.run.status = "running"
        self.run.save(update_fields=["run_request", "status", "updated_at"])
        self.now = timezone.now()

    def reserve(self, snapshot=None):
        return delivery.reserve_repair(self.website, self.run, snapshot or self.snapshot, now=self.now)

    def test_original_goal_attribution_and_repair_identity_are_preserved_on_replay(self):
        with transaction.atomic():
            first, repeated = self.reserve(), self.reserve()
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(first.payload["goal_operation_id"], str(self.parent.pk))
        self.assertEqual(first.payload["alert"]["goal_id"], str(self.parent.pk))
        self.assertEqual(first.payload["scope"], self.parent.receipt["goal"]["scope"])
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).count(), 1)

    def test_legacy_repair_remains_explicitly_unattributed(self):
        original = deepcopy(self.run.run_request)
        original.pop("goal_budget")
        legacy = ContentFactoryRun.objects.create(run_id="legacy-repair-alert", organization=self.org,
            workflow="article_system_setup", status="running", run_request=original)
        snapshot = deepcopy(self.snapshot)
        snapshot["run_id"] = legacy.run_id
        signed(snapshot)
        with transaction.atomic():
            row = delivery.reserve_repair(self.website, legacy, snapshot, now=self.now)
        self.assertIsNone(row.payload["alert"]["goal_id"])
        self.assertEqual(row.payload["scope"]["company_id"], "unattributed")
        def acknowledged(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertIn("Goal unavailable", kwargs["text"])
            self.assertIn("setup_delivery_projection_repaired", kwargs["text"])
            return {"ok": True, "ts": "1234567890.123456", "channel": "C0000000000"}
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            self.assertEqual(delivery.advance(row.pk, now=self.now), "completed")
            self.assertIsNone(delivery.advance(row.pk, now=self.now))
            self.assertEqual(client.return_value.chat_postMessage.call_count, 1)

    def test_rolled_back_projection_cannot_leave_a_repair_alert(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            with transaction.atomic():
                ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="completed")
                self.reserve()
                raise RuntimeError("synthetic rollback")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action=delivery.ACTION).exists())

    def test_another_accepted_state_version_is_a_distinct_causal_repair(self):
        newer = deepcopy(self.snapshot)
        newer["execution"]["state_version"] += 1
        newer["body"]["state_projection"]["state_version"] += 1
        signed(newer)
        with transaction.atomic():
            first, second = self.reserve(), self.reserve(newer)
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.payload["alert"]["occurrence"], second.payload["alert"]["occurrence"])
