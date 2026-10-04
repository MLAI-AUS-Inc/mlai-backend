"""Pure rollout policy regressions; no database or external services."""

from unittest import TestCase
from unittest.mock import patch

from content_factory.website_contract import WebsiteAuthorityError
from content_factory.website_rollout import (
    apply_repository_write_policy, repository_write_policy, require_repository_write_policy,
)


class WebsiteRolloutTests(TestCase):
    def test_unknown_mode_and_empty_canary_fail_closed(self):
        for mode in ("disabled", "typo", "canary"):
            self.assertFalse(repository_write_policy("site.test", environ={"WEBSITE_CONNECTION_WRITE_MODE": mode})["allowed"])

    def test_canary_matches_exact_domain_not_subdomain_suffix_or_url(self):
        env = {"WEBSITE_CONNECTION_WRITE_MODE": "canary", "WEBSITE_CONNECTION_CANARY_DOMAINS": " Site.Test.,https://bad.test, *.wild.test"}
        self.assertTrue(repository_write_policy("SITE.TEST", environ=env)["allowed"])
        for domain in ("sub.site.test", "evilsite.test", "site.test.evil", "bad.test", "wild.test", ""):
            self.assertFalse(repository_write_policy(domain, environ=env)["allowed"])

    @patch.dict("os.environ", {"WEBSITE_CONNECTION_WRITE_MODE": "disabled"})
    def test_disabled_denies_all_new_write_actions_but_preserves_read_and_disconnect(self):
        for action in ("setup", "publish", "merge", "preview", "cleanup"):
            with self.assertRaises(WebsiteAuthorityError):
                require_repository_write_policy(action, "site.test")
        for action in ("read", "scan", "config_write", "disconnect", "revoke"):
            require_repository_write_policy(action, "site.test")

    @patch.dict("os.environ", {"WEBSITE_CONNECTION_WRITE_MODE": "disabled"})
    def test_client_projection_cannot_retain_publish_permission(self):
        original = {"capabilities": {"publishingReady": True, "previewSupported": True, "generationReady": True},
                    "allowedActions": ["scan", "publish", "setup", "cleanup", "disconnect", "reconnect"], "blockers": []}
        value = apply_repository_write_policy(original, "site.test")
        self.assertEqual(value["allowedActions"], ["scan", "disconnect", "reconnect"])
        self.assertFalse(value["capabilities"]["publishingReady"])
        self.assertTrue(value["capabilities"]["generationReady"])
        self.assertTrue(original["capabilities"]["publishingReady"])
        self.assertEqual(value["blockers"][0]["code"], "website_writes_paused")

    def test_enabled_is_only_a_rollout_decision_not_an_authorization_grant(self):
        self.assertTrue(repository_write_policy("site.test", environ={})["allowed"])
        self.assertEqual(set(repository_write_policy("site.test", environ={})), {"mode", "allowed", "code", "message"})
