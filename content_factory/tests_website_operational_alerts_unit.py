"""Operational alerts are causal, gated and redacted, without owner-action notices."""
from copy import deepcopy
from datetime import timedelta
from contextlib import nullcontext
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_operational_alerts as alerts, website_operational_alert_delivery as delivery
from . import website_goal_runner as runner, website_prepare as prepare, website_cost_recovery as recovery
from . import tests_website_goal_unit as goal_fixtures


@override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class OperationalAlertTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.operation = goal_fixtures.GoalRunnerRegressions().operation()
        goal = self.operation.receipt["goal"]
        goal["budget_used"] = {"model_cost": 25, "tokens": 17, "provider_body": "must-remain-private"}
        runner.stop_goal(self.operation, goal, reason="model_cost", now=self.now)

    def test_repeated_budget_observations_keep_one_incident_and_safe_counters(self):
        first = alerts.budget_alert(self.operation, now=self.now)
        self.assertTrue(alerts.valid(first, self.operation.receipt["goal"]["scope"]))
        self.assertEqual(alerts.budget_alert(self.operation, now=self.now + timedelta(seconds=30)), first)
        self.assertEqual(first["usage"]["model_usd"], 25)
        self.assertEqual(first["usage"]["tokens"], 17)
        self.assertNotIn("must-remain-private", str(first))
        self.assertNotIn("must-remain-private", alerts.text(first))

    def test_rollout_and_current_scope_fences_precede_any_alert(self):
        for setting in ("ARTICLES_OPERATIONAL_ALERTS_ENABLED", "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED", "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED"):
            with self.subTest(setting=setting), override_settings(**{setting: False}):
                self.assertIsNone(alerts.budget_alert(self.operation, now=self.now))
        changed = deepcopy(self.operation)
        changed.connection.generation += 1
        self.assertIsNone(alerts.budget_alert(changed, now=self.now))

    def test_unproven_freeform_and_future_events_cannot_be_alerts(self):
        for change in ({"reason": "provider supplied text"}, {"reason": []}, {"id": "forged"},
                {"at": (self.now + timedelta(hours=1)).isoformat()}):
            operation = deepcopy(self.operation)
            operation.receipt["goal"]["budget_alert"].update(change)
            self.assertIsNone(alerts.budget_alert(operation, now=self.now))
        self.operation.state = "needs_user"
        self.assertIsNone(alerts.budget_alert(self.operation, now=self.now))

    def test_saved_payload_and_text_reject_unknown_fields_or_provider_secrets(self):
        first = alerts.budget_alert(self.operation, now=self.now)
        for change in ({"provider_text": "must-remain-private"}, {"version": True}, {"reason": "must-remain-private"}):
            self.assertFalse(alerts.valid({**first, **change}, self.operation.receipt["goal"]["scope"]))
        body = {"kind": "must-remain-private", "reason": "must-remain-private", "provider_text": "must-remain-private",
            "usage": {"tokens": "must-remain-private", "prompt": "must-remain-private"}, "goal_id": "sk-secret-for-test"}
        self.assertNotIn("must-remain-private", alerts.text(body))
        self.assertNotIn("sk-secret", alerts.text(body))
        changed_reason = {**first, "reason": "active_compute"}
        self.assertFalse(alerts.valid(changed_reason, self.operation.receipt["goal"]["scope"]))

    def test_alert_reservation_requires_an_accepted_goal_compare_and_set(self):
        incident = alerts.budget_alert(self.operation, now=self.now)
        with patch.object(prepare.transaction, "atomic", return_value=nullcontext()), \
                patch.object(recovery, "lock_connection", return_value=True), \
                patch.object(prepare, "_persist_prepare_state", return_value=None) as persist, \
                patch.object(delivery, "reserve") as reserve:
            self.assertIsNone(prepare._persist_prepare(self.operation, now=self.now))
            reserve.assert_not_called()
            persist.return_value = self.operation
            self.assertIs(prepare._persist_prepare(self.operation, now=self.now), self.operation)
            reserve.assert_called_once_with(self.operation, incident, now=self.now)

    @override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=False)
    def test_feature_off_delivery_does_not_read_or_send(self):
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.filter") as rows, \
                patch("integrations.services.slack.SlackService.get_client") as client:
            self.assertIsNone(delivery.advance("unused"))
        rows.assert_not_called()
        client.assert_not_called()

    def test_destination_is_only_the_existing_configured_ops_channel(self):
        for value, expected in (("", None), ("https://must-not-send.example", None), ("C0000000000", "C0000000000")):
            with self.subTest(value=value), override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=value):
                self.assertEqual(delivery.channel(), expected)
