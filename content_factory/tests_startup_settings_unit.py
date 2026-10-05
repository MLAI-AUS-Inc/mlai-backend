"""Settings, repository, and logo effects with explicit fake I/O boundaries."""
from io import BytesIO
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.exceptions import ValidationError

from content_factory import github_repository_views as repositories
from content_factory import vibe_marketing_views as views


class RepositorySelectionTests(SimpleTestCase):
    def setUp(self):
        self.config = type("Config", (), {"objects": Mock()})()
        self.config.pk = 1
        self.config.website_connection_id = None
        self.config.github_repo = "old/site"
        self.config.article_system = {"scan": {"ready": True}}
        self.config.daily_discovery_enabled = True
        self.config.save = Mock()
        self.config._meta = Mock()
        self.config._meta.get_field.return_value.get_default.return_value = None
        self.config.objects.select_for_update.return_value.get.return_value = self.config
        self.context = Obj(organization=Obj(pk=2))
        patcher = patch.object(repositories.Organization.objects, "select_for_update")
        self.organization_lock = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("bind_website", "transition_connection", "contract_for"):
            patcher = patch.object(repositories, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        for name, value in (("_resolve_context_or_response", (self.context, None)),
                            ("_get_config", self.config), ("_github_repo_company_conflict_response", None),
                            ("_verify_github_repository_access", {"verified": True})):
            patcher = patch.object(views, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(repositories, "reset_article_setup_config")
        self.reset = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(repositories.ResearchAutomation.objects, "filter")
        self.automation = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, repo, *, company=True):
        request = Obj(user=Obj(id=7), data={"githubRepo": repo, **({"companyId": "company"} if company else {})}, query_params={})
        return repositories.VibeMarketingGitHubRepositoryView.put.__wrapped__(
            repositories.VibeMarketingGitHubRepositoryView(), request)

    def test_selection_verifies_prospective_repo_before_persist_and_invalidates_readiness(self):
        response = self.call("new/site")
        self.assertEqual(response.status_code, 200)
        candidate = self._verify_github_repository_access.call_args.args[1]
        self.assertEqual(candidate.github_repo, "new/site")
        self.assertIsNot(candidate, self.config)
        self.assertEqual(self.config.github_repo, "new/site")
        self.assertNotIn("scan", self.config.article_system)
        self.assertFalse(self.config.daily_discovery_enabled)
        self.reset.assert_called_once_with(self.config, github_repo="new/site")
        self.bind_website.assert_called_once_with(self.config, user=self.bind_website.call_args.kwargs["user"], repo="new/site", expected={"githubRepo": "new/site", "companyId": "company"})
        self.assertTrue(response.data["requiresVerification"])
        self.automation.return_value.update.assert_called_once_with(status=repositories.ResearchAutomationStatus.PAUSED)

    def test_inaccessible_or_malformed_repo_never_changes_selection(self):
        self._verify_github_repository_access.return_value = {"verified": False, "reasonCode": "github_access_required"}
        self.assertEqual(self.call("new/site").status_code, 409)
        self.assertEqual(self.call("https://github.com/new/site").status_code, 400)
        self.assertEqual(self.call("new/site", company=False).status_code, 400)
        self.assertEqual(self.config.github_repo, "old/site")
        self.config.save.assert_not_called()
        self.reset.assert_not_called()

    def test_unlink_does_not_require_oauth_and_invalidates_old_ready_state(self):
        response = self.call("")
        self.assertEqual(response.data["githubRepo"], "")
        self._verify_github_repository_access.assert_not_called()
        self.reset.assert_called_once()
        self.assertFalse(response.data["requiresVerification"])

    def test_reselect_same_repo_keeps_existing_setup(self):
        self.config.website_connection_id = 42
        response = self.call("old/site")
        self.assertFalse(response.data["repositoryChanged"])
        self.reset.assert_not_called()
        self.config.save.assert_not_called()

    def test_legacy_repository_without_connection_binds_before_reverification(self):
        response = self.call("old/site")
        self.bind_website.assert_called_once()
        self.organization_lock.return_value.get.assert_called_once_with(pk=2)
        self.assertTrue(response.data["requiresVerification"])

    def test_unlink_revokes_existing_connection_generation(self):
        self.config.website_connection_id = 42
        self.config.website_connection = Obj(pk=42)
        self.call("")
        self.contract_for.assert_called_once_with(self.config.website_connection)
        self.transition_connection.assert_called_once_with(self.config, action="disconnect",
            expected=self.contract_for.return_value)
        self.bind_website.assert_not_called()

    def test_stale_connection_rejection_rolls_back_without_resetting_setup(self):
        self.bind_website.side_effect = repositories.WebsiteAuthorityError("website_connection_changed", "Refresh and retry.")
        with patch.object(repositories.transaction, "set_rollback") as rollback:
            response = self.call("new/site")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_connection_changed")
        rollback.assert_called_once_with(True)
        self.reset.assert_not_called()
        self.config.save.assert_not_called()


class LogoAndBootstrapTests(SimpleTestCase):
    def setUp(self):
        self.company = Obj(id="company", name="Acme", domain="acme.test", avatar_url="old")
        self.user = Obj(id=7)

    def upload(self):
        data = BytesIO()
        Image.new("RGBA", (512, 512), (10, 20, 30, 128)).save(data, format="PNG")
        return SimpleUploadedFile("crop.png", data.getvalue(), content_type="image/png")

    def test_upload_uses_unique_png_path_and_storage_failure_preserves_old_logo(self):
        view = views.VibeMarketingCompanyAvatarView()
        with patch.object(view, "_company", return_value=(self.company, None)), \
             patch("founder_tools.profile_fields.save_company_branding") as save, \
             patch("core.firebase_utils.upload_file_to_storage", return_value="new-url") as upload:
            for _ in range(2):
                response = view.post(Obj(user=self.user, FILES={"avatar": self.upload()}))
                self.assertEqual(response.status_code, 200)
            paths = [call.args[1] for call in upload.call_args_list]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(path.startswith("company-avatars/company/") and path.endswith(".png") for path in paths))
            self.assertEqual(upload.call_args.kwargs["content_type"], "image/png")
            save.reset_mock()
            upload.side_effect = RuntimeError("synthetic storage failure")
            with self.assertLogs(views.logger, level="ERROR"):
                self.assertEqual(view.post(Obj(user=self.user, FILES={"avatar": self.upload()})).status_code, 502)
            save.assert_not_called()
            self.assertEqual(self.company.avatar_url, "old")

    def test_remove_calls_canonical_clear_without_storage_upload(self):
        view = views.VibeMarketingCompanyAvatarView()
        with patch.object(view, "_company", return_value=(self.company, None)), \
             patch("founder_tools.profile_fields.save_company_branding") as save:
            self.assertEqual(view.delete(Obj(user=self.user)).status_code, 200)
        save.assert_called_once_with(self.company, self.user, "")

    def test_bootstrap_resolves_requested_company_before_domainless_branch(self):
        selected = Obj(domain="")
        with patch.object(views, "_resolve_profile_company_or_response", return_value=(Obj(), selected, None)) as resolve, \
             patch.object(views, "_serialize_bootstrap_without_domain", return_value={"selected": True}) as serialize, \
             patch.object(views, "_timed_vibe_response", side_effect=lambda payload, **kw: payload):
            result = views.VibeMarketingBootstrapView().get(Obj(query_params={"companyId": "selected"}))
        self.assertTrue(result["selected"])
        resolve.assert_called_once()
        serialize.assert_called_once_with(selected)

    def test_invalid_timezone_fails_before_company_or_settings_mutation(self):
        with patch.object(views, "_resolve_profile_company_or_response") as resolve, self.assertRaises(ValidationError):
            views.VibeMarketingSettingsView().put(
                Obj(data={"defaultTimezone": "not/a-zone"}))
        resolve.assert_not_called()

    def test_invalid_linkedin_requests_rollback_after_company_update(self):
        company = Obj(name="Acme", domain="acme.test", save=Mock())
        with patch.object(views, "_resolve_profile_company_or_response", return_value=(Obj(), company, None)), \
             patch.object(views, "apply_company_domain_change"), \
             patch.object(views, "ensure_company_organization", return_value=Obj()), \
             patch.object(views.transaction, "set_rollback") as rollback:
            response = views.VibeMarketingSettingsView._save_settings.__wrapped__(views.VibeMarketingSettingsView(),
                Obj(user=Obj(is_staff=False, is_superuser=False),
                    data={"companyName": "Changed", "companyLinkedInUrl": "https://other.test"}))
        self.assertEqual(response.status_code, 400)
        rollback.assert_called_once_with(True)

    def test_name_only_bootstrap_retains_saved_optional_fields_and_logo(self):
        company = Obj(id="company", name="Acme", domain="", location="Melbourne", abn=None,
            organization_id=2, organization=Obj(pk=2, company_linkedin_url="https://www.linkedin.com/company/acme",
                competitors=["rival.test"], seed_keywords=["health"]))
        with patch.object(views, "company_registration_status", return_value={}), \
             patch.object(views, "company_avatar_url", return_value="canonical-logo"), \
             patch.object(views, "_serialize_startup_profile", return_value={"shortDescription": "Saved", "hasRevenue": "No"}), \
             patch("founder_tools.serializers._serialize_marketing_settings", return_value={"companyContext": "Notes", "defaultTimezone": "Australia/Melbourne"}):
            result = views._serialize_bootstrap_without_domain(company)
        self.assertEqual(result["startupProfile"]["shortDescription"], "Saved")
        self.assertEqual(result["startupProfile"]["hasRevenue"], "No")
        self.assertEqual(result["settings"]["companyContext"], "Notes")
        self.assertEqual(result["company"]["avatarUrl"], "canonical-logo")
        self.assertEqual(result["organization"]["domain"], "")
        self.assertFalse(result["checks"]["websiteProfile"]["passed"])
