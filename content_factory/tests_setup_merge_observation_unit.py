"""Historical merge receipt tests without a database or provider credentials."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_connections as connections
from .website_contract import WebsiteAuthorityError


class SetupMergeObservationUnitTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk="11111111-1111-4111-8111-111111111111",
            generation=2, configuration_version=3, state="connected", installation_id="45",
            authorized_by_id=7, repository_id=123, github_repo="example/site", app_root="", branch="main")
        self.url = "https://github.com/example/site/pull/37"
        self.request = {"website_connection_id": str(self.connection.pk), "connection_generation": 2,
            "repository_id": 123, "github_repo": "example/site", "domain": "example.test",
            "app_root": "", "branch": "main", "operation_id": "22222222-2222-4222-8222-222222222222",
            "operation_attempt": 1, "deletion_epoch": 0, "expected_source_sha": "a" * 40,
            "source_sha": "a" * 40, "context": {"source_sha": "a" * 40}}
        self.run = SimpleNamespace(pk=1, run_id="setup-37", workflow="article_system_setup",
            github_repo="example/site", domain="example.test", run_request=deepcopy(self.request),
            result={"pr_url": self.url, "pr_number": 37}, refresh_from_db=Mock())
        self.pull = {"number": 37, "merged": True, "html_url": self.url, "merge_commit_sha": "b" * 40,
            "base": {"ref": "main", "repo": {"id": 123, "full_name": "example/site"}},
            "head": {"sha": "c" * 40, "repo": {"id": 123, "full_name": "example/site"}}}
        self.phases = []

    @contextmanager
    def guard(self, payload, **kwargs):
        self.assertEqual(kwargs["action"], "read")
        self.assertEqual(payload["operation_id"], self.request["operation_id"])
        self.assertEqual(payload["operation_attempt"], 1)
        self.assertEqual(payload["connection_generation"], 2)
        self.assertEqual(payload["run_id"], self.run.run_id)
        self.assertNotIn("source_sha", payload)
        self.assertNotIn("expected_source_sha", payload)
        self.assertNotIn("context", payload)
        self.phases.append("locked")
        yield self.connection
        self.phases.append("unlocked")

    def observe(self, provider=None):
        def read(*args):
            self.assertEqual(self.phases[-1], "unlocked")
            self.assertEqual(args, (self.connection, 37))
            return provider() if provider else self.pull
        with patch.object(connections, "authority_guard", self.guard), \
                patch.object(connections, "read_setup_merge_pull", side_effect=read), \
                patch("workflow_runs.models.ContentFactoryRun.objects") as rows:
            rows.select_for_update.return_value.get.return_value = self.run
            with connections.setup_merge_observation_guard(self.run) as receipt:
                self.assertEqual(self.phases[-1], "locked")
                return receipt

    def test_changed_source_only_records_provider_merge_and_keeps_original_scope(self):
        receipt = self.observe()
        self.assertEqual(receipt["merge_commit_sha"], "b" * 40)
        self.assertEqual(receipt["head_sha"], "c" * 40)
        self.assertEqual(self.run.run_request, self.request)
        self.assertEqual(self.phases, ["locked", "unlocked", "locked", "unlocked"])

    def test_rejects_unmerged_wrong_pr_source_and_repository_receipts(self):
        for change in [
            {"merged": False}, {"number": 38}, {"html_url": self.url + "0"},
            {"merge_commit_sha": "unknown"},
            {"base": {**self.pull["base"], "ref": "other"}},
            {"base": {**self.pull["base"], "repo": {"id": 999, "full_name": "example/site"}}},
            {"head": {**self.pull["head"], "repo": {"id": 999, "full_name": "example/site"}}},
            {"head": {**self.pull["head"], "sha": "unknown"}},
        ]:
            with self.subTest(change=change):
                with self.assertRaises(WebsiteAuthorityError):
                    self.observe(lambda: {**self.pull, **change})

    def test_concurrent_rebind_is_denied_after_provider_read(self):
        def changed():
            self.connection.installation_id = "new-installation"
            return self.pull
        with self.assertRaises(WebsiteAuthorityError) as caught:
            self.observe(changed)
        self.assertEqual(caught.exception.code, "website_connection_changed")

    def test_concurrent_request_replacement_is_denied(self):
        def changed():
            self.run.run_request = {**self.request, "operation_attempt": 2}
            return self.pull
        with self.assertRaises(WebsiteAuthorityError) as caught:
            self.observe(changed)
        self.assertEqual(caught.exception.code, "website_run_changed")

    def test_current_generation_and_operation_denials_are_not_bypassed(self):
        for code in ["website_disconnected", "website_connection_changed", "website_operation_changed", "website_operation_cancelled"]:
            with self.subTest(code=code), patch.object(connections, "authority_guard", side_effect=WebsiteAuthorityError(code, "denied")), \
                    patch.object(connections, "read_setup_merge_pull") as provider:
                with self.assertRaises(WebsiteAuthorityError):
                    with connections.setup_merge_observation_guard(self.run):
                        self.fail("Denied authority cannot write")
                provider.assert_not_called()

    def test_only_saved_setup_pr_identity_can_use_metadata_scope(self):
        for change in [{"workflow": "article_generation"}, {"github_repo": "example/other"},
                {"result": {"pr_url": "https://other.example/example/site/pull/37"}},
                {"run_request": {**self.request, "branch": "other"}}]:
            with self.subTest(change=change):
                previous = {key: getattr(self.run, key) for key in change}
                for key, value in change.items():
                    setattr(self.run, key, value)
                with self.assertRaises(WebsiteAuthorityError):
                    self.observe()
                for key, value in previous.items():
                    setattr(self.run, key, value)
