"""Plan steps 7 and 8: a merged setup is Ready on MLAI's own proof; the founder's host is a status."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from .activation import article_capabilities, durable_integration_evidence, live_deployment_verified
from .article_publication_policy import journey_can_publish
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import DIGEST, LISTING, MERGED, LiveUrlFixture, _connect, _next_js_target
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import authority_guard
from .website_contract import WebsiteAuthorityError
from .website_journey import project_journey
from . import website_live_status as live_status
from .website_models import WebsiteConnectionOperation, WebsiteConnectionTarget

READY_OFF = {"ARTICLES_READY_ON_MERGE_ENABLED": False}
ACCESS = {"verified": True, "writable": True, "branch": "main", "sha": MERGED}
ACCOUNT = {"saved": True, "owned": True}
LISTING_URL = "https://lavie.example.test/blog"


class MergedLegacySite(LiveUrlFixture):
    """A legacy-cohort site whose setup PR merged and whose post-merge scan carried MLAI's proof to the merge."""

    def setUp(self):
        super().setUp()
        cache.delete(live_status.THROTTLE_KEY)
        self.built = make_history_free_org(domain="lavie.example.test")
        self.connection = _connect(self.built, verified_sha=MERGED)
        self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True, "generationReady": True}
        self.connection.last_verified_at = timezone.now() - timedelta(minutes=5)
        self.connection.save(update_fields=["capabilities", "last_verified_at"])
        self.target = _next_js_target(self.connection)
        self.built.config.refresh_from_db()
        self.config = self.built.config

    def journey(self, *, deployed=False, watch=None, **proof):
        """The owner's journey for these facts, as ``journey_for_context`` projects them."""
        receipt = {"status": "passed", "source_sha": MERGED, "connection_generation": self.connection.generation,
            "target_id": "articles", "public_url": LISTING_URL, "checked_at": timezone.now().isoformat()} if deployed else {}
        website = {"status": "connected", "repositoryId": 777, "verifiedSha": MERGED, "connectionGeneration": self.connection.generation,
            "writePolicy": {"allowed": True}}
        capabilities = article_capabilities(self.config, domain=self.built.organization.domain, account=ACCOUNT,
            repository_access=ACCESS, evidence=durable_integration_evidence(self.config, self.connection))
        return project_journey(company_id="42", domain=self.built.organization.domain, website=website, capabilities=capabilities,
            discovery={"complete": True, "sourceSha": MERGED, "supportPaths": []},
            target={"key": "articles", "contract": self.target.contract, "deployment_receipt": receipt},
            proof={"integrationVerified": True, "buildVerified": True, **proof}, live_watch=watch)

    def live(self):
        return self.fetch({LISTING_URL: LISTING})

    def not_deployed(self):
        return self.fetch({LISTING_URL: b"<html><head></head><body>Old blog</body></html>"})


class ReadyOnMergeTests(MergedLegacySite, TestCase):
    def test_a_merged_setup_whose_host_never_deploys_is_ready_and_can_publish(self):
        self.assertFalse(live_deployment_verified(self.connection, self.target))
        evidence = durable_integration_evidence(self.config, self.connection)
        self.assertEqual((evidence["verified"], evidence["publishingVerified"], evidence["liveVerified"]), (True, True, False))
        capabilities = article_capabilities(self.config, domain="lavie.example.test", account=ACCOUNT, repository_access=ACCESS, evidence=evidence)
        self.assertTrue(capabilities["canGenerateArticle"])
        self.assertTrue(capabilities["canPublishArticle"])
        self.assertEqual(capabilities["publishingReasonCode"], "")
        journey = self.journey(readyOnMerge=True)
        self.assertTrue(journey["capabilities"]["canPublishArticle"])
        self.assertTrue(journey_can_publish(journey))
        self.assertEqual(journey["prerequisites"]["deployment"]["status"], "complete")
        self.assertIsNone(journey["nextAction"])
        self.assertEqual(journey["liveStatus"]["status"], "checking")
        self.assertEqual(journey["liveStatus"]["message"], live_status.CHECKING_MESSAGE)

    def test_the_kill_switch_restores_the_host_lock(self):
        with override_settings(**READY_OFF):
            evidence = durable_integration_evidence(self.config, self.connection)
            self.assertEqual((evidence["verified"], evidence["publishingVerified"]), (True, False))
            capabilities = article_capabilities(self.config, domain="lavie.example.test", account=ACCOUNT, repository_access=ACCESS, evidence=evidence)
            self.assertTrue(capabilities["canGenerateArticle"])
            self.assertFalse(capabilities["canPublishArticle"])
            journey = self.journey()
            self.assertFalse(journey_can_publish(journey))
            self.assertEqual(journey["prerequisites"]["deployment"]["status"], "needs_action")
            self.assertIsNone(journey["liveStatus"])

    def test_the_host_deploying_later_flips_the_status_to_live(self):
        with self.not_deployed():
            self.assertEqual(live_status.observe(self.config), "waiting")
        watch = live_status.watch_for(self.connection)
        self.assertEqual((watch.action, watch.state), ("live-status", "pending"))
        self.assertEqual(watch.receipt["last_code"], "live_route_unavailable")
        self.assertTrue(live_status.enabled())
        # The owner can publish throughout.
        self.assertTrue(durable_integration_evidence(self.config, self.connection)["publishingVerified"])
        later = timezone.now() + timedelta(hours=1)
        with self.live():
            self.assertEqual(live_status.observe(self.config, now=later), "live")
        watch.refresh_from_db()
        self.assertEqual((watch.state, watch.receipt["status"]), ("completed", "live"))
        self.assertTrue(live_deployment_verified(self.connection, self.target))
        self.assertTrue(durable_integration_evidence(self.config, self.connection)["liveVerified"])
        receipt = WebsiteConnectionOperation.objects.get(connection=self.connection, action="deployment-verify").receipt
        self.assertEqual((receipt["method"], receipt["artifact_digest"], receipt["source_sha"]), ("live_url", DIGEST, MERGED))
        # Observing only: the target's build proof and age, and the connection's source, are untouched.
        target = WebsiteConnectionTarget.objects.get(pk=self.target.pk)
        self.assertNotIn("live_verification", target.contract)
        self.assertEqual(target.verified_at, self.target.verified_at)
        journey = self.journey(deployed=True, readyOnMerge=True, watch=live_status.summary(watch))
        self.assertEqual(journey["liveStatus"]["status"], "live")
        self.assertEqual(journey["liveStatus"]["message"], "Your articles page is live on your site.")

    def test_a_host_that_has_not_deployed_after_24_hours_is_a_status_and_a_staff_alert(self):
        start = timezone.now()
        with self.not_deployed(), patch("content_factory.website_live_status._probe_host", return_value="vercel"):
            live_status.observe(self.config, now=start)
            watch = live_status.watch_for(self.connection)
            projected = live_status.project(deployed=False, ready=True, watch=live_status.summary(watch), now=start + timedelta(hours=1))
            self.assertEqual(projected["status"], "waiting")
            self.assertEqual(projected["message"],
                "Your host hasn't deployed the latest change yet. Check your Vercel project is connected to this repository.")
            self.assertEqual(live_status.observe(self.config, now=start + timedelta(hours=25)), "waiting")
        watch.refresh_from_db()
        self.assertEqual((watch.state, watch.receipt["reason_code"]), ("needs_attention", "live_route_unavailable"))
        self.assertIsNotNone(watch.next_attempt_at)
        # Still Ready and publishable; the founder is told what to check and that staff know.
        self.assertTrue(durable_integration_evidence(self.config, self.connection)["publishingVerified"])
        projected = live_status.project(deployed=False, ready=True, watch=live_status.summary(watch), now=start + timedelta(hours=25))
        self.assertTrue(projected["staffAlerted"])
        self.assertTrue(projected["message"].endswith("Our team has been told and will help."))
        # The stuck-founder alert sees it as a stopped website step.
        from .founder_stuck_alerts import find_stuck_conditions
        conditions = [row for row in find_stuck_conditions(now=start + timedelta(hours=25)) if row.step == "live-status"]
        self.assertEqual(len(conditions), 1)
        self.assertEqual((conditions[0].kind, conditions[0].code), ("operation_stopped", "live_route_unavailable"))
        # When the host deploys, the watch closes and the alert condition goes away.
        with self.live():
            self.assertEqual(live_status.observe(self.config, now=watch.next_attempt_at), "live")
        watch.refresh_from_db()
        self.assertEqual(watch.state, "completed")

    def test_the_sweep_checks_ready_sites_and_leaves_others(self):
        with self.not_deployed():
            result = live_status.run_live_status_sweep()
        self.assertEqual((result["status"], result["waiting"]), ("completed", 1))
        self.assertEqual(live_status.run_live_status_sweep()["status"], "throttled")
        cache.delete(live_status.THROTTLE_KEY)
        # Not due yet: the sweep does not even load it.
        self.assertEqual(live_status.run_live_status_sweep()["waiting"], 0)
        with override_settings(**READY_OFF):
            self.assertEqual(live_status.run_live_status_sweep()["status"], "disabled")


class PrepareDeadlineTests(MergedLegacySite, TestCase):
    def prepare(self):
        return WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
            action="prepare", state="running", idempotency_key="prepare-1", payload={"company_id": "42"},
            receipt={"current_step": "verify"})

    def test_a_deployment_wait_past_24_hours_keeps_waiting_and_alerts_staff(self):
        from .website_prepare import prepare_failure
        op = self.prepare()
        start = timezone.now()
        error = WebsiteAuthorityError("live_route_unavailable", "The public blog page has not deployed yet.", retryable=True)
        prepare_failure(op, "verify", error, now=start)
        self.assertEqual((op.state, op.receipt["status"]), ("pending", "waiting_for_deployment"))
        prepare_failure(op, "verify", error, now=start + timedelta(hours=25))
        self.assertEqual((op.state, op.receipt["status"]), ("pending", "waiting_for_deployment"))
        self.assertNotIn("user_action", op.receipt)
        self.assertEqual(op.next_attempt_at, start + timedelta(hours=27))
        self.assertEqual(op.receipt["message"], live_status.HOST_HELP["unknown"] + live_status.STAFF_TOLD)
        alert = WebsiteConnectionOperation.objects.get(action="live-status")
        self.assertEqual((alert.state, alert.receipt["reason_code"]), ("needs_attention", "live_route_unavailable"))
        op.state = "completed"
        from .website_prepare import _finish_prepare
        with patch("content_factory.website_prepare._persist_prepare") as persist:
            _finish_prepare(op, now=start + timedelta(hours=26))
        persist.assert_called_once()
        alert.refresh_from_db()
        self.assertEqual(alert.state, "completed")

    def test_with_the_kill_switch_off_the_deadline_still_asks_to_deploy(self):
        from .website_prepare import prepare_failure
        op = self.prepare()
        start = timezone.now()
        error = WebsiteAuthorityError("live_route_unavailable", "The public blog page has not deployed yet.", retryable=True)
        with override_settings(**READY_OFF):
            prepare_failure(op, "verify", error, now=start)
            prepare_failure(op, "verify", error, now=start + timedelta(hours=25))
        self.assertEqual((op.state, op.receipt["user_action"]["id"]), ("needs_user", "deploy_site"))
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action="live-status").exists())

    def test_the_verify_step_finishes_once_ci_proof_is_recorded_even_if_the_host_lags(self):
        from . import website_prepare
        op = SimpleNamespace(receipt={}, pk="op")
        context = SimpleNamespace(organization=self.built.organization, company=self.built.company)
        ci = {"evidence_digest": "e"}
        lag = WebsiteAuthorityError("live_route_unavailable", "Not deployed yet.")
        with patch("content_factory.website_verification.current_ci_attestation", return_value=ci), \
                patch("content_factory.website_verification.verify_live_deployment", side_effect=lag):
            self.assertEqual(website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={}),
                {"status": "completed"})
            with override_settings(**READY_OFF), self.assertRaises(WebsiteAuthorityError):
                website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={})
        other = WebsiteAuthorityError("ci_marker_identity_mismatch", "The CI proof names another marker.")
        with patch("content_factory.website_verification.current_ci_attestation", return_value=ci), \
                patch("content_factory.website_verification.verify_live_deployment", side_effect=other), \
                self.assertRaises(WebsiteAuthorityError):
            website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={})


    @override_settings(ARTICLES_LIVE_URL_VERIFICATION_ENABLED=True)
    def test_a_no_workflow_verify_step_finishes_on_mlais_build_proof_even_if_the_host_lags(self):
        from . import website_prepare
        from .website_live_verification import selected
        self.assertTrue(selected(self.built.organization))
        op = SimpleNamespace(receipt={}, pk="op")
        context = SimpleNamespace(organization=self.built.organization, company=self.built.company)
        with patch("content_factory.website_verification.current_ci_attestation", return_value=None), \
                patch("content_factory.website_verification.discover_source_attestation") as discover, self.not_deployed():
            self.assertEqual(website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={}),
                {"status": "completed"})
            discover.assert_not_called()
            with override_settings(**READY_OFF), self.assertRaises(WebsiteAuthorityError) as raised:
                website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={})
        self.assertEqual(raised.exception.code, "live_route_unavailable")


class PublishGuardTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.website.verified_sha = "a" * 40
        self.website.last_verified_at = timezone.now()
        self.website.save(update_fields=["verified_sha", "last_verified_at"])
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=1, target_key="native", adapter="react_component",
            source_sha="a" * 40, verified_at=timezone.now(), capabilities={"publishingReady": True},
            contract={"contract_digest": "d" * 64, "live_marker": {"value": "e" * 64}})
        self.config.default_publish_target_id = "native"
        self.config.save(update_fields=["default_publish_target_id"])

    def test_publishing_needs_the_verified_target_not_the_hosts_deployment(self):
        with authority_guard({**self.binding, "expected_source_sha": "a" * 40}, action="publish"):
            pass
        with override_settings(**READY_OFF), self.assertRaises(WebsiteAuthorityError) as error:
            with authority_guard({**self.binding, "expected_source_sha": "a" * 40}, action="publish"):
                pass
        self.assertEqual(error.exception.code, "deployment_verification_required")
        # A target without MLAI's proof at the verified source still refuses.
        WebsiteConnectionTarget.objects.filter(target_key="native").update(source_sha="b" * 40)
        with self.assertRaises(WebsiteAuthorityError) as error:
            with authority_guard({**self.binding, "expected_source_sha": "a" * 40}, action="publish"):
                pass
        self.assertEqual(error.exception.code, "website_target_verification_required")
