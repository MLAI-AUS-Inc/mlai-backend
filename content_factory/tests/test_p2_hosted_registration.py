"""Hosted fallback uses explicit source proof, consent, own-worker activation and no repository publish."""
from types import SimpleNamespace
from unittest.mock import patch, Mock
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from content_factory import hosted_auto_register as auto
from content_factory.hosted_article_models import HostedArticleSite
from content_factory.self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_named_legacy_org, fake_provider
from content_factory.website_models import WebsiteConnection, WebsiteScanSnapshot
from workflow_runs.models import ContentFactoryRun


@override_settings(**ALL_FLAGS_ON, ARTICLES_HOSTED_SITES_ENABLED=True)
class HostedRegistrationTests(TransactionTestCase):
    def setUp(self):
        self.built = make_p2_org()
        self.org = self.built.organization
        self.site = WebsiteConnection.objects.create(organization=self.org, repository_id=123,
            installation_id="45", github_repo="synthetic/site", branch="main", site_url="https://"+self.org.domain,
            verified_sha="a"*40, capabilities={"publishingReady": False})
        self.built.config.website_connection = self.site
        self.built.config.save(update_fields=["website_connection"])
        self.scan = WebsiteScanSnapshot.objects.create(connection=self.site, generation=1, run_id="scan",
            source_sha="a"*40, fingerprint="b"*64, detector_version="fixture",
            evidence={"scan_complete": True, "repo_head_sha": "a"*40, "detected_framework": "nextjs"})
        self.failure = ContentFactoryRun.objects.create(run_id="native-failed", organization=self.org, domain=self.org.domain,
            workflow="article_system_setup", status="failed", result={"self_heal_v1": {"mode": "policy", "source_sha": "a"*40,
                "rung": "deterministic", "attempts": 1, "code": "ADOPTED_DIRECTORY_BUILD_UNVERIFIED"}})
        self.allow = override_settings(ARTICLES_HOSTED_RUNG_ORGANIZATIONS=str(self.org.pk))
        self.allow.enable(); self.addCleanup(self.allow.disable)

    def test_register_without_admin_then_proxy_only_after_slot_and_activate_after_exact_owner_merge(self):
        calls = []
        def remote(path, payload):
            calls.append((path, payload))
            return {"run_id": payload.get("run_id"), "status": "queued"}
        with patch.object(auto, "_remote", side_effect=remote):
            self.assertTrue(auto.hosted_rung_ready(self.org))
            first = auto.run_hosted_sweep()
            self.assertEqual(first["registered"], 1)
            site = HostedArticleSite.objects.get(organization=self.org)
            self.assertEqual(site.state, "draft")
            self.assertEqual(site.profile["build"]["adapter"], auto.ADAPTER)
            self.assertEqual(site.profile["source_recipe"]["digest"], auto.RECIPE_DIGEST)
            self.assertEqual(calls[0][0], "/api/hosted-sites/sync")
            site.worker_url = "https://mlai-test.owned.workers.dev"
            site.save(update_fields=["worker_url"])
            claim = Mock(return_value=False)
            with fake_provider("content_factory.failure_policy_engine", claim_pull_request_slot=claim):
                self.assertEqual(auto.run_hosted_sweep()["proxy_requested"], 0)
                self.assertEqual(len(calls), 1)
                claim.return_value = True
                self.assertEqual(auto.run_hosted_sweep()["proxy_requested"], 1)
            site.refresh_from_db()
            run = ContentFactoryRun.objects.get(run_id=site.profile["auto"]["proxy_run_id"])
            self.assertEqual(run.run_request["merge_policy"], "backend_worker")
            self.assertFalse(auto.activate(site))
            # Deployment content alone, without owner click, is never enough.
            run.result = {"merge_v1": {"merge_sha": "e"*40, "head_sha": "c"*40, "approved_by_user_id": self.built.owner.pk}}
            run.save(update_fields=["result"])
            self.assertFalse(auto.activate(site))
            run.result["owner_merge_approval_v1"] = {"kind": "proxy", "head_sha": "c"*40,
                "approved_at": timezone.now().isoformat(), "approved_by_user_id": self.built.owner.pk}
            run.save(update_fields=["result"])
            with patch.object(auto, "fetch_live_route", side_effect=[(b"same blog", {"x-mlai-hosted": "1"}), (b"same blog", {})]) as fetch:
                self.assertTrue(auto.activate(site))
                self.assertEqual(fetch.call_args_list[0].args, ("https://"+site.domain+"/blog/", site.domain))
                self.assertEqual(fetch.call_args_list[1].args, (site.worker_url+"/blog/", "mlai-test.owned.workers.dev"))
            site.refresh_from_db()
            self.assertEqual(site.state, "active")
            self.assertIsNotNone(site.proxy_verified_at)

    def test_registered_proxy_real_owner_click_and_real_merge_worker_lead_to_activation(self):
        from content_factory.tests_p2_merge_worker import MergeWorkerTests
        from content_factory import publish_merge_worker as worker, vibe_marketing_views as views
        fixture = MergeWorkerTests("test_proxy_owner_approve_works_after_cf_renames_workflow_and_only_worker_merges")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        WebsiteScanSnapshot.objects.create(connection=fixture.website, generation=1, run_id="sync-scan",
            source_sha="a"*40, fingerprint="b"*64, detector_version="fixture",
            evidence={"scan_complete": True, "repo_head_sha": "a"*40, "detected_framework": "nextjs"})
        ContentFactoryRun.objects.create(run_id="sync-native-failed", organization=fixture.org, domain=fixture.org.domain,
            workflow="article_system_setup", status="failed", result={"self_heal_v1": {"mode": "policy", "source_sha": "a"*40,
                "rung": "deterministic", "attempts": 1, "code": "ADOPTED_DIRECTORY_BUILD_UNVERIFIED"}})
        with override_settings(ARTICLES_HOSTED_RUNG_ORGANIZATIONS=str(fixture.org.pk)), patch.object(auto, "_remote", return_value={"status": "queued"}):
            auto.run_hosted_sweep()
            site = HostedArticleSite.objects.get(organization=fixture.org)
            site.worker_url = "https://sync.owned.workers.dev"
            site.save(update_fields=["worker_url"])
            with fake_provider("content_factory.failure_policy_engine", claim_pull_request_slot=lambda *a, **k: True):
                auto.run_hosted_sweep()
            site.refresh_from_db()
            run = ContentFactoryRun.objects.get(run_id=site.profile["auto"]["proxy_run_id"])
            observed = fixture.run_fixture(kind="proxy", approved=False)
            run.result = observed.result
            run.workflow = "hosted_native_verification"
            run.save(update_fields=["result", "workflow"])
            observed.delete()
            context = SimpleNamespace(organization=fixture.org, profile=fixture.fixture.company.profile)
            with fixture.providers():
                self.assertEqual(worker.run_merge_sweep()["merged"], 0)
                with patch.object(views, "_serialize_run", return_value={"runId": run.run_id}):
                    self.assertEqual(views.control_article_run(user=fixture.owner, context=context, run=run,
                        action="approve", data={"merge": "auto"}).status_code, 202)
                self.assertEqual(worker.run_merge_sweep()["merged"], 1)
            with patch.object(auto, "fetch_live_route", side_effect=[(b"verified blog", {"x-mlai-hosted": "1"}), (b"verified blog", {})]):
                self.assertTrue(auto.activate(site))
            site.refresh_from_db()
            self.assertEqual(site.state, "active")

    def test_not_registered_before_native_failure_or_when_allowlist_or_flags_or_source_mismatch(self):
        with override_settings(ARTICLES_HOSTED_RUNG_ORGANIZATIONS=""):
            self.assertFalse(auto.hosted_rung_ready(self.org))
            self.assertEqual(auto.run_hosted_sweep()["registered"], 0)
        self.failure.result["self_heal_v1"]["source_sha"] = "d"*40
        self.failure.save(update_fields=["result"])
        self.assertFalse(auto.hosted_rung_ready(self.org))
        with patch.object(auto, "_remote") as remote:
            self.assertEqual(auto.run_hosted_sweep()["registered"], 0)
            remote.assert_not_called()
        self.assertFalse(HostedArticleSite.objects.exists())
        with override_settings(ARTICLES_HOSTED_AUTO_REGISTER_ENABLED=False):
            self.assertEqual(auto.run_hosted_sweep(), {"status": "disabled"})

    def test_transient_provider_failure_does_not_open_the_hosted_rung(self):
        self.failure.result["self_heal_v1"]["code"] = "GITHUB_TEMPORARILY_UNAVAILABLE"
        self.failure.save(update_fields=["result"])
        self.assertFalse(auto.hosted_rung_ready(self.org))
        self.assertEqual(auto.run_hosted_sweep()["registered"], 0)

    def test_unsupported_platform_domain_claim_and_existing_pawshare_are_unchanged(self):
        self.scan.evidence["detected_framework"] = "wix"
        self.scan.save(update_fields=["evidence"])
        self.assertFalse(auto.hosted_rung_ready(self.org))
        legacy = make_named_legacy_org("pawshare")
        self.assertFalse(auto.hosted_rung_ready(legacy.organization))
        existing = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="paused",
            profile={"existing": "unchanged"}, worker_url="https://old.owned.workers.dev")
        before = dict(HostedArticleSite.objects.filter(pk=existing.pk).values().get())
        auto.run_hosted_sweep()
        self.assertEqual(HostedArticleSite.objects.filter(pk=existing.pk).values().get(), before)

    def test_another_startup_domain_claim_is_never_registered(self):
        make_p2_org(domain="sub."+self.org.domain)
        with patch.object(auto, "_remote") as remote:
            self.assertEqual(auto.run_hosted_sweep()["registered"], 0)
            remote.assert_not_called()
        self.assertFalse(HostedArticleSite.objects.filter(organization=self.org).exists())

    def test_sync_transport_loss_uses_persisted_backoff_and_bounded_attempts(self):
        from datetime import timedelta
        now = timezone.now()
        with patch.object(auto, "_remote", return_value=None) as remote:
            auto.run_hosted_sweep(now=now)
            auto.run_hosted_sweep(now=now)
            self.assertEqual(remote.call_count, 1)
            auto.run_hosted_sweep(now=now+timedelta(minutes=11))
            self.assertEqual(remote.call_count, 2)
        site = HostedArticleSite.objects.get(organization=self.org)
        self.assertEqual(site.profile["auto"]["build_attempts"], 2)

    def test_another_worker_body_or_arbitrary_worker_url_never_activates(self):
        site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, worker_url="https://mine.owned.workers.dev",
            profile={"auto": {"proxy_run_id": "proxy"}})
        ContentFactoryRun.objects.create(organization=self.org, run_id="proxy", workflow="article_system_setup", result={
            "merge_v1": {"merge_sha": "e"*40, "head_sha": "c"*40, "approved_by_user_id": self.built.owner.pk},
            "owner_merge_approval_v1": {"kind": "proxy", "head_sha": "c"*40, "approved_at": timezone.now().isoformat(), "approved_by_user_id": self.built.owner.pk}})
        with patch.object(auto, "fetch_live_route", side_effect=[(b"other site", {"x-mlai-hosted": "1"}), (b"my site", {})]):
            self.assertFalse(auto.activate(site))
        site.worker_url = "https://internal.example.test"
        with patch.object(auto, "fetch_live_route") as fetch:
            self.assertFalse(auto.activate(site))
            fetch.assert_not_called()
