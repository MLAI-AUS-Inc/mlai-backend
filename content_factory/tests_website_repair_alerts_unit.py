"""Only accepted authoritative pull repairs create operational incidents."""
from copy import deepcopy
from unittest.mock import Mock, patch
import json
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import tests_setup_delivery_unit as delivery_fixtures
from . import website_operational_alert_delivery as delivery, website_operational_alerts as alerts
from . import website_goal as goals, setup_delivery_reconciliation as reconciliation
from .website_models import WebsiteConnectionOperation


@override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=True, ROO_API_KEY="fixture-service-key",
    ARTICLES_OPERATIONAL_ALERTS_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class ReconcilerAlertTests(SimpleTestCase):
    def setUp(self):
        self.fixture = delivery_fixtures.SetupDeliveryChainTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.website.branch, self.fixture.website.app_root = "main", ""
        self.fixture.website.configuration_version = 1

    def incidents(self):
        return [row for row in WebsiteConnectionOperation.objects.rows if row.action == delivery.ACTION]

    def remote(self, snapshot):
        return Mock(status_code=200, iter_content=Mock(return_value=[json.dumps(snapshot).encode()]))

    def test_actual_lost_terminal_snapshot_creates_one_incident_and_healthy_repeat_creates_none(self):
        snapshot = delivery_fixtures.envelope(kind="run_snapshot", status="completed", event_id="snapshot-terminal")
        with patch.object(reconciliation.requests, "get", return_value=self.remote(snapshot)), \
                patch("integrations.services.slack.SlackService.get_client") as client:
            self.assertEqual(reconciliation.reconcile_setup_delivery_run(self.fixture.run, base_url="https://worker.fixture", headers={}), "repaired")
            self.assertEqual(len(self.incidents()), 1)
            first = self.incidents()[0]
            self.assertEqual(first.payload["alert"]["reason"], "setup_delivery_projection_repaired")
            self.assertIsNone(first.payload["alert"]["goal_id"])
            self.assertEqual(first.payload["scope"]["company_id"], "unattributed")
            self.assertTrue(alerts.valid(first.payload["alert"], first.payload["scope"]))
            self.assertEqual(reconciliation.reconcile_setup_delivery_run(self.fixture.run, base_url="https://worker.fixture", headers={}), "no_change")
            self.assertEqual(len(self.incidents()), 1)
        client.assert_not_called()
        self.fixture.legacy.assert_not_called()
        self.fixture.snapshot.assert_not_called()

    def test_normal_callback_and_shadow_pull_never_reserve_repair_alerts(self):
        self.assertEqual(self.fixture.post().data["disposition"], "applied")
        self.assertEqual(self.incidents(), [])
        snapshot = delivery_fixtures.envelope(kind="run_snapshot", version=2, status="completed", event_id="shadow-terminal")
        with override_settings(CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED=False), \
                patch.object(reconciliation.requests, "get", return_value=self.remote(snapshot)):
            self.assertEqual(reconciliation.reconcile_setup_delivery_run(self.fixture.run, base_url="https://worker.fixture", headers={}), "shadow_validated")
        self.assertEqual(self.incidents(), [])

    def test_callback_winning_the_pull_race_does_not_manufacture_a_repair_incident(self):
        stale = delivery_fixtures.envelope(kind="run_snapshot", status="failed", event_id="stale-snapshot")
        def newer(*args, **kwargs):
            self.assertEqual(self.fixture.post(delivery_fixtures.envelope(version=3, event_id="newer-callback")).data["disposition"], "applied")
            return self.remote(stale)
        with patch.object(reconciliation.requests, "get", side_effect=newer):
            self.assertEqual(reconciliation.reconcile_setup_delivery_run(self.fixture.run, base_url="https://worker.fixture", headers={}), "deferred")
        self.assertEqual(self.incidents(), [])

    @override_settings(ARTICLES_OPERATIONAL_ALERTS_ENABLED=False)
    def test_feature_off_keeps_the_existing_pure_repair_path(self):
        snapshot = delivery_fixtures.envelope(kind="run_snapshot", status="completed", event_id="disabled-terminal")
        with patch.object(reconciliation.requests, "get", return_value=self.remote(snapshot)):
            self.assertEqual(reconciliation.reconcile_setup_delivery_run(self.fixture.run, base_url="https://worker.fixture", headers={}), "repaired")
        self.assertEqual(self.incidents(), [])

    def test_snapshot_identity_is_causal_and_foreign_bindings_are_refused(self):
        snapshot = delivery_fixtures.envelope(kind="run_snapshot", status="completed", event_id="identity-terminal")
        scope = goals.scope_for(self.fixture.website, "unattributed")
        first = alerts.repair_alert(self.fixture.website, self.fixture.run, snapshot, scope=scope, now=timezone.now())
        other = deepcopy(snapshot)
        other["execution"]["state_version"] += 1
        other["body"]["state_projection"]["state_version"] += 1
        delivery_fixtures.signed(other)
        second = alerts.repair_alert(self.fixture.website, self.fixture.run, other, scope=scope, now=timezone.now())
        self.assertNotEqual(first["id"], second["id"])
        other["scope"]["website_connection_id"] = "foreign"
        self.assertIsNone(alerts.repair_alert(self.fixture.website, self.fixture.run, other, scope=scope, now=timezone.now()))
