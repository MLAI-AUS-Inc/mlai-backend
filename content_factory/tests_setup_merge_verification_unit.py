"""First-time setup merge proof, without database or provider access."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_connections as authority
from .website_contract import WebsiteAuthorityError


class SetupMergeVerificationUnitTests(SimpleTestCase):
    def setUp(self):
        self.base, self.head = "a" * 40, "b" * 40
        identifier = "11111111-1111-4111-8111-111111111111"
        self.website = SimpleNamespace(id=identifier, pk=identifier, generation=2,
            repository_id=123, github_repo="example/site", state="connected", verified_sha="",
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.website.targets.filter.return_value = []
        self.run = SimpleNamespace(run_id="setup-1", workflow="article_system_setup", status="completed",
            approval_state="approved", domain="example.test", github_repo="example/site",
            run_request={"website_connection_id": identifier, "connection_generation": 2,
                "repository_id": 123, "expected_source_sha": self.base},
            result={"status": "setup_pr_created", "source_sha": self.base, "branch_commit_sha": self.head,
                "resume_generation": 1,
                "article_system_setup": {"status": "pr_created", "setup_run_id": "setup-1",
                    "source_sha": self.base, "branch_commit_sha": self.head, "resume_generation": 1},
                "live_preview": {"status": "running", "exactRender": True, "commitSha": self.head},
                "directory_quality_gates": {"status": "passed", "local_build_skipped": False,
                    **{key: True for key in ("dependency_validation", "static_policy", "local_build",
                        "dom_slot_compliance", "publish_surface", "browser", "visual_style")}},
                "directory_quality_verification": {"status": "passed", "commit_sha": self.head,
                    "preview_identity": f"commit:{self.head}", "resume_generation": 1,
                    "task_id": "verification-1", "completed_at": "2026-10-09T10:45:37+00:00"}})

    def validate(self, head=None):
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "verify_repository_head", return_value=self.base) as verify:
            authority.validate_setup_merge_source(self.run, head or self.head)
        verify.assert_called_once_with(self.website, self.base)

    def test_first_setup_uses_exact_approved_run_without_promoting_target(self):
        before = deepcopy(self.run.result)
        self.validate()
        self.assertEqual(self.run.result, before)
        self.assertEqual(self.website.verified_sha, "")
        self.website.targets.update_or_create.assert_not_called()

    def test_preserves_existing_target_proof_path(self):
        self.website.verified_sha = self.base
        self.website.targets.filter.return_value = [SimpleNamespace(contract={"verification": {
            "status": "preview_verified", "source_sha": self.head, "base_sha": self.base}})]
        self.run.result = {}
        self.validate()

    def test_rejects_changed_head_and_incomplete_or_stale_run_proof(self):
        original = deepcopy(self.run.result)
        changes = [
            ((), "branch_commit_sha", "c" * 40),
            ((), "source_sha", "c" * 40),
            ((), "status", "preview_ready"),
            ((), "resume_generation", True),
            ((), "manual_quality_gate_override", {"approved": True}),
            (("article_system_setup",), "branch_commit_sha", "c" * 40),
            (("article_system_setup",), "source_sha", "c" * 40),
            (("article_system_setup",), "setup_run_id", "other"),
            (("article_system_setup",), "resume_generation", 0),
            (("live_preview",), "exactRender", False),
            (("live_preview",), "commitSha", "c" * 40),
            (("directory_quality_verification",), "commit_sha", "c" * 40),
            (("directory_quality_verification",), "preview_identity", "commit:" + "c" * 40),
            (("directory_quality_verification",), "resume_generation", 0),
            (("directory_quality_verification",), "resume_generation", True),
            (("directory_quality_verification",), "status", "running"),
            (("directory_quality_verification",), "completed_at", None),
            (("directory_quality_verification",), "task_id", None),
            (("directory_quality_gates",), "local_build_skipped", True),
            (("directory_quality_gates",), "browser", False),
            (("directory_quality_gates",), "visual_style", False),
            (("directory_quality_gates",), "status", "failed"),
        ]
        for path, key, value in changes:
            with self.subTest(key=key, value=value):
                self.run.result = deepcopy(original)
                node = self.run.result
                for part in path:
                    node = node[part]
                node[key] = value
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
        self.run.result = original
        with self.assertRaises(WebsiteAuthorityError):
            self.validate("c" * 40)

    def test_rejects_unapproved_cancelled_and_other_workflows(self):
        for key, value in (("approval_state", "pending"), ("status", "cancelled"), ("workflow", "article_generation")):
            with self.subTest(key=key):
                previous = getattr(self.run, key)
                setattr(self.run, key, value)
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
                setattr(self.run, key, previous)

    def test_rejects_revoked_rebound_repository_and_changed_base(self):
        for key, value in (("state", "revoked"), ("generation", 3), ("repository_id", 999),
                ("verified_sha", "c" * 40)):
            with self.subTest(key=key):
                previous = getattr(self.website, key)
                setattr(self.website, key, value)
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
                setattr(self.website, key, previous)
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "verify_repository_head", side_effect=WebsiteAuthorityError("website_source_changed", "Changed")):
            with self.assertRaises(WebsiteAuthorityError):
                authority.validate_setup_merge_source(self.run, self.head)
