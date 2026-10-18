"""Private PR-stop credential profile, with no database or live provider."""
from unittest.mock import Mock, patch
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from integrations.services import github_app as app
from integrations.tests_github_workflow_permissions import response


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PullRequestControlTokenTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.check = Mock()
        self.grants = {"contents": "write", "pull_requests": "write", "workflows": "write", "administration": "write"}
        self.required = {"contents": "read", "pull_requests": "write"}
        self.enterContext(patch.object(app, "_github_app_jwt", return_value="synthetic-jwt"))
        self.get = self.enterContext(patch.object(app.http_requests, "get", side_effect=lambda *a, **k: response(200, {"permissions": self.grants})))
        self.post = self.enterContext(patch.object(app.http_requests, "post", return_value=response(201, {"token": "synthetic-control-token", "permissions": self.required})))
        self.delete = self.enterContext(patch.object(app.http_requests, "delete", return_value=response(204, {})))

    def mint(self, **changes):
        return app.create_installation_access_token(**{"installation_id": "45", "repository": "example/site", "repository_id": 123,
            "permission_mode": "write", "permission_profile": "pull_request_control", "use_cache": False, "before_issue": self.check, **changes})

    def test_only_one_repository_read_contents_and_write_pull_requests_are_requested(self):
        value = self.mint()
        self.assertEqual(value.permissions, self.required)
        self.assertEqual(value.permission_profile, "pull_request_control")
        self.assertEqual(self.post.call_args.kwargs["json"], {"repository_ids": [123], "permissions": self.required})
        self.assertEqual(self.get.call_count, 2)
        self.check.assert_called_once_with()
        self.mint()
        self.assertEqual(self.post.call_count, 2)

    def test_mutable_identity_read_mode_cached_use_or_missing_purpose_never_reaches_provider(self):
        for change in ({"repository_id": None}, {"repository_id": True}, {"permission_mode": "read"}, {"permission_mode": "WRITE"},
                {"use_cache": True}, {"use_cache": 0}, {"before_issue": None}):
            with self.subTest(change=change), self.assertRaises(app.GitHubAppTokenError): self.mint(**change)
        self.get.assert_not_called(); self.post.assert_not_called(); self.check.assert_not_called()

    def test_owner_revocation_after_grants_is_checked_immediately_before_mint(self):
        def revoked():
            self.assertEqual(self.get.call_count, 2)
            self.post.assert_not_called()
            raise PermissionError("synthetic revoked purpose")
        with self.assertRaises(PermissionError): self.mint(before_issue=revoked)
        self.post.assert_not_called()

    def test_missing_approved_pull_request_write_is_not_silently_upgraded(self):
        self.grants["pull_requests"] = "read"
        with self.assertRaises(app.GitHubInstallationAccessRequired): self.mint()
        self.post.assert_not_called(); self.check.assert_not_called()

    def test_missing_grants_or_suspended_installation_never_mints(self):
        for value in ({}, {"permissions": self.grants, "suspended_at": "now"}):
            self.get.side_effect = None; self.get.return_value = response(200, value)
            with self.assertRaises(app.GitHubAppTokenError): self.mint()
        self.post.assert_not_called()

    def test_overbroad_or_incomplete_token_is_revoked_before_escaping(self):
        for value in ({"contents": "write", "pull_requests": "write"}, {"contents": "read", "pull_requests": "read"},
                {**self.required, "workflows": "write"}, {**self.required, "checks": "read"}, {}):
            self.post.return_value = response(201, {"token": "synthetic-rejected-token", "permissions": value})
            with self.subTest(value=value), self.assertRaises(app.GitHubAppTokenError): self.mint()
        self.assertEqual(self.delete.call_count, 5)

    def test_incidental_metadata_read_is_accepted_without_expanding_other_grants(self):
        self.post.return_value = response(201, {"token": "synthetic-control-token", "permissions": {**self.required, "metadata": "read"}})
        self.assertEqual(self.mint().permissions, {**self.required, "metadata": "read"})

    def test_generic_website_broker_cannot_request_the_private_profile(self):
        from content_factory.website_tokens import mint_website_token
        from content_factory.website_contract import WebsiteAuthorityError
        with self.assertRaises(WebsiteAuthorityError) as error:
            mint_website_token({"permission_profile": "pull_request_control"}, permission_mode="write", action="publish")
        self.assertEqual(error.exception.code, "invalid_permission_profile")
        self.post.assert_not_called()
