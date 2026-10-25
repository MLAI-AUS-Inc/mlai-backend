"""An unchanged site's proof is renewed before it expires, without asking its owner.

TalaThrive (25 Oct 2026): its branch was still at the commit verified on 17 Oct,
yet after seven days Articles said "Check your website integration again" and
every topic's Select was disabled until a person asked us to re-read the same
native CI check. Vyavos had the same expiry, and its live-route check was still
eight days old after that re-read, so publishing stayed blocked.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from . import website_freshness as freshness
from .models import OrganizationContentConfig
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget

SHA = "e" * 40
TARGET = "stack_native_collection_data_articles_json_static"


class WebsiteFreshnessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.user = get_user_model().objects.create_user(email="freshness@example.test")
        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.org = Organization.objects.create(domain="freshness.example.test", name="Synthetic freshness")
        VibeRaisingCompany.objects.create(profile=profile, organization=self.org, domain=self.org.domain, name="Synthetic")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo="example/site",
            default_publish_target_id=TARGET, pillar_strategy={})
        self.site(verified=self.now - timedelta(days=6), deployed=self.now - timedelta(days=6))

    def site(self, *, verified, deployed, origin="authenticated_ci", blockers=()):
        self.connection = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo=self.config.github_repo, repository_id=123, installation_id="42", branch="main", verified_sha=SHA,
            last_verified_at=verified, blockers=list(blockers), capabilities={"publishingReady": True, "generationReady": True})
        self.config.website_connection = self.connection
        self.config.save(update_fields=["website_connection"])
        setup = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=1,
            idempotency_key=f"{self.connection.pk}:setup", action="workflow", state="completed", payload={"attempt": 1})
        self.proof = {"website_connection_id": str(self.connection.pk), "connection_generation": 1, "repository_id": 123,
            "operation_id": str(setup.pk), "operation_attempt": 1, "deletion_epoch": 0, "github_repo": "example/site",
            "source_sha": SHA, "target_id": TARGET, "contract_digest": "c" * 64, "evidence_digest": "d" * 64,
            "schema_version": 2, "status": "verified"}
        self.target = WebsiteConnectionTarget.objects.create(connection=self.connection, target_key=TARGET, generation=1,
            adapter="stack_native", source_sha=SHA, verified_at=verified, capabilities={"publishingReady": True},
            contract={"contract_digest": "c" * 64, "verification": self.proof})
        self.saved = {**self.proof, "allowed": True, "attestation_origin": origin, "checked_at": verified.isoformat()}
        WebsiteConnectionOperation.objects.create(connection=self.connection, generation=1, action="ci-verify",
            idempotency_key=f"{self.connection.pk}:ci:{'d' * 64}", state="completed",
            payload={"operation_id": str(setup.pk), "evidence_digest": "d" * 64}, receipt=self.saved)
        WebsiteConnectionOperation.objects.create(connection=self.connection, generation=1, action="deployment-verify",
            idempotency_key=f"{self.connection.pk}:deployment", state="completed", payload={"source_sha": SHA, "target_id": TARGET},
            receipt={"status": "passed", "source_sha": SHA, "checked_at": deployed.isoformat()})

    def renew(self, **kwargs):
        renewed = {**self.saved, "checked_at": self.now.isoformat()}
        with patch("content_factory.website_verification.discover_source_attestation", return_value=self.proof) as discover, \
                patch("content_factory.website_verification.record_ci_attestation",
                    return_value=SimpleNamespace(receipt=renewed)) as record, \
                patch("content_factory.website_verification.verify_live_deployment") as live, \
                patch("content_factory.website_verification.current_ci_attestation", return_value=self.saved) as current:
            for name, value in kwargs.items():
                {"discover": discover, "record": record, "live": live, "current": current}[name].side_effect = value
            outcome = freshness.refresh(self.config, now=self.now)
        return outcome, discover, record, live, renewed

    def test_an_idle_site_rereads_its_native_check_and_live_route_before_expiry(self):
        outcome, discover, record, live, renewed = self.renew()

        self.assertEqual(outcome, {"outcome": "refreshed", "renewed": ["ci", "deployment"]})
        discover.assert_called_once_with(self.connection, self.target, SHA)
        record.assert_called_once_with(self.proof)
        live.assert_called_once_with(self.config, data=renewed)

    def test_recent_proof_is_left_alone(self):
        self.connection.delete()
        self.site(verified=self.now - timedelta(days=1), deployed=self.now - timedelta(days=1))

        outcome, discover, record, live, _ = self.renew()

        self.assertEqual(outcome, {"outcome": "fresh"})
        discover.assert_not_called()
        live.assert_not_called()

    def test_only_an_old_live_check_reuses_the_current_ci_receipt(self):
        self.connection.delete()
        self.site(verified=self.now - timedelta(days=1), deployed=self.now - timedelta(days=8))

        outcome, discover, _, live, _ = self.renew()

        self.assertEqual(outcome, {"outcome": "refreshed", "renewed": ["deployment"]})
        discover.assert_not_called()
        live.assert_called_once_with(self.config, data=self.saved)

    def test_a_changed_or_unverified_source_is_left_to_its_owner_flows(self):
        cases = {
            "changed": dict(blockers=[{"code": "repository_source_changed"}]),
            "hosted": dict(origin="authenticated_hosted"),
        }
        for name, changes in cases.items():
            with self.subTest(name):
                self.connection.delete()
                self.site(verified=self.now - timedelta(days=8), deployed=self.now - timedelta(days=8), **changes)

                outcome, discover, _, live, _ = self.renew()

                self.assertEqual(outcome, {"outcome": "not_renewable"})
                discover.assert_not_called()
                live.assert_not_called()
        self.target.source_sha = "f" * 40
        self.target.save(update_fields=["source_sha"])
        self.assertEqual(self.renew()[0], {"outcome": "not_renewable"})

    def test_a_provider_refusal_is_reported_not_raised(self):
        refused = WebsiteAuthorityError("website_source_changed", "The repository changed since this verification.")
        outcome = self.renew(record=refused)[0]
        self.assertEqual(outcome, {"outcome": "needs_owner", "reason": "website_source_changed", "renewed": []})

        unavailable = WebsiteAuthorityError("github_source_unavailable", "GitHub is unavailable.", retryable=True)
        outcome = self.renew(live=unavailable)[0]
        self.assertEqual(outcome, {"outcome": "unavailable", "reason": "github_source_unavailable", "renewed": ["ci"]})

        outcome = self.renew(discover=RuntimeError("socket closed"))[0]
        self.assertEqual(outcome["outcome"], "unavailable")

    def test_opening_articles_renews_once_per_retry_window(self):
        with patch.object(freshness, "refresh", return_value={"outcome": "refreshed"}) as refresh:
            self.assertEqual(freshness.refresh_for_page(self.org, now=self.now), {"outcome": "refreshed"})
            self.assertIsNone(freshness.refresh_for_page(self.org, now=self.now))
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs["trigger"], "page")

    def test_opening_articles_does_nothing_for_fresh_or_disabled_sites(self):
        with patch.object(freshness, "refresh") as refresh:
            with override_settings(WEBSITE_VERIFICATION_AUTO_REFRESH_ENABLED=False):
                self.assertIsNone(freshness.refresh_for_page(self.org, now=self.now))
            self.assertIsNone(freshness.refresh_for_page(self.org, now=self.now - timedelta(days=5)))
        refresh.assert_not_called()

    def test_the_sweep_renews_due_sites_and_throttles_itself(self):
        with patch.object(freshness, "refresh", return_value={"outcome": "refreshed"}) as refresh:
            self.assertEqual(freshness.run_verification_freshness_sweep(now=self.now),
                {"status": "completed", "examined": 1, "refreshed": 1, "needs_owner": 0, "unavailable": 0})
            self.assertEqual(freshness.run_verification_freshness_sweep(now=self.now), {"status": "skipped"})
            cache.delete("website-freshness:sweep")
            # The same site is not retried until its own retry window passes.
            self.assertEqual(freshness.run_verification_freshness_sweep(now=self.now)["examined"], 0)
        refresh.assert_called_once_with(self.config, now=self.now, trigger="sweep")
        with override_settings(WEBSITE_VERIFICATION_AUTO_REFRESH_ENABLED=False):
            self.assertEqual(freshness.run_verification_freshness_sweep(now=self.now), {"status": "disabled"})

    def test_a_site_needing_its_owner_does_not_fail_the_scheduler(self):
        from core.scheduling import runner_failed
        with patch.object(freshness, "refresh", return_value={"outcome": "needs_owner"}):
            result = freshness.run_verification_freshness_sweep(now=self.now)
        self.assertEqual(result["needs_owner"], 1)
        self.assertFalse(runner_failed(result))

    def test_bootstrap_renews_before_projecting_and_survives_a_renewal_error(self):
        from . import vibe_marketing_views as views
        company = SimpleNamespace(domain=self.org.domain)
        context = SimpleNamespace(organization=self.org)
        for failure in (None, RuntimeError("cache unavailable")):
            order = []

            def renew(organization):
                order.append("renew")
                if failure:
                    raise failure

            with self.subTest(failure=failure), \
                    patch.object(views, "_resolve_profile_company_or_response", return_value=(None, company, None)), \
                    patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
                    patch.object(views, "_serialize_bootstrap", side_effect=lambda *a, **k: order.append("project") or {"ok": True}), \
                    patch.object(views, "_timed_vibe_response", side_effect=lambda payload, **kwargs: payload), \
                    patch.object(freshness, "refresh_for_page", side_effect=renew):
                result = views.VibeMarketingBootstrapView().get(SimpleNamespace(query_params={}))
                self.assertEqual(result, {"ok": True})
                self.assertEqual(order, ["renew", "project"])
