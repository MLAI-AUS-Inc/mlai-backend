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
        # Nothing has checked the site yet, so nothing claims to be checking.
        self.assertIsNone(journey["liveStatus"])
        with self.not_deployed():
            live_status.observe(self.config)
        journey = self.journey(readyOnMerge=True, watch=live_status.summary(live_status.watch_for(self.connection)))
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

    def test_the_general_operations_worker_leaves_the_watch_to_reach_its_staff_alert(self):
        from .website_reconciliation import process_website_connection_operations
        start = timezone.now()
        with self.not_deployed(), patch("content_factory.website_live_status._probe_host", return_value="vercel"):
            live_status.observe(self.config, now=start)
            watch = live_status.watch_for(self.connection)
            payload = dict(watch.payload)
            moment = start
            while moment < start + timedelta(hours=25):
                moment += timedelta(minutes=30)
                process_website_connection_operations(now=moment)
                watch.refresh_from_db()
                self.assertIn(watch.state, {"pending", "needs_attention"})
                self.assertEqual(watch.payload, payload)
                if watch.next_attempt_at <= moment:
                    live_status.observe(self.config, now=moment)
        watch.refresh_from_db()
        self.assertEqual(watch.receipt["started_at"], start.isoformat())
        self.assertEqual((watch.state, watch.receipt["reason_code"]), ("needs_attention", "live_route_unavailable"))
        projected = live_status.project(deployed=False, ready=True, watch=live_status.summary(watch), now=moment)
        self.assertEqual(projected["status"], "waiting")
        self.assertTrue(projected["message"].startswith(live_status.HOST_HELP["vercel"]))

    def test_a_live_site_gets_a_not_due_recheck_so_the_sweep_moves_on(self):
        with self.live():
            self.assertEqual(live_status.observe(self.config), "live")
        watch = live_status.watch_for(self.connection)
        self.assertEqual(watch.state, "completed")
        self.assertGreater(watch.next_attempt_at, timezone.now() + timedelta(hours=23))
        self.assertEqual(live_status.run_live_status_sweep()["live"], 0)

    def test_skipped_sites_never_use_up_the_sweep(self):
        # A more recently verified site the watch cannot observe (no MLAI build proof).
        other = make_history_free_org(domain="skipped.example.test")
        skipped = _connect(other, verified_sha=MERGED)
        skipped.capabilities = {**skipped.capabilities, "publishingReady": True}
        skipped.last_verified_at = timezone.now()
        skipped.save(update_fields=["capabilities", "last_verified_at"])
        _next_js_target(skipped, build_proof=False)
        with self.not_deployed():
            result = live_status.run_live_status_sweep(limit=1)
        self.assertEqual((result["skipped"], result["waiting"]), (1, 1))
        self.assertIsNone(live_status.watch_for(skipped))
        cache.delete(live_status.THROTTLE_KEY)
        self.assertEqual(live_status.run_live_status_sweep(budget=timedelta(0))["deferred"], True)

    def test_a_newer_source_watch_replaces_the_older_one(self):
        with self.not_deployed():
            live_status.observe(self.config)
        old = live_status.watch_for(self.connection)
        live_status._reserve(self.connection, now=timezone.now(), source_sha="f" * 40)
        old.refresh_from_db()
        self.assertEqual(old.state, "cancelled")

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

    def test_a_deadline_alert_closes_when_its_preparation_stops_waiting(self):
        from .website_prepare import prepare_failure
        op = self.prepare()
        start = timezone.now()
        error = WebsiteAuthorityError("live_route_unavailable", "The public blog page has not deployed yet.", retryable=True)
        prepare_failure(op, "verify", error, now=start)
        prepare_failure(op, "verify", error, now=start + timedelta(hours=25))
        alert = WebsiteConnectionOperation.objects.get(action="live-status")
        # Still waiting (the saved preparation is running): the alert stays.
        self.assertEqual(live_status.close_stale_prepare_alerts(), 0)
        # Cancelled outside _finish_prepare: the sweep closes it.
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(state="cancelled")
        self.assertEqual(live_status.close_stale_prepare_alerts(), 1)
        alert.refresh_from_db()
        self.assertEqual(alert.state, "completed")

    def test_a_sealed_target_that_lags_on_the_host_keeps_its_deployment_wait(self):
        from . import website_prepare
        from .website_sealed_workflow import REQUIRED_VERSION
        self.target.capabilities = {**self.target.capabilities, REQUIRED_VERSION: 3}
        self.target.save(update_fields=["capabilities"])
        op = SimpleNamespace(receipt={}, pk="op")
        context = SimpleNamespace(organization=self.built.organization, company=self.built.company)
        lag = WebsiteAuthorityError("live_route_unavailable", "Not deployed yet.")
        with patch("content_factory.website_verification.current_ci_attestation", return_value={"evidence_digest": "e"}), \
                patch("content_factory.website_verification.verify_live_deployment", side_effect=lag), \
                self.assertRaises(WebsiteAuthorityError) as raised:
            website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={})
        self.assertEqual(raised.exception.code, "live_route_unavailable")

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
        op = SimpleNamespace(receipt={}, pk="op", generation=self.config.website_connection.generation)
        context = SimpleNamespace(organization=self.built.organization, company=self.built.company)
        with patch("content_factory.website_verification.current_ci_attestation", return_value=None), \
                patch("content_factory.website_verification.discover_source_attestation") as discover, self.not_deployed():
            self.assertEqual(website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={}),
                {"status": "completed"})
            discover.assert_not_called()
            with override_settings(**READY_OFF), self.assertRaises(WebsiteAuthorityError) as raised:
                website_prepare._dispatch_prepare_step(op, context, self.config, "verify", user=None, journey={})
        self.assertEqual(raised.exception.code, "live_route_unavailable")


@override_settings(ARTICLES_LIVE_URL_VERIFICATION_ENABLED=True)
class ProofRenewalTests(MergedLegacySite, TestCase):
    """The 7-day proof renewal must not need the founder's host either."""

    def age(self, days):
        aged = timezone.now() - timedelta(days=days)
        WebsiteConnectionTarget.objects.filter(pk=self.target.pk).update(verified_at=aged)
        type(self.connection).objects.filter(pk=self.connection.pk).update(last_verified_at=aged)
        self.connection.refresh_from_db()
        self.target.refresh_from_db()

    def renew(self):
        from . import website_reconciliation as recon
        queued = recon.queue_verification_refreshes(now=timezone.now())
        self.assertEqual(len(queued), 1)
        with self.not_deployed():
            state = recon._process_source_reverification(queued[0], timezone.now())
        return state, WebsiteConnectionOperation.objects.get(pk=queued[0])

    def test_a_merged_site_whose_host_never_deploys_is_still_publishable_after_8_days(self):
        self.age(6)
        state, operation = self.renew()
        self.assertEqual(state, "completed")
        self.assertEqual((operation.receipt["method"], operation.receipt["host_code"]), ("build_proof", "live_route_unavailable"))
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        # MLAI's proof is renewed, the verified source is unchanged and nothing claims the site is live.
        self.assertEqual(self.connection.verified_sha, MERGED)
        self.assertNotIn("live_verification", self.target.contract)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action="deployment-verify").exists())
        later = timezone.now() + timedelta(days=2)
        evidence = durable_integration_evidence(self.config, self.connection, now=later)
        self.assertEqual((evidence["verified"], evidence["publishingVerified"], evidence["liveVerified"]), (True, True, False))

    def test_with_the_kill_switch_off_a_build_proof_alone_still_ages_out(self):
        from . import website_reconciliation as recon
        self.age(6)
        with override_settings(**READY_OFF):
            # As before: a build proof alone is not renewed, so it ages out.
            self.assertEqual(recon.queue_verification_refreshes(now=timezone.now()), [])
        evidence = durable_integration_evidence(self.config, self.connection, now=timezone.now() + timedelta(days=2))
        self.assertEqual(evidence["reasonCode"], "verification_stale")

    @override_settings(ARTICLES_LIVE_URL_VERIFICATION_ENABLED=False)
    def test_without_live_url_verification_a_build_proof_alone_is_renewed(self):
        self.age(6)
        with patch("content_factory.website_verification.discover_source_attestation") as discover:
            state, operation = self.renew()
        discover.assert_not_called()
        self.assertEqual((state, operation.receipt["method"]), ("completed", "build_proof"))
        self.connection.refresh_from_db()
        evidence = durable_integration_evidence(self.config, self.connection, now=timezone.now() + timedelta(days=2))
        self.assertEqual((evidence["verified"], evidence["publishingVerified"]), (True, True))

    def test_a_sealed_target_is_not_renewed_without_its_full_check(self):
        from .website_verification import renew_build_proof
        from .website_sealed_workflow import REQUIRED_VERSION
        self.target.capabilities = {**self.target.capabilities, REQUIRED_VERSION: 3}
        self.target.save(update_fields=["capabilities"])
        self.assertIsNone(renew_build_proof(self.config, self.target, source_sha=MERGED))
        self.assertIsNone(renew_build_proof(self.config, self.target, source_sha="f" * 40))


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
