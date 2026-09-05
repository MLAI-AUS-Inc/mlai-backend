from pathlib import Path
from unittest import TestCase

from scripts.ci_reuse_pr_checks import (
    attestation_name,
    find_reusable_validation,
)


REPOSITORY = "MLAI-AUS-Inc/mlai-backend"
COMMIT_SHA = "a" * 40
HEAD_SHA = "b" * 40
TREE_SHA = "c" * 40
WORKFLOW_ID = 123
RUN_ID = 456


def api_fixture(*, artifact_tree=TREE_SHA, expired=False, run_pull_number=42):
    def api_get(path, params=None):
        if path.endswith(f"/commits/{COMMIT_SHA}/pulls"):
            return [
                {
                    "base": {"ref": "main"},
                    "head": {"sha": HEAD_SHA},
                    "merge_commit_sha": COMMIT_SHA,
                    "merged_at": "2026-08-13T00:00:00Z",
                    "number": 42,
                }
            ]
        if path.endswith(f"/actions/workflows/{WORKFLOW_ID}/runs"):
            return {
                "workflow_runs": [
                    {
                        "conclusion": "success",
                        "event": "pull_request",
                        "id": RUN_ID,
                        "pull_requests": (
                            []
                            if run_pull_number is None
                            else [{"number": run_pull_number}]
                        ),
                        "run_attempt": 1,
                    }
                ]
            }
        if path.endswith(f"/actions/runs/{RUN_ID}/artifacts"):
            return {
                "artifacts": [
                    {
                        "expired": expired,
                        "name": attestation_name(artifact_tree, HEAD_SHA, 1),
                    }
                ]
            }
        raise AssertionError(f"Unexpected GitHub API request: {path} {params}")

    return api_get


class ReusePrChecksTests(TestCase):
    def route(self, api_get):
        return find_reusable_validation(
            api_get=api_get,
            repository=REPOSITORY,
            commit_sha=COMMIT_SHA,
            branch="main",
            workflow_id=WORKFLOW_ID,
            current_tree=TREE_SHA,
        )

    def test_reuses_successful_validation_for_exact_tree_and_pull_request(self):
        reuse, reason = self.route(api_fixture())

        self.assertTrue(reuse)
        self.assertIn("PR #42", reason)
        self.assertIn(str(RUN_ID), reason)

    def test_reuses_when_github_omits_run_pull_request_associations(self):
        reuse, reason = self.route(api_fixture(run_pull_number=None))

        self.assertTrue(reuse)
        self.assertIn("PR #42", reason)

    def test_stale_base_tree_falls_back_to_full_checks(self):
        reuse, reason = self.route(api_fixture(artifact_tree="d" * 40))

        self.assertFalse(reuse)
        self.assertIn("exact deployed tree", reason)

    def test_expired_attestation_falls_back_to_full_checks(self):
        reuse, _reason = self.route(api_fixture(expired=True))

        self.assertFalse(reuse)

    def test_attestation_from_another_pull_request_is_not_reused(self):
        reuse, _reason = self.route(api_fixture(run_pull_number=99))

        self.assertFalse(reuse)

    def test_direct_push_falls_back_without_querying_workflow_runs(self):
        def api_get(path, params=None):
            self.assertTrue(path.endswith(f"/commits/{COMMIT_SHA}/pulls"))
            return []

        reuse, reason = self.route(api_get)

        self.assertFalse(reuse)
        self.assertIn("no exact merged pull request", reason)

    def test_deploy_workflow_preserves_full_check_fallback(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("validation-route:", workflow)
        self.assertNotIn("record-pr-validation:", workflow)
        self.assertEqual(workflow.count("reuse_pr_checks != 'true'"), 3)
        self.assertIn("needs.validation-route.outputs.reuse_pr_checks == 'true'", workflow)
        self.assertIn("needs.checks.result == 'success'", workflow)
        self.assertIn("needs.postgres-search.result == 'success'", workflow)
        self.assertIn("needs.migration-tests.result == 'success'", workflow)
