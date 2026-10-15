"""Database/network-free checks for observation-only bootstrap projections."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
from django.apps import apps
from django.core.cache import cache
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import vibe_marketing_views as views
from content_factory.website_github_access import verify_account_access


class BootstrapReadPathTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.user = SimpleNamespace(pk=702)
        self.website = SimpleNamespace(pk="connection-1", generation=1, repository_id=42,
            installation_id="45", state="connected", configuration_version=1,
            verified_sha="a" * 40, branch="main", authorized_by_id=self.user.pk)
        self.config = SimpleNamespace(website_connection=self.website, github_repo="owner/site",
            connected_slack_user_id="user-702", github_installation_id="45",
            github_token_encrypted="", github_token_expires_at=None)

    def context(self):
        return SimpleNamespace(organization=SimpleNamespace(pk=701, domain="example.test"), profile=SimpleNamespace(user=self.user))

    def test_account_cold_read_never_probes_or_acquires_a_lease_even_with_force(self):
        with patch("integrations.services.github_app.probe_installation_liveness") as provider, patch.object(
            cache, "add"
        ) as lease, patch.object(cache, "set") as write:
            for force in (False, True):
                proof = verify_account_access(self.user, connection=self.website, allow_probe=False, force=force)
                self.assertFalse(proof["verified"])
                self.assertEqual(proof["status"], "checking")
        provider.assert_not_called()
        lease.assert_not_called()
        write.assert_not_called()

    def test_account_observation_is_finite_and_current_actor_installation_scoped(self):
        with patch("integrations.services.github_app.probe_installation_liveness", return_value="live") as provider:
            proof = verify_account_access(self.user, connection=self.website, force=True)
            self.assertTrue(proof["verified"])
            self.assertEqual(verify_account_access(self.user, connection=self.website, allow_probe=False), proof)
            self.assertFalse(verify_account_access(SimpleNamespace(pk=703), connection=self.website, allow_probe=False)["verified"])
            self.website.state = "revoked"
            self.assertFalse(verify_account_access(self.user, connection=self.website, allow_probe=False)["verified"])
            self.website.state = "connected"
            cache.clear()
            self.assertFalse(verify_account_access(self.user, connection=self.website, allow_probe=False)["verified"])
        provider.assert_called_once_with("45")

    def test_account_context_propagates_observation_mode_to_real_helper(self):
        with patch("integrations.services.github_installations.user_github_installations", return_value=[]), patch.object(
            views, "actor_ids_for_user", return_value={"user-702"}
        ), patch("integrations.services.github_app.probe_installation_liveness") as provider:
            account = views._github_account_for_context(self.context(), self.config, allow_probe=False)
        self.assertTrue(account["owned"])
        self.assertFalse(account["verified"])
        self.assertEqual(account["status"], "checking")
        provider.assert_not_called()

    def test_repository_observation_mode_never_mints_credentials_or_calls_http(self):
        for typed in (False, True):
            with self.subTest(typed=typed), patch("content_factory.website_failure_scope.taxonomy_enabled", return_value=typed), patch.object(
                views, "_github_account_for_context", return_value={"owned": True}
            ), patch("integrations.services.github_app.create_installation_access_token") as mint, patch.object(
                views.http_client, "get"
            ) as get, patch.object(views.http_client, "delete") as delete:
                result = views._verify_github_repository_access(self.context(), self.config, force=True, allow_probe=False)
                self.assertFalse(result["verified"])
                self.assertEqual(result["reasonCode"], "github_verification_required")
                mint.assert_not_called()
                get.assert_not_called()
                delete.assert_not_called()

    def test_repository_positive_cache_cannot_survive_changed_local_binding_or_actor(self):
        for typed in (False, True):
            cache.clear()
            with self.subTest(typed=typed), ExitStack() as stack:
                stack.enter_context(patch("content_factory.website_failure_scope.taxonomy_enabled", return_value=typed))
                account = stack.enter_context(patch.object(views, "_github_account_for_context", return_value={"owned": True}))
                mint = stack.enter_context(patch("integrations.services.github_app.create_installation_access_token",
                    return_value=SimpleNamespace(token="test-ephemeral-observation")))
                stack.enter_context(patch("integrations.services.github_app.require_installation_repository_permissions",
                    return_value={"contents": "write", "pull_requests": "write"}))
                get = stack.enter_context(patch.object(views.http_client, "get", side_effect=[
                    Mock(status_code=200, json=lambda: {"id": 42, "full_name": "owner/site", "default_branch": "main"}),
                    Mock(status_code=200, json=lambda: {"sha": "a" * 40}),
                ]))
                stack.enter_context(patch.object(views.http_client, "delete"))
                proof = views._verify_github_repository_access(self.context(), self.config, force=True)
                self.assertTrue(proof["verified"])
                self.assertEqual(views._verify_github_repository_access(self.context(), self.config, allow_probe=False), proof)
                for key, changed in (("state", "revoked"), ("state", "disconnected"), ("state", "paused"),
                        ("generation", 2), ("configuration_version", 2), ("repository_id", 43),
                        ("installation_id", "46"), ("branch", "next"), ("verified_sha", "b" * 40),
                        ("authorized_by_id", 703)):
                    original = getattr(self.website, key)
                    setattr(self.website, key, changed)
                    self.assertFalse(views._verify_github_repository_access(self.context(), self.config, allow_probe=False)["verified"], key)
                    setattr(self.website, key, original)
                account.return_value = {"owned": False}
                self.assertFalse(views._verify_github_repository_access(self.context(), self.config, allow_probe=False)["verified"])
                self.assertEqual(get.call_count, 2)
                mint.assert_called_once()

    def test_bootstrap_loads_config_and_runs_once_and_reuses_cold_capabilities(self):
        runs, capabilities = [], {"canGenerateArticle": False}
        with patch.object(views, "_get_config", return_value=self.config) as config, patch.object(
            views, "_latest_runs_for_org", return_value=runs
        ) as latest, patch.object(views, "_bootstrap_cache_seconds", return_value=0), patch.object(
            views, "_compute_bootstrap_payload", return_value={"articleCapabilities": capabilities}
        ) as compute, patch.object(views, "_overlay_live_bootstrap_fields", return_value={}) as overlay:
            views._serialize_bootstrap(self.context())
        config.assert_called_once()
        latest.assert_called_once()
        self.assertIs(compute.call_args.kwargs["config"], self.config)
        self.assertIs(compute.call_args.kwargs["latest_runs"], runs)
        self.assertIs(overlay.call_args.kwargs["config"], self.config)
        self.assertIs(overlay.call_args.kwargs["latest_runs"], runs)
        self.assertIs(overlay.call_args.kwargs["capabilities"], capabilities)

    def test_preloaded_config_skips_progress_config_query(self):
        context = self.context()
        with patch.object(views, "_get_config", side_effect=AssertionError("Repeated config read")):
            organization, config, runs, checks = views._workflow_progress_context(context=context,
                config=self.config, latest_runs=[], checks={})
        self.assertIs(config, self.config)
        self.assertIs(organization, context.organization)
        self.assertEqual(runs, [])
        self.assertEqual(checks, {})

    def test_capability_projections_observe_and_forced_admission_still_probes(self):
        self.config.website_connection_id = self.website.pk
        for force, explicit, expected in ((False, None, False), (True, None, True), (True, False, False)):
            with self.subTest(force=force, explicit=explicit), patch.object(views, "_github_account_for_context",
                return_value={"owned": True, "saved": True}
            ) as account, patch.object(views, "_article_system_setup_gate", return_value={}), patch.object(
                views, "resolve_article_system", return_value={}
            ), patch.object(views, "integration_evidence", return_value={"verified": False}), patch.object(
                views, "_verify_github_repository_access", return_value={"verified": False}
            ) as repository, patch.object(views, "article_capabilities", return_value={}):
                views._article_capabilities_for_context(self.context(), self.config,
                    latest_runs=[], force=force, allow_probe=explicit)
                self.assertIs(account.call_args.kwargs["allow_probe"], expected)
                self.assertIs(repository.call_args.kwargs["allow_probe"], expected)
                self.assertIs(account.call_args.kwargs["force"], force)
                self.assertIs(repository.call_args.kwargs["force"], force)
