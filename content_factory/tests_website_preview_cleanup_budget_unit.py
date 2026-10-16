"""Preview cleanup deadlines retain their original scope and never fabricate success."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_preview_cleanup_budget as policy, website_operational_alerts as alerts
from . import website_operational_alert_delivery as delivery
from . import tests_website_goal_unit as fixtures


@override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class PreviewCleanupBudgetTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.op = fixtures.GoalRunnerRegressions().operation()
        self.op.connection_id = self.op.connection.pk
        self.op.generation = self.op.connection.generation
        self.op.action, self.op.state, self.op.attempts = "rotate", "pending", 1
        self.op.payload = {"stop_preview_run_ids": ["synthetic-run"], "cancel_run_ids": ["synthetic-run"],
                           "private_provider_value": "must-remain-private"}
        self.op.receipt = {}

    def expired(self):
        policy.start(self.op, now=self.now)
        with self.assertRaises(policy.PreviewCleanupBudgetReached):
            policy.begin(self.op, now=self.now + policy.WALL_TIME)
        return self.op

    def test_window_starts_when_claimed_not_when_the_operation_was_queued(self):
        self.op.created_at = self.now - timedelta(days=10)
        policy.start(self.op, now=self.now)
        first = deepcopy(self.op.receipt[policy.KEY])
        for minutes in (1, 5, 60, 1200):
            policy.begin(self.op, now=self.now + timedelta(minutes=minutes))
            self.assertEqual(self.op.receipt[policy.KEY], first)
        self.assertEqual(first["started_at"], self.now.isoformat())
        self.assertEqual(self.op.payload["stop_preview_run_ids"], ["synthetic-run"])

    def test_expiry_stops_without_absence_success_or_an_owner_action_and_keeps_all_resource_ids(self):
        payload = deepcopy(self.op.payload)
        self.op.receipt["user_action"] = {"id": "retry_disconnect"}
        self.expired()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertEqual(self.op.receipt["status"], "preview_cleanup_abandoned")
        self.assertTrue(self.op.receipt["remote_cleanup_pending"])
        self.assertEqual(self.op.payload, payload)
        self.assertNotIn("cleanup_success", self.op.receipt)
        self.assertNotIn("user_action", self.op.receipt)
        first = deepcopy(self.op.receipt[policy.KEY])
        with self.assertRaises(policy.PreviewCleanupBudgetReached):
            policy.begin(self.op, now=self.now + timedelta(days=2))
        self.assertEqual(self.op.receipt[policy.KEY], first)

    def test_null_malformed_foreign_and_future_windows_cannot_be_reset_to_new_budgets(self):
        policy.start(self.op, now=self.now)
        original = deepcopy(self.op.receipt[policy.KEY])
        for change in (None, [], {**original, "status": []}, {**original, "version": True},
                {**original, "operation_id": "other"}, {**original, "generation": True},
                {**original, "started_at": (self.now + timedelta(hours=1)).isoformat()},
                {**original, "deadline": (self.now + timedelta(days=2)).isoformat()}):
            with self.subTest(value=change):
                self.op.receipt = {policy.KEY: deepcopy(change)}
                self.op.state = "pending"
                with self.assertRaises(policy.PreviewCleanupBudgetReached):
                    policy.begin(self.op, now=self.now)
                self.assertEqual(self.op.receipt[policy.KEY], change)
                self.assertEqual(self.op.receipt["status"], "preview_cleanup_budget_invalid")
                self.assertIsNone(alerts.cleanup_alert(self.op, now=self.now))

    def test_success_closes_the_same_window_and_operations_without_previews_are_unchanged(self):
        policy.begin(self.op, now=self.now)
        self.op.state = "completed"
        policy.completed(self.op, now=self.now + timedelta(minutes=10))
        self.assertEqual(self.op.receipt[policy.KEY]["status"], "completed")
        self.assertEqual(self.op.receipt[policy.KEY]["started_at"], self.now.isoformat())
        self.assertIsNone(alerts.cleanup_alert(self.op, now=self.now + timedelta(days=2)))
        self.op.receipt, self.op.payload = {}, {"cancel_run_ids": ["synthetic-run"]}
        policy.start(self.op, now=self.now)
        policy.begin(self.op, now=self.now + timedelta(days=2))
        self.assertEqual(self.op.receipt, {})

    def test_deadline_incident_has_no_invented_goal_or_private_provider_payload(self):
        self.expired()
        alert = alerts.cleanup_alert(self.op, now=self.now + policy.WALL_TIME)
        scope = fixtures.goals.scope_for(self.op.connection, "unattributed")
        self.assertTrue(alerts.valid(alert, scope))
        self.assertIsNone(alert["goal_id"])
        self.assertEqual(alert["cleanup_operation_id"], str(self.op.pk))
        self.assertEqual(alert["reason"], "preview_cleanup_abandoned")
        self.assertEqual(alerts.cleanup_alert(self.op, now=self.now + timedelta(days=2)), alert)
        self.assertNotIn("must-remain-private", str(alert))
        self.assertNotIn("must-remain-private", alerts.text(alert))
        self.assertNotIn("synthetic-run", alerts.text(alert))
        for change in ({"goal_id": str(self.op.pk)}, {"cleanup_operation_id": "other"},
                {"provider_body": "must-remain-private"}, {"reason": []}, {"deadline": self.now.isoformat()}):
            self.assertFalse(alerts.valid({**alert, **change}, scope))

    @override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=False)
    def test_delivery_off_still_records_the_cap_but_cannot_send_or_reserve(self):
        self.expired()
        self.assertEqual(self.op.state, "needs_attention")
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.get_or_create") as rows:
            self.assertIsNone(delivery.reserve_cleanup(self.op, now=self.now + policy.WALL_TIME))
        rows.assert_not_called()

    def test_only_accepted_cas_can_reserve_and_connection_lock_precedes_operation_update(self):
        self.expired()
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        order = []
        with patch("django.db.transaction.atomic", return_value=nullcontext()), \
                patch.object(WebsiteConnection.objects, "select_for_update") as connection, \
                patch.object(WebsiteConnectionOperation.objects, "filter") as operations, \
                patch.object(delivery, "reserve_cleanup") as reserve:
            connection.return_value.filter.return_value.first.side_effect = lambda: order.append("connection") or self.op.connection
            operations.return_value.update.side_effect = lambda **kwargs: order.append("operation") or 0
            self.assertEqual(policy.persist(self.op, claimed_at=self.now, now=self.now + policy.WALL_TIME), 0)
            reserve.assert_not_called()
            operations.return_value.update.side_effect = lambda **kwargs: order.append("operation") or 1
            self.assertEqual(policy.persist(self.op, claimed_at=self.now, now=self.now + policy.WALL_TIME), 1)
            reserve.assert_called_once_with(self.op, now=self.now + policy.WALL_TIME)
        self.assertEqual(order, ["connection", "operation", "connection", "operation"])
