"""DB/network-free regression coverage of article activation and paid research."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.response import Response

from content_factory.activation import article_capabilities, github_account_state, integration_evidence
from content_factory.billing import get_content_factory_research_cost_points
from content_factory import vibe_marketing_views as views


def config(**overrides):
    now = timezone.now()
    values = dict(github_repo="founder/site", connected_slack_user_id="founder-1",
                  github_installation_id="42", github_token_encrypted="synthetic-token",
                  github_refresh_token_encrypted="synthetic-refresh", github_user_name="founder",
                  github_token_expires_at=now - timedelta(days=2), github_connection_state="auth_required",
                  articles_scaffolded=False, article_system={}, last_scanned_sha="abc123", last_scanned_at=now,
                  publish_targets=[{"publish_capability": "direct", "kind": "react_article_system"}],
                  scan_summary={"github_repo": "founder/site", "default_branch": "main", "repo_head_sha": "abc123",
                                "article_system_readiness": {"ready": True, "safe_publish_route": True},
                                "publish_targets": [{"publish_capability": "direct", "kind": "react_article_system"}]},
                  pillar_strategy={})
    values.update(overrides)
    return SimpleNamespace(**values)


class ActivationEvidenceTests(SimpleTestCase):
    def test_expired_token_with_saved_refresh_is_checking(self):
        account = github_account_state(config(), actor_ids=["founder-1"])
        self.assertTrue(account["saved"])
        self.assertEqual(account["status"], "checking")
        capabilities = article_capabilities(config(), domain="example.com", account=account)
        self.assertFalse(capabilities["canGenerateArticle"])
        self.assertTrue(capabilities["canResearch"])

    def test_wrong_founder_does_not_inherit_authorization(self):
        account = github_account_state(config(), actor_ids=["another-founder"])
        self.assertFalse(account["saved"])
        self.assertFalse(account["owned"])

    def test_token_without_refresh_or_app_requires_attention(self):
        account = github_account_state(config(github_installation_id="", github_refresh_token_encrypted=""))
        self.assertEqual(account["status"], "needs_action")

    def test_registry_only_authorization_is_saved_without_claiming_repository_access(self):
        install = SimpleNamespace(installation_id="42", github_user_name="founder", account_login="founder")
        account = github_account_state(None, actor_ids=["founder-1"], installations=[install])
        self.assertTrue(account["saved"])
        self.assertEqual(account["status"], "checking")
        self.assertFalse(article_capabilities(None, account=account)["repositoryAccessVerified"])

    def test_history_scaffold_and_merge_cannot_grant_integration(self):
        cfg = config(articles_scaffolded=True, publish_targets=[], scan_summary={})
        with patch.object(views, "_article_generation_history_exists", side_effect=AssertionError("History must not be a permission")), \
             patch.object(views, "_github_repo_operable", return_value=True):
            readiness = views.compute_article_readiness(object(), cfg, setup_gate={"setupMerged": True, "generationReady": True})
        self.assertFalse(readiness["generation_ready"])

    def test_ready_requires_current_access_matching_repository_branch_and_head(self):
        cfg = config()
        evidence = integration_evidence(cfg)
        self.assertTrue(evidence["verified"])
        account = github_account_state(cfg, actor_ids=["founder-1"])
        access = {"verified": True, "branch": "main", "sha": "abc123"}
        ready = article_capabilities(cfg, domain="example.com", account=account, evidence=evidence, repository_access=access)
        self.assertTrue(ready["canGenerateArticle"])
        self.assertTrue(ready["canPublishArticle"])
        for bad_access in ({}, {**access, "sha": "new-head"}, {**access, "branch": "next"}):
            self.assertFalse(article_capabilities(cfg, account=account, evidence=evidence, repository_access=bad_access)["canGenerateArticle"])

    def test_changed_repository_stale_evidence_and_provisional_target_block(self):
        for cfg in (config(github_repo="founder/other"), config(last_scanned_at=timezone.now() - timedelta(days=8)),
                    config(publish_targets=[{"publish_capability": "direct", "provisional": True}]),
                    config(publish_targets=[{"publish_capability": "direct", "seed_detail_proof": {"status": "structural"}}]),
                    config(article_system={"publish_disconnected_at": timezone.now().isoformat()})):
            self.assertFalse(integration_evidence(cfg)["verified"])

    def test_stories_route_is_preserved(self):
        cfg = config(article_system={"route_path": "/stories/"})
        capabilities = article_capabilities(cfg, domain="talathrive.com")
        self.assertEqual(capabilities["routePath"], "/stories")
        self.assertEqual(capabilities["surfaceLabel"], "Stories")

    def test_unmerged_and_merged_unverified_setup_remain_blocked(self):
        cfg = config()
        self.assertFalse(integration_evidence(cfg, setup_gate={"setupBlocked": True})["verified"])
        self.assertFalse(integration_evidence(cfg, setup_gate={"setupMerged": True, "published": False})["verified"])


class ActivationAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=1, id=1, email="synthetic@example.test")
        self.context = SimpleNamespace(organization=SimpleNamespace(id=1, pk=1, domain="example.test"),
                                      profile=SimpleNamespace(user=self.user), company=SimpleNamespace(pk="company-1"))
        self.request = SimpleNamespace(data={}, user=self.user)

    def test_article_is_rejected_before_billing_or_dispatch(self):
        blocked = Response({"reasonCode": "integration_required"}, status=409)
        with patch.object(views, "_resolve_context_or_response", return_value=(self.context, None)), \
             patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=blocked), \
             patch.object(views, "_charge_roo_points_for_article") as billing, \
             patch.object(views, "_queue_content_factory_run") as queue:
            # This fixture isolates activation; the combined consent wrapper is
            # exercised against real rows in tests_activation_connections.
            response = views.VibeMarketingArticleView.post.__wrapped__(views.VibeMarketingArticleView(), self.request)
        self.assertEqual(response.status_code, 409)
        billing.assert_not_called()
        queue.assert_not_called()

    def test_all_article_control_mutations_reject_before_remote_work(self):
        run = SimpleNamespace(workflow="article_generation", github_repo="founder/site")
        for action in ("restart", "resume", "revise", "regenerate-image", "regenerate-images", "publish-pr", "promote-bundle", "approve", "merge-publish-pr", "retry-preview-quality"):
            with self.subTest(action=action), \
                 patch.object(views, "_resolve_context_or_response", return_value=(self.context, None)), \
                 patch.object(views, "get_object_or_404", return_value=run), \
                 patch.object(views, "_run_belongs_to_context", return_value=True), \
                 patch.object(views, "_get_config", return_value=config()), \
                 patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({}, status=409)), \
                 patch.object(views, "_call_content_factory_run_action") as remote, \
                 patch.object(views, "_restart_article_run") as restart:
                response = views.VibeMarketingRunControlView.post.__wrapped__(views.VibeMarketingRunControlView(), self.request, "run-1", action)
                self.assertEqual(response.status_code, 409)
                remote.assert_not_called()
                restart.assert_not_called()

    def test_comment_revision_blocks_before_comment_mutation(self):
        with patch.object(views.VibeMarketingRunCommentsSubmitView, "_resolve_run", return_value=(self.context, object(), None)), \
             patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({}, status=409)), \
             patch.object(views, "_call_content_factory_component_revision") as remote:
            response = views.VibeMarketingRunCommentsSubmitView.post.__wrapped__(views.VibeMarketingRunCommentsSubmitView(), self.request, "run-1")
        self.assertEqual(response.status_code, 409)
        remote.assert_not_called()

    def test_all_research_entry_points_charge_and_queue_before_setup(self):
        cfg = config(github_repo="", publish_targets=[], scan_summary={})
        for data in ({}, {"customTopicTitle": "Culture"}, {"contentIslandSlug": "culture"}):
            request = SimpleNamespace(data=data, user=self.user)
            run = SimpleNamespace(run_id="research-1", status="queued")
            with self.subTest(data=data), \
                 patch("integrations.services.daily_research_policy.record_engagement"), \
                 patch.object(views, "_resolve_context_or_response", return_value=(self.context, None)), \
                 patch.object(views, "_get_config", return_value=cfg), \
                 patch.object(views, "founder_actor_id_for_user", return_value="founder-1"), \
                 patch("content_factory.editorial_catalog.discovery_audience_context", return_value=None), \
                 patch("content_factory.custom_islands.resolve_island_discovery_scope", return_value={"name": "Culture", "keyword": "culture", "icon_key": "default", "color_key": "green", "context": {}}), \
                 patch.object(views, "_charge_roo_points_for_content_island_topic_generation", return_value=(self.user, {"client_request_id": "stable-id"}, None)) as charge, \
                 patch.object(views, "_queue_content_factory_run", return_value=run) as queue, \
                 patch.object(views, "_run_start_payload", return_value={"runId": "research-1"}), \
                 patch.object(views, "_setup_blocked_response_for_generation", side_effect=AssertionError("Research must not require setup")):
                response = views.VibeMarketingDiscoveryView().post(request)
                self.assertEqual(response.status_code, 202)
                charge.assert_called_once()
                self.assertEqual(charge.call_args.kwargs["payload"]["requested_topic_count"], 4)
                self.assertIsNotNone(queue.call_args.kwargs["billing_refund_context"])

    def test_changed_quote_is_rejected_before_charge(self):
        request = SimpleNamespace(data={"expectedCostPoints": 1}, user=self.user)
        with patch.object(views, "charge_content_factory_topic_generation_for_user") as spend:
            response = views._charge_roo_points_for_content_island_topic_generation(request, context=self.context, payload={"requested_topic_count": 4})[-1]
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["costPoints"], 4)
        spend.assert_not_called()

    def test_research_retry_uses_same_scoped_ledger_and_dispatch_key(self):
        request = SimpleNamespace(data={"clientRequestId": "retry-1", "expectedCostPoints": 4}, user=self.user)
        charged = Mock(return_value=(self.user, SimpleNamespace(id=12), 4))
        payloads = [{"requested_topic_count": 4}, {"requested_topic_count": 4}]
        with patch.object(views, "charge_content_factory_topic_generation_for_user", charged), \
             patch.object(views, "founder_actor_id_for_user", return_value="founder-1"), \
             patch.object(views, "_roo_points_balance_for_user", return_value=10):
            for payload in payloads:
                self.assertIsNone(views._charge_roo_points_for_content_island_topic_generation(request, context=self.context, payload=payload)[-1])
        self.assertEqual(payloads[0]["client_request_id"], payloads[1]["client_request_id"])
        self.assertEqual(payloads[0]["roo_points_cost"], 4)
        self.assertLessEqual(len(payloads[0]["client_request_id"]), 100)
        self.assertEqual(get_content_factory_research_cost_points("example.test", 8), 8)
        self.assertEqual(get_content_factory_research_cost_points("mlai.au", 8), 0)


class ActivationScopeAndProbeTests(SimpleTestCase):
    def setUp(self):
        self.cfg = config()
        self.context = SimpleNamespace(profile=SimpleNamespace(user=SimpleNamespace(pk=1)),
                                       organization=SimpleNamespace(pk=2, domain="example.test"))

    def test_new_scan_cannot_reuse_old_raw_or_summary_readiness(self):
        cfg = config(github_repo="founder/new", article_system={
            "readiness": {"ready": True},
            "scan": {"github_repo": "founder/new", "default_branch": "main", "repo_head_sha": "abc123",
                     "completed_at": timezone.now().isoformat()}},
            scan_summary={"github_repo": "founder/site", "article_system_readiness": {"ready": True}})
        self.assertFalse(integration_evidence(cfg)["verified"])
        new_scan = SimpleNamespace(workflow="repo_scan", status="completed", github_repo="founder/new",
            run_request={}, updated_at=timezone.now(), result={"github_repo": "founder/new",
            "default_branch": "main", "repo_head_sha": "abc123", "publish_targets": cfg.publish_targets})
        self.assertFalse(integration_evidence(cfg, [new_scan])["verified"])

    def test_target_must_belong_to_the_same_verified_scan(self):
        cfg = config(scan_summary={**self.cfg.scan_summary, "publish_targets": []})
        self.assertEqual(integration_evidence(cfg)["reasonCode"], "verification_required")
        cfg = config(publish_targets=[{"publish_capability": "direct", "kind": "different_registry"}])
        self.assertFalse(integration_evidence(cfg)["verified"])

    def test_merge_timestamp_is_compared_as_an_instant(self):
        from datetime import timezone as tz, timedelta as delta
        # An offset date can sort before UTC lexically while occurring later.
        now = timezone.now()
        older = now - delta(hours=2)
        cfg = config(last_scanned_at=older)
        merged = (now - delta(hours=1)).astimezone(tz(delta(hours=-10))).isoformat()
        self.assertFalse(integration_evidence(cfg, setup_gate={"setupMerged": True,
            "published": True, "mergedAt": merged})["verified"])

    def probe(self, responses, *, source="github_oauth_user_token"):
        store = Mock()
        store.get.return_value = None
        with patch.object(views, "cache", store), \
             patch.object(views, "_github_account_for_context", return_value={"owned": True}), \
             patch.object(views, "_github_token_for_repo_operation", return_value=("synthetic-only", source)) as token, \
             patch.object(views.http_client, "get", side_effect=responses) as network:
            result = views._verify_github_repository_access(self.context, self.cfg, force=True)
        token.assert_called_once_with(domain="example.test", github_repo="founder/site", permission_mode="write")
        return result, network, store

    def test_repository_probe_verifies_real_push_permission_branch_and_head(self):
        result, network, store = self.probe([
            Mock(status_code=200, json=Mock(return_value={"full_name": "founder/site", "default_branch": "main", "permissions": {"push": True}})),
            Mock(status_code=200, json=Mock(return_value={"sha": "abc123"}))])
        self.assertTrue(result["verified"])
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["sha"], "abc123")
        self.assertEqual(network.call_count, 2)
        self.assertNotIn("synthetic-only", repr(store.set.call_args))

    def test_revoked_wrong_or_read_only_repository_is_not_ready(self):
        for response in (Mock(status_code=401), Mock(status_code=404),
                         Mock(status_code=200, json=Mock(return_value={"full_name": "founder/other", "default_branch": "main", "permissions": {"push": True}})),
                         Mock(status_code=200, json=Mock(return_value={"full_name": "founder/site", "default_branch": "main", "permissions": {"push": False}}))):
            with self.subTest(response=response):
                result, network, _ = self.probe([response])
                self.assertFalse(result["verified"])
                self.assertEqual(result["reasonCode"], "github_access_required")
                self.assertEqual(network.call_count, 1)

    def test_outage_and_revoked_access_are_actionable_even_if_setup_missing(self):
        result, _, _ = self.probe([views.http_client.RequestException("synthetic outage")])
        self.assertFalse(result["verified"])
        account = github_account_state(self.cfg)
        for access, code, stage in ((result, "github_unavailable", "unavailable"),
            ({"verified": False, "reasonCode": "github_access_required"}, "github_access_required", "github")):
            caps = article_capabilities(self.cfg, account=account, evidence={"verified": False,
                "reasonCode": "integration_required"}, repository_access=access)
            self.assertEqual(caps["reasonCode"], code)
            self.assertEqual(caps["stage"], stage)

    def test_research_workflow_is_remote_and_idempotently_dispatchable(self):
        self.assertIn("island_refresh", views.VIBE_MARKETING_WORKFLOWS)
        self.assertIn("island_refresh", views.REMOTE_REQUIRED_WORKFLOWS)
        self.assertIn("island_refresh", views.CONTENT_FACTORY_KEYED_DISPATCH_WORKFLOWS)
        self.assertIn("island-research", views.CONTENT_FACTORY_KEYED_DISPATCH_ENDPOINTS)
        result = views._run_result_from_remote({"result": {"message": "Complete"},
            "island_research": True, "suggested_islands": [{"id": "proposal"}]})
        self.assertTrue(result["island_research"])
        self.assertEqual(result["suggested_islands"], [{"id": "proposal"}])


class ActivationRequestAndLegacyTests(SimpleTestCase):
    def test_real_drf_request_rejects_article_before_ledger_or_worker(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from rest_framework.permissions import IsAuthenticated
        user = SimpleNamespace(pk=1, is_authenticated=True)
        request = APIRequestFactory().post("/api/v1/vibe-marketing/article", {"topic": "Culture", "expectedCostPoints": 6}, format="json")
        force_authenticate(request, user=user)
        context = SimpleNamespace(organization=SimpleNamespace(domain="example.test"))
        with patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
             patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({"code": "article_system_setup_blocked"}, status=409)), \
             patch.object(views, "_charge_roo_points_for_article") as charge, \
             patch.object(views, "_queue_content_factory_run") as queue, \
             patch.object(views.VibeMarketingArticleView, "post", views.VibeMarketingArticleView.post.__wrapped__):
            response = views.VibeMarketingArticleView.as_view(authentication_classes=[], permission_classes=[IsAuthenticated])(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "article_system_setup_blocked")
        charge.assert_not_called()
        queue.assert_not_called()

    def test_unauthenticated_request_stops_before_context_resolution(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework.permissions import IsAuthenticated
        request = APIRequestFactory().post("/api/v1/vibe-marketing/article", {}, format="json")
        with patch.object(views, "_resolve_context_or_response") as resolver:
            response = views.VibeMarketingArticleView.as_view(authentication_classes=[], permission_classes=[IsAuthenticated])(request)
        self.assertEqual(response.status_code, 403)
        resolver.assert_not_called()

    def test_legacy_article_charge_uses_gate_before_ledger_query(self):
        from integrations.services import article_generation as service
        error = service.ArticleGenerationError("Finish setup")
        with patch.object(service, "require_article_activation", side_effect=error), \
             patch("roo.services.PointsService.spend") as spend, \
             patch.object(service.ContentFactoryJob.objects, "filter") as jobs:
            with self.assertRaises(service.ArticleGenerationError):
                service._charge_content_factory_user(user=SimpleNamespace(pk=1), created_by_slack_id="founder-1",
                    article_request={"client_request_id": "attempt"}, resolved_domain="example.test")
        spend.assert_not_called()
        jobs.assert_not_called()

    def test_legacy_topic_charge_checks_quote_before_ledger_write(self):
        from integrations.services import article_generation as service
        with patch.object(service, "_validate_authenticated_content_factory_actor"), \
             patch("roo.services.PointsService.spend") as spend, \
             patch("roo.models.Ledger.objects.filter") as ledger:
            for value in (1, 4.0, "4", True):
                with self.subTest(value=value), self.assertRaises(service.ArticleGenerationError) as failure:
                    service.charge_content_factory_topic_generation_for_user(user=SimpleNamespace(pk=1), actor_id="founder-1",
                        article_request={"client_request_id": "attempt", "requested_topic_count": 4, "expectedCostPoints": value},
                        resolved_domain="example.test")
                self.assertEqual(failure.exception.payload["code"], "roo_points_quote_changed")
        spend.assert_not_called()
        ledger.assert_not_called()

    def test_empty_research_refund_matches_current_points_service_signature(self):
        import inspect
        from content_factory.island_research import refund_empty_or_failed_research
        from roo.services import PointsService
        payer = SimpleNamespace(pk=1)
        charge = SimpleNamespace(idempotency_key="content_factory:topic_generation:charge:retry", user=payer, delta=-1, source="CONTENT_FACTORY", created_by_slack_id="founder-1", reference_id="retry")
        run = SimpleNamespace(status="completed", domain="example.test", run_request={"island_research_brief": {"subject": "Culture"},
            "client_request_id": "retry"}, result={"island_research": True, "suggested_islands": []}, save=Mock())
        with patch("roo.models.Ledger.objects.select_related") as ledger, patch.object(PointsService, "refund", autospec=True) as refund:
            ledger.return_value.filter.return_value.first.return_value = charge
            refund_empty_or_failed_research(run)
            refund_empty_or_failed_research(run)
        refund.assert_called_once()
        self.assertEqual(refund.call_args.kwargs["delta"], 1)
        self.assertEqual(refund.call_args.kwargs["idempotency_key"], "content_factory:topic_generation:refund:retry")


class PulseSourceAdmissionTests(SimpleTestCase):
    def setUp(self):
        from community_chat.startups.views import GenerateView
        self.view = GenerateView()
        self.view.company = SimpleNamespace(organization=SimpleNamespace(pk=2))
        self.user = SimpleNamespace(pk=1)

    def test_selected_disconnected_or_unconfigured_source_blocks_before_shared_generation(self):
        from vibe_raising.views import VibeRaisingEmailDraftStartView
        for row in ({"key": "gmail", "status": "reauth_required", "selected": False},
                    {"key": "google_analytics", "status": "connected", "selected": False, "usableForUpdates": False},
                    {"key": "gmail", "status": "syncing", "selected": True},
                    {"key": "gmail", "status": "connected", "selected": True, "available": False},
                    {"key": "gmail", "status": "connected", "selected": True, "usableForUpdates": False}):
            request = SimpleNamespace(user=self.user, data={"inputSources": [row["key"]], "manualSummary": "Founder notes"})
            with self.subTest(row=row), patch("integrations.services.external_connectors.serialize_source_status", return_value={"sources": [row]}), \
                 patch.object(VibeRaisingEmailDraftStartView, "post") as generate:
                response = self.view.post(request)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["unavailableSources"], [row["key"]])
                generate.assert_not_called()

    def test_explicit_source_exclusion_preserves_notes_only_and_usable_sources(self):
        from vibe_raising.views import VibeRaisingEmailDraftStartView
        for data in ({"inputSources": [], "manualSummary": "Founder notes"},
                     {"inputSources": ["gmail"], "manualSummary": "Founder notes"}):
            request = SimpleNamespace(user=self.user, data=data)
            with self.subTest(data=data), patch("integrations.services.external_connectors.serialize_source_status", return_value={"sources": [
                    {"key": "gmail", "status": "connected", "selected": True, "enabled": False}]}), \
                 patch.object(VibeRaisingEmailDraftStartView, "post", return_value=Response({}, status=202)) as generate:
                self.assertEqual(self.view.post(request).status_code, 202)
                generate.assert_called_once_with(request)


class AutomaticContinuationAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.context = SimpleNamespace(organization=SimpleNamespace(pk=2, domain="example.test"),
            profile=SimpleNamespace(user=SimpleNamespace(pk=1)))
        self.run = SimpleNamespace(run_id="setup-1", workflow="article_system_setup", github_repo="founder/site",
            slack_user_id="founder-1", result={}, run_request={"blocked_article_run_ids": ["old-article"]})

    def test_unverified_merge_cannot_dispatch_historical_parent_or_charge(self):
        with patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({
                 "reasonCode": "verification_required", "detail": "Verify your articles integration."}, status=409)), \
             patch.object(views, "_persist_setup_merged_verification", return_value=self.run) as persist, \
             patch.object(views, "_call_content_factory_run_action") as remote, \
             patch.object(views, "_charge_roo_points_for_article") as charge:
            result = views._maybe_verify_merged_setup_for_blocked_articles(run=self.run, context=self.context)
        self.assertIs(result, self.run)
        self.assertEqual(persist.call_args.args[1]["status"], "verification_required")
        self.assertEqual(persist.call_args.args[1]["nextRequiredStep"], "articles")
        remote.assert_not_called()
        charge.assert_not_called()

    def test_verified_current_integration_allows_parent_continuation(self):
        with patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=None), \
             patch.object(views, "founder_actor_id_for_user", return_value="founder-1"), \
             patch.object(views, "_persist_setup_merged_verification", return_value=self.run) as persist, \
             patch.object(views, "_call_content_factory_run_action", return_value={"status": "queued"}) as remote:
            result = views._maybe_verify_merged_setup_for_blocked_articles(run=self.run, context=self.context)
        self.assertIs(result, self.run)
        self.assertEqual(remote.call_args.kwargs["action"], "verify-merged-setup")
        self.assertEqual(remote.call_args.kwargs["payload"]["blocked_article_run_ids"], ["old-article"])
        self.assertTrue(persist.call_args.args[1]["accepted"])

    def test_poll_auto_merge_stops_before_github_mutation(self):
        with patch.object(views, "_get_config", return_value=config()), \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({"detail": "Finish setup."}, status=409)), \
             patch.object(views, "_github_token_for_repo_operation") as token, \
             patch.object(views, "_github_pull_checks_state") as github:
            result = views._check_and_merge_publish_pr(run=self.run, context=self.context)
        self.assertEqual(result["outcome"], "error")
        token.assert_not_called()
        github.assert_not_called()


class WebsiteSourceStatusTests(SimpleTestCase):
    def test_saved_expired_github_survives_disabled_oauth_and_is_not_a_pulse_source(self):
        from community_chat.startups import website_connections as sources
        company = SimpleNamespace(organization=SimpleNamespace(pk=2))
        user = SimpleNamespace(pk=1)
        with patch.object(sources.OrganizationContentConfig.objects, "filter") as configs, \
             patch.object(sources, "google_connection_for_org", return_value=None), \
             patch.object(sources, "is_provider_configured", return_value=False), \
             patch.object(sources, "actor_ids_for_user", return_value=["founder-1"]), \
             patch.object(sources, "user_github_installations", return_value=[]), \
             self.settings(GITHUB_OAUTH_CLIENT_ID="", GITHUB_OAUTH_CLIENT_SECRET=""):
            configs.return_value.first.return_value = config()
            row = next(row for row in sources.website_connection_sources(user, company) if row["key"] == "github")
        self.assertEqual(row["status"], "checking")
        self.assertEqual(row["accountLabel"], "founder")
        self.assertTrue(row["repositorySelected"])
        self.assertFalse(row["repositoryAccessVerified"])
        self.assertFalse(row["usableForUpdates"])
        self.assertFalse(row["selected"])


class MergedSetupInventoryVerificationTests(SimpleTestCase):
    def test_inventory_scan_pins_merged_setup_without_overwriting_selected_stories_route(self):
        cfg = config(article_system={"route_path": "/stories", "pending_article_system_setup": {
            "status": "merged", "mergeStatus": "merged", "setupRunId": "setup-1", "routePath": "/stories"}})
        cfg.save = Mock()
        self.assertTrue(views._pin_merged_setup_verification_scan(cfg, SimpleNamespace(run_id="verification-1")))
        pending = cfg.article_system["pending_article_system_setup"]
        self.assertEqual(pending["rescanRunId"], "verification-1")
        self.assertEqual(pending["setupRunId"], "setup-1")
        self.assertEqual(pending["routePath"], "/stories")
        self.assertEqual(pending["mergeStatus"], "merged")
        cfg.save.assert_called_once_with(update_fields=["article_system", "updated_at"])

    def test_inventory_scan_does_not_grant_merge_or_pin_unmerged_setup(self):
        cfg = config(article_system={"pending_article_system_setup": {"status": "pr_created", "setupRunId": "setup-1"}})
        cfg.save = Mock()
        self.assertFalse(views._pin_merged_setup_verification_scan(cfg, SimpleNamespace(run_id="scan-1")))
        cfg.save.assert_not_called()
        self.assertNotIn("rescanRunId", cfg.article_system["pending_article_system_setup"])

    def test_real_url_configuration_resolves_existing_research_and_verification_routes(self):
        from django.urls import resolve
        self.assertIs(resolve("/api/v1/vibe-marketing/scan/").func.view_class, views.VibeMarketingScanView)
        from content_factory.island_research_views import ContentIslandResearchView
        self.assertIs(resolve("/api/v1/vibe-marketing/islands/research").func.view_class, ContentIslandResearchView)


class SelectedPublishTargetTests(SimpleTestCase):
    def test_default_target_cannot_borrow_another_targets_safe_proof(self):
        cfg = config(default_publish_target_id="blocked-target", publish_targets=[
            {"target_id": "safe-target", "publish_capability": "direct"},
            {"target_id": "blocked-target", "publish_capability": "bundle_only"}])
        self.assertFalse(integration_evidence(cfg)["verified"])

    def test_surface_label_uses_the_verified_targets_actual_route(self):
        cfg = config(publish_targets=[{"publish_capability": "direct", "route_template": "/stories/{category}/{slug}"}])
        caps = article_capabilities(cfg)
        self.assertEqual(caps["routePath"], "/stories")
        self.assertEqual(caps["surfaceLabel"], "Stories")
