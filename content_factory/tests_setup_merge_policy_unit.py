"""Setup merge regressions with real authority, token, policy and merge code."""

from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
import requests

from . import website_prepare as prepare, vibe_marketing_views as views
from . import tests_prepare_permissions_unit as fixtures
from . import tests_setup_merge_verification_unit as proofs


def response(payload, code=200):
    import json
    result = requests.Response()
    result.status_code = code
    result._content = json.dumps(payload).encode()
    return result


class SetupMergePolicyUnitTests(SimpleTestCase):
    rows = fixtures.PrepareVerificationRuntimeTests.rows
    boundaries = fixtures.PrepareVerificationRuntimeTests.boundaries

    def setUp(self):
        from workflow_runs.models import ContentFactoryRun
        fixtures.PrepareVerificationRuntimeTests.setUp(self)
        proof = proofs.SetupMergeVerificationUnitTests()
        proof.setUp()
        self.config.requires_review = False
        self.config.auto_publish = True
        self.run = ContentFactoryRun(pk=19, run_id="setup-1", workflow="article_system_setup",
            domain=self.org.domain, github_repo=self.website.github_repo, status="completed",
            approval_state="approved", result=deepcopy(proof.run.result),
            run_request={**prepare.contract_for(self.website), "expected_source_sha": self.website.verified_sha})
        self.run.save = Mock()
        self.run.refresh_from_db = Mock()
        self.head = proof.head
        self.run.result.update(pr_number=17, pr_url=f"https://github.com/{self.website.github_repo}/pull/17")
        repo = {"id": self.website.repository_id, "full_name": self.website.github_repo}
        self.pull = {"number": 17, "state": "open", "merged": False,
            "html_url": self.run.result["pr_url"],
            "head": {"sha": self.head, "ref": "cf/setup", "repo": repo},
            "base": {"ref": self.website.branch, "repo": repo}}
        self.rules = []
        self.protection_code = 403
        self.protection = {"message": "Resource not accessible by integration"}
        self.rules_code = 200
        self.checks = {"check_runs": []}
        self.merge_code = 200
        self.merge_payload = {"merged": True, "sha": "c" * 40}
        self.merge_calls = []

    def provider(self, method, url, **kwargs):
        if url.endswith("/protection"):
            return response(self.protection, self.protection_code)
        if "/rules/branches/" in url:
            return response(self.rules, self.rules_code)
        if method == "PUT" and url.endswith("/merge"):
            self.merge_calls.append(kwargs["json"])
            if self.merge_payload.get("merged") is True:
                self.pull.update(merged=True, merge_commit_sha=self.merge_payload["sha"])
            return response(self.merge_payload, self.merge_code)
        if "/pulls/" in url:
            return response(self.pull)
        if url.endswith("/status"):
            return response({"total_count": 0, "state": "success"})
        if url.endswith("/check-runs"):
            return response(self.checks)
        if "/commits/" in url:
            return response({"sha": self.website.verified_sha})
        return response({"id": self.website.repository_id, "full_name": self.website.github_repo,
            "default_branch": self.website.branch})

    def merge_boundaries(self):
        from integrations.services import github_app
        from workflow_runs.models import ContentFactoryRun
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection
        stack = self.boundaries()
        stack.enter_context(patch.object(OrganizationContentConfig.objects, "select_related",
            return_value=self.rows(first=self.config)))
        stack.enter_context(patch.object(WebsiteConnection.objects, "get", return_value=self.website))
        stack.enter_context(patch.object(ContentFactoryRun.objects, "select_for_update",
            return_value=self.rows(first=self.run)))
        stack.enter_context(patch.object(github_app, "create_installation_access_token",
            return_value=github_app.GitHubInstallationToken("synthetic", None, "4", self.website.github_repo)))
        stack.enter_context(patch.object(views.http_client, "get", side_effect=lambda url, **kw: self.provider("GET", url, **kw)))
        stack.enter_context(patch.object(views.http_client, "request", side_effect=self.provider))
        return stack

    def merge(self):
        with self.merge_boundaries():
            return prepare.maybe_merge_prepared_setup(self.run, self.context, self.config, user=self.user)

    def test_protection_403_is_unknown_and_exact_head_merge_is_attempted(self):
        result = self.merge()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(self.merge_calls[0]["sha"], self.head)
        self.assertEqual(self.run.result["merge_status"], "merged")

    def test_checks_read_403_still_uses_policy_and_attempts_fenced_merge(self):
        from tests.website_fixtures import setup_branch_policy_get_fixture
        original_provider = self.provider
        def provider(method, url, **kwargs):
            if "/rules/branches/" in url or url.endswith("/protection"):
                return setup_branch_policy_get_fixture(url, **kwargs)
            if url.endswith(("/status", "/check-runs")):
                return response({"message": "Resource not accessible by integration"}, 403)
            return original_provider(method, url, **kwargs)
        self.provider = provider
        result = self.merge()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(self.merge_calls[0]["sha"], self.head)
        self.assertEqual(self.run.result["merge_status"], "merged")

    def test_ruleset_requiring_review_asks_without_merging(self):
        self.rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
        result = self.merge()
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["user_action"]["id"], "merge_pr")
        self.assertEqual(self.merge_calls, [])

    def test_protection_404_is_unknown_and_still_allows_a_fenced_attempt(self):
        self.protection_code = 404
        self.assertEqual(self.merge()["status"], "pending")
        self.assertEqual(len(self.merge_calls), 1)

    def test_ruleset_code_owner_review_is_an_owner_action_even_with_zero_count(self):
        self.rules = [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 0, "require_code_owner_review": True}}]
        self.assertEqual(self.merge()["status"], "needs_user")
        self.assertEqual(self.merge_calls, [])

    def test_unavailable_rules_wait_without_merging(self):
        self.rules_code = 503
        self.assertEqual(self.merge()["status"], "pending")
        self.assertEqual(self.merge_calls, [])

    def test_rate_limit_is_a_wait(self):
        self.rules_code = 429
        self.assertEqual(self.merge()["status"], "pending")
        self.assertEqual(self.merge_calls, [])

    def test_json_merge_503_remains_a_wait(self):
        self.merge_code = 503
        self.merge_payload = {"message": "Internal Server Error"}
        result = self.merge()
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("user_action", result)
        self.assertEqual(len(self.merge_calls), 1)

    def test_required_review_refusal_remains_owner_action(self):
        self.merge_code = 405
        self.merge_payload = {"message": "At least one approving review is required"}
        result = self.merge()
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["user_action"]["id"], "merge_pr")
        self.assertEqual(len(self.merge_calls), 1)

    def test_pending_checks_wait_without_merging_or_asking_owner(self):
        self.checks = {"check_runs": [{"status": "in_progress"}]}
        result = self.merge()
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("user_action", result)
        self.assertEqual(self.merge_calls, [])

    def test_check_refusal_waits_instead_of_asking_owner(self):
        self.merge_code = 405
        self.merge_payload = {"message": "Required status check is pending"}
        self.assertEqual(self.merge()["status"], "pending")
        self.assertEqual(len(self.merge_calls), 1)

    def test_owner_review_preference_prevents_automatic_merge(self):
        self.config.requires_review = True
        self.assertEqual(self.merge()["status"], "needs_user")
        self.assertEqual(self.merge_calls, [])

    def test_status_refresh_never_merges_even_with_auto_publish_consent(self):
        with self.merge_boundaries():
            refreshed, changed = views._refresh_pending_article_system_setup_pr_status(
                context=self.context, config=self.config, run=self.run)
        self.assertIs(refreshed, self.run)
        self.assertFalse(changed)
        self.assertEqual(self.merge_calls, [])

    def test_a_negative_merge_receipt_never_becomes_a_success(self):
        self.merge_payload = {"merged": False, "message": "Required status checks are pending"}
        self.assertEqual(self.merge()["status"], "pending")
        self.assertNotEqual(self.run.result.get("merge_status"), "merged")

    def test_changed_preview_head_never_merges(self):
        # Preparation never merges or re-checks a changed head; the owner's Publish does.
        self.pull["head"]["sha"] = "d" * 40
        result = self.merge()
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual((result["user_action"]["id"], result["user_action"]["run_id"]), ("review_setup", "setup-1"))
        self.assertEqual(self.merge_calls, [])

    def test_failed_checks_are_repairable_instead_of_an_owner_merge_action(self):
        self.checks = {"check_runs": [{"status": "completed", "conclusion": "failure"}]}
        with self.assertRaises(prepare.WebsiteAuthorityError) as raised:
            self.merge()
        self.assertEqual(raised.exception.code, "setup_checks_failed")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.merge_calls, [])
