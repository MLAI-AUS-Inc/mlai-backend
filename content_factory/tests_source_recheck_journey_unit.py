"""A pushed website is being checked, not sent back to setup.

After a push to the website's branch the backend re-scans and re-verifies the
new source on its own: a reopened preparation, or the background
``source-reverify`` check the push queued. Until that check settled, the
journey projected a stale inventory with "Scan repository" as the next action,
so My Startup showed a finished website as needing setup again. The owner's
view now reports the facts that check renews as running, with no action, and
surfaces an action only once the check fails. These run without a database.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from .website_journey import SOURCE_CHANGED_REASON, SOURCE_RECHECK_REASON, project_journey, source_check_in_flight

VERIFIED = "a" * 40
PUSHED = "b" * 40


def live_target():
    return {"key": "blog", "sourceSha": VERIFIED, "adapter": "static_markdown", "deployment_receipt": {"status": "passed",
        "source_sha": VERIFIED, "connection_generation": 3, "target_id": "blog", "public_url": "https://example.test/blog",
        "checked_at": timezone.now().isoformat()}}


def pushed_journey(*, source_check=None, allowed=True, setup_pull_request=None, permissions=None):
    """A finished, live website right after a push it has not re-verified."""
    return project_journey(company_id="42", domain="example.test", website={"status": "connected", "repositoryId": 123,
        "connectionGeneration": 3, "verifiedSha": VERIFIED, "writePolicy": {"allowed": allowed}},
        capabilities={"repositoryAccessVerified": True, "repositoryWriteVerified": True, "canGenerateArticle": False,
            "reasonCode": "repository_source_changed", "reason": "The repository source changed. Scan and verify the current source.",
            "repositorySourceSha": PUSHED},
        discovery={"complete": True, "sourceSha": VERIFIED},
        target=live_target(), proof={"integrationVerified": True, "buildVerified": False, "verificationStale": True},
        permissions=permissions, setup_pull_request=setup_pull_request, source_check=source_check)


class CheckingJourneyTests(SimpleTestCase):
    check = {"operationId": "check-1", "reason": SOURCE_CHANGED_REASON}

    def test_without_a_check_the_owner_is_asked_to_scan(self):
        journey = pushed_journey()
        self.assertEqual(journey["nextAction"]["id"], "scan")
        self.assertEqual(journey["prerequisites"]["inventory"]["status"], "stale")

    def test_a_running_check_reports_checking_and_offers_no_action(self):
        journey = pushed_journey(source_check=self.check)
        self.assertIsNone(journey["nextAction"])
        for key, code in (("inventory", "scan_stale"), ("verification", "verification_stale")):
            fact = journey["prerequisites"][key]
            self.assertEqual((fact["status"], fact["reason"], fact["operationId"]), ("running", SOURCE_CHANGED_REASON, "check-1"))
            self.assertEqual(fact["reasonCode"], code)
        steps = {step["id"]: step for step in journey["steps"]}
        self.assertEqual({key: steps[key]["status"] for key in ("scan", "integration", "preview", "activation", "articles")},
            {"scan": "running", "integration": "complete", "preview": "running", "activation": "complete", "articles": "running"})
        self.assertEqual(journey["reason"], SOURCE_CHANGED_REASON)
        self.assertEqual(journey["reasonCode"], "repository_source_changed")
        # Checking changes no permission: publishing still waits for the new proof.
        self.assertFalse(journey["capabilities"]["canPublishArticle"])
        self.assertEqual(journey["allowedActions"], pushed_journey()["allowedActions"])

    def test_owner_needs_that_a_check_cannot_repair_stay_actionable(self):
        paused = pushed_journey(source_check=self.check, allowed=False)
        self.assertEqual(paused["prerequisites"]["inventory"]["status"], "stale")
        setup = pushed_journey(source_check=self.check, setup_pull_request={"runId": "setup-1", "savedSetup": True})
        self.assertEqual(setup["nextAction"]["label"], "Review setup changes")
        grants = pushed_journey(source_check=self.check, permissions={"workflows": True, "checks": False, "statuses": True})
        self.assertEqual(grants["prerequisites"]["permissions"]["status"], "needs_action")

    def test_a_check_changes_nothing_on_a_ready_website(self):
        ready = project_journey(company_id="42", domain="example.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "verifiedSha": VERIFIED, "writePolicy": {"allowed": True}},
            capabilities={"repositoryAccessVerified": True, "repositoryWriteVerified": True, "canGenerateArticle": True},
            discovery={"complete": True, "sourceSha": VERIFIED}, target=live_target(), proof={"integrationVerified": True, "buildVerified": True},
            source_check={"operationId": "refresh-1", "reason": SOURCE_RECHECK_REASON})
        self.assertEqual({step["status"] for step in ready["steps"]}, {"complete"})
        self.assertTrue(ready["capabilities"]["canPublishArticle"])
        self.assertEqual(ready["reason"], "")


def queued(*items):
    rows = MagicMock()
    rows.filter.return_value.order_by.return_value.__getitem__.side_effect = lambda _: list(items)
    return rows


class InFlightCheckTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.connection = SimpleNamespace(generation=3, verified_sha=VERIFIED, blockers=[{"code": "repository_source_changed"}],
            operations=queued())

    def operation(self, action="prepare", state="pending", *, age=timedelta(minutes=5), **payload):
        return SimpleNamespace(pk=uuid4(), action=action, state=state, payload=payload, receipt={},
            updated_at=self.now - age, created_at=self.now - age)

    def test_a_preparation_reopened_for_the_source_is_the_check(self):
        prepare = self.operation(source_reverify=True)
        self.assertEqual(source_check_in_flight(self.connection, prepare, now=self.now),
            {"operationId": str(prepare.pk), "reason": SOURCE_CHANGED_REASON})
        self.connection.operations.filter.assert_not_called()

    def test_the_queued_push_check_counts_when_no_preparation_reopened(self):
        failed, waiting = self.operation("source-reverify", source_sha=PUSHED), self.operation("source-reverify", source_sha=PUSHED)
        failed.receipt = {"status": "verification_pending", "code": "github_ci_evidence_permission_required", "retryable": False}
        self.connection.operations = queued(failed, waiting)
        for latest in (self.operation(state="completed"), self.operation(source_reverify=True, age=timedelta(hours=25)), None):
            with self.subTest(latest=getattr(latest, "state", None)):
                self.assertEqual(source_check_in_flight(self.connection, latest, now=self.now)["operationId"], str(waiting.pk))
        self.connection.operations.filter.assert_called_with(generation=3, action="source-reverify", state="pending",
            created_at__gte=self.now - timedelta(hours=24))

    def test_a_failed_or_missing_check_is_not_reported(self):
        failed = self.operation("source-reverify")
        failed.receipt = {"retryable": False}
        self.connection.operations = queued(failed)
        self.assertIsNone(source_check_in_flight(self.connection, self.operation(state="needs_attention", source_reverify=True), now=self.now))

    def test_an_unchanged_source_is_checked_again(self):
        self.connection.blockers = []
        refresh = self.operation("source-reverify", source_sha=VERIFIED)
        self.connection.operations = queued(refresh)
        self.assertEqual(source_check_in_flight(self.connection, None, {"repositorySourceSha": VERIFIED}, now=self.now)["reason"],
            SOURCE_RECHECK_REASON)
        self.assertEqual(source_check_in_flight(self.connection, None, {"repositorySourceSha": PUSHED}, now=self.now)["reason"],
            SOURCE_CHANGED_REASON)
