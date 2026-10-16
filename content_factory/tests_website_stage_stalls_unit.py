"""Operational stalls follow actual policy expiry, never a poll counter."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_operational_alerts as alerts, website_operational_alert_delivery as delivery
from . import website_prepare as prepare, website_cost_recovery as recovery
from . import tests_website_goal_unit as fixtures
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class StageStallTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.op = fixtures.GoalRunnerRegressions().operation()
        self.op.connection.capabilities = {}
        self.op.connection.verified_sha = "a" * 40

    def fail(self, code="github_unavailable", step="setup", *, now=None):
        error = WebsiteAuthorityError(code, "provider-secret-must-stay-private")
        if code == "manual_merge_required":
            error.user_action = {"id": "merge_pr", "label": "Merge pull request"}
        prepare.prepare_failure(self.op, step, error, now=now or self.now)

    def test_each_real_deadline_creates_one_causal_incident_and_normal_waits_create_none(self):
        cases = (("github_unavailable", "transient_started_at", "transient_policy_deadline"),
            ("preview_pending", "waiting_started_at", "waiting_policy_deadline"),
            ("deployment_source_unverified", "deployment_wait_started_at", "deployment_policy_deadline"))
        for code, timer, reason in cases:
            with self.subTest(code=code):
                self.setUp()
                self.fail(code)
                self.assertIsNone(alerts.stall_alert(self.op, now=self.now))
                self.fail(code, now=self.now + timedelta(hours=23, minutes=59))
                self.assertIsNone(alerts.stall_alert(self.op, now=self.now + timedelta(hours=23, minutes=59)))
                self.fail(code, now=self.now + timedelta(hours=24))
                first = alerts.stall_alert(self.op, now=self.now + timedelta(hours=24))
                self.assertEqual(first["reason"], reason)
                self.assertEqual(first["started_at"], self.op.receipt[timer])
                self.assertEqual(first["deadline"], (self.now + timedelta(hours=24)).isoformat())
                self.assertTrue(alerts.valid(first, self.op.receipt["goal"]["scope"]))
                self.fail(code, now=self.now + timedelta(hours=25))
                self.assertEqual(alerts.stall_alert(self.op, now=self.now + timedelta(hours=25)), first)
                self.assertNotIn("provider-secret", alerts.text(first))

    @override_settings(ARTICLES_DEPLOYMENT_DETECTION_ENABLED=True)
    def test_manual_deployment_and_explicit_owner_review_are_not_stalls(self):
        self.op.connection.capabilities = {"deploymentFingerprint": {"version": 1, "provider": "github_pages",
            "automation": "manual", "connectionGeneration": self.op.connection.generation, "appRoot": "", "sourceSha": "a" * 40}}
        self.fail("deployment_source_unverified", "verify")
        self.assertEqual(self.op.state, "needs_user")
        self.fail("deployment_source_unverified", "verify", now=self.now + timedelta(days=3))
        self.assertIsNone(alerts.stall_alert(self.op, now=self.now + timedelta(days=3)))
        self.fail("manual_merge_required", "merge", now=self.now + timedelta(days=4))
        self.assertEqual(self.op.state, "needs_user")
        self.assertIsNone(alerts.stall_alert(self.op, now=self.now + timedelta(days=4)))

    def expired(self):
        self.op.receipt["transient_started_at"] = (self.now - timedelta(hours=24)).isoformat()
        self.fail()
        return alerts.stall_alert(self.op, now=self.now)

    def test_stall_reservation_uses_the_successful_goal_cas_and_does_not_notify_owner(self):
        incident = self.expired()
        with patch.object(prepare.transaction, "atomic", return_value=nullcontext()), \
                patch.object(recovery, "lock_connection", return_value=True), \
                patch.object(prepare, "_persist_prepare_state", return_value=None) as persist, \
                patch.object(delivery, "reserve") as reserve, \
                patch("content_factory.website_notification_outbox.reserve_notice") as owner_notice:
            self.assertIsNone(prepare._persist_prepare(self.op, now=self.now))
            reserve.assert_not_called()
            persist.return_value = self.op
            self.assertIs(prepare._persist_prepare(self.op, now=self.now), self.op)
            reserve.assert_called_once_with(self.op, incident, now=self.now)
        owner_notice.assert_not_called()

    def test_changed_authority_unproven_deadlines_and_free_text_cannot_create_alerts(self):
        self.expired()
        original = deepcopy(self.op.receipt["stage_policy_stall"])
        for change in ({"stage": "provider-secret"}, {"reason": []}, {"deadline": []},
                {"deadline": (self.now + timedelta(minutes=1)).isoformat()}, {"at": (self.now + timedelta(hours=1)).isoformat()},
                {"id": "forged"}):
            self.op.receipt["stage_policy_stall"] = {**original, **change}
            self.assertIsNone(alerts.stall_alert(self.op, now=self.now))
        self.op.receipt["stage_policy_stall"] = original
        self.op.connection.generation += 1
        self.assertIsNone(alerts.stall_alert(self.op, now=self.now))

    @override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=False)
    def test_disabled_policy_records_no_stall_and_preserves_the_existing_outcome(self):
        self.op.receipt["transient_started_at"] = (self.now - timedelta(hours=24)).isoformat()
        self.fail()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertNotIn("stage_policy_stall", self.op.receipt)
        self.assertIsNone(alerts.stall_alert(self.op, now=self.now))

    def test_a_new_policy_window_has_a_new_identity(self):
        first = self.expired()
        self.op.receipt["transient_started_at"] = (self.now - timedelta(hours=25)).isoformat()
        self.fail()
        second = alerts.stall_alert(self.op, now=self.now)
        self.assertNotEqual(first["id"], second["id"])
