"""Articles-setup "Publish to production" merge.

The setup PR merge token is scoped to contents+pull_requests only, so reading the PR's
commit-status / check-runs 403s with "Resource not accessible by integration". The merge
must no longer hard-fail on that: it tolerates the checks-read gap, tries a direct squash
merge (works on an unprotected main), then falls back to GitHub native auto-merge — the
same robustness as article-generation publish. Plus hands-off auto-publish for orgs that
opt into auto_publish.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from content_factory import vibe_marketing_views as views
from content_factory.models import OrganizationContentConfig
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

REPO = "The-Product-Bus/tpbnewsite"
PR = 1
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
PERMISSION_ERROR = "Resource not accessible by integration"


def _open_pull(merged=False, node_id="PR_node_1"):
    return {"merged": merged, "state": "open", "head": {"sha": "abc123"}, "node_id": node_id}


class SetupMergeBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="theproductbus.com", name="TPB")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo=REPO)
        self.context = SimpleNamespace(organization=self.org)
        self.run = self._setup_run()

    def _setup_run(self, run_id="setup-1", status="pr_created"):
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=REPO,
            status=ContentFactoryRunStatus.COMPLETED,
            result={
                "pr_url": PR_URL,
                "pr_number": PR,
                "article_system_setup": {
                    "setup_run_id": run_id,
                    "setupRunId": run_id,
                    "pr_url": PR_URL,
                    "pr_number": PR,
                    "status": status,
                    "setupStatus": status,
                    "merge_status": "not_merged",
                },
            },
        )

    def _patch_token(self):
        return mock.patch.object(
            views, "_github_token_for_repo_operation", return_value=("tok", "github_app_installation")
        )


class AttemptSetupPublishMergeTests(SetupMergeBase):
    def test_checks_read_403_no_longer_hard_fails_and_direct_merge_succeeds(self):
        """Regression: the checks/statuses-read 403 must not abort — the direct merge runs."""
        merge_calls = []

        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull()
            if method == "GET" and "/status" in path:
                raise ValueError(PERMISSION_ERROR)
            if method == "GET" and "/check-runs" in path:
                raise ValueError(PERMISSION_ERROR)
            if method == "PUT" and path.endswith(f"/pulls/{PR}/merge"):
                merge_calls.append(body)
                return {"merged": True, "sha": "merged-sha"}
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(views, "_github_api_request", side_effect=fake_api):
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(len(merge_calls), 1)  # the 403 did NOT short-circuit the merge
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "merged")

    def test_direct_merge_success_with_clean_checks(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull()
            if method == "GET" and "/status" in path:
                return {"total_count": 0, "state": ""}
            if method == "GET" and "/check-runs" in path:
                return {"check_runs": []}
            if method == "PUT" and path.endswith(f"/pulls/{PR}/merge"):
                return {"merged": True}
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(views, "_github_api_request", side_effect=fake_api):
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "merged")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["article_system_setup"]["checks_status"], "success")

    def test_direct_merge_failure_falls_back_to_native_auto_merge(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull()
            if method == "GET" and "/status" in path:
                raise ValueError(PERMISSION_ERROR)
            if method == "PUT" and path.endswith(f"/pulls/{PR}/merge"):
                raise ValueError("At least 1 approving review is required.")
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(
            views, "_github_api_request", side_effect=fake_api
        ), mock.patch.object(views, "_enable_native_auto_merge", return_value={"status": "enabled", "message": "ok"}) as auto:
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "auto_merge_pending")
        auto.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "publishing")
        self.assertTrue(self.run.result["article_system_setup"]["native_auto_merge_enabled"])

    def test_native_auto_merge_unavailable_surfaces_manual(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull()
            if method == "GET" and "/status" in path:
                raise ValueError(PERMISSION_ERROR)
            if method == "PUT" and path.endswith(f"/pulls/{PR}/merge"):
                raise ValueError("Pull Request is not mergeable")
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(
            views, "_github_api_request", side_effect=fake_api
        ), mock.patch.object(
            views, "_enable_native_auto_merge",
            return_value={"status": "unavailable", "message": "Auto-merge is not allowed for this repository"},
        ):
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "manual_required")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["merge_status"], "manual_merge_required")

    def test_already_merged_pr_short_circuits_without_merge_call(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return {"merged": True, "state": "closed"}
            if method == "PUT":
                raise AssertionError("merge must not be attempted for an already-merged PR")
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(views, "_github_api_request", side_effect=fake_api):
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "merged")

    def test_failed_checks_block_without_merging(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull()
            if method == "GET" and "/status" in path:
                return {"total_count": 0, "state": ""}
            if method == "GET" and "/check-runs" in path:
                return {"check_runs": [{"status": "completed", "conclusion": "failure"}]}
            if method == "PUT":
                raise AssertionError("merge must not be attempted when checks failed")
            raise AssertionError(f"unexpected {method} {path}")

        with self._patch_token(), mock.patch.object(views, "_github_api_request", side_effect=fake_api):
            outcome = views._attempt_setup_publish_merge(run=self.run, context=self.context)

        self.assertEqual(outcome["outcome"], "checks_failed")


class MergeSetupPrWrapperTests(SetupMergeBase):
    def test_wrapper_returns_run_on_merge(self):
        with self._patch_token(), mock.patch.object(
            views, "_attempt_setup_publish_merge",
            return_value={"outcome": "merged", "detail": "ok", "checks": {}, "run": self.run},
        ):
            run, error = views._merge_setup_pr_for_run(run=self.run, context=self.context)
        self.assertIsNotNone(run)
        self.assertIsNone(error)

    def test_wrapper_returns_run_on_publishing(self):
        with self._patch_token(), mock.patch.object(
            views, "_attempt_setup_publish_merge",
            return_value={"outcome": "auto_merge_pending", "detail": "publishing", "checks": {}, "run": self.run},
        ):
            run, error = views._merge_setup_pr_for_run(run=self.run, context=self.context)
        self.assertIsNotNone(run)
        self.assertIsNone(error)

    def test_wrapper_returns_409_on_manual_required(self):
        with self._patch_token(), mock.patch.object(
            views, "_attempt_setup_publish_merge",
            return_value={"outcome": "manual_required", "detail": "nope", "checks": {}, "run": self.run},
        ):
            run, error = views._merge_setup_pr_for_run(run=self.run, context=self.context)
        self.assertIsNone(run)
        self.assertEqual(error.status_code, 409)


class EnableNativeAutoMergeTests(SetupMergeBase):
    def test_enables_via_graphql_with_node_id(self):
        graphql_calls = []

        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            if method == "GET" and path.endswith(f"/pulls/{PR}"):
                return _open_pull(node_id="PR_node_99")
            raise AssertionError(f"unexpected {method} {path}")

        def fake_graphql(*, query, variables, token):
            graphql_calls.append(variables)
            return {"enablePullRequestAutoMerge": {"pullRequest": {"id": "PR_node_99", "number": PR}}}

        with mock.patch.object(views, "_github_api_request", side_effect=fake_api), mock.patch.object(
            views, "_github_graphql_request", side_effect=fake_graphql
        ):
            result = views._enable_native_auto_merge(repo=REPO, pr_number=PR, token="tok")

        self.assertEqual(result["status"], "enabled")
        self.assertEqual(graphql_calls[0]["pullRequestId"], "PR_node_99")
        self.assertEqual(graphql_calls[0]["mergeMethod"], "SQUASH")

    def test_graphql_error_maps_to_unavailable(self):
        def fake_api(method, path, *, token=None, body=None, expected=(200,)):
            return _open_pull(node_id="PR_node_99")

        with mock.patch.object(views, "_github_api_request", side_effect=fake_api), mock.patch.object(
            views, "_github_graphql_request", side_effect=ValueError("Auto-merge is not allowed for this repository")
        ):
            result = views._enable_native_auto_merge(repo=REPO, pr_number=PR, token="tok")

        self.assertEqual(result["status"], "unavailable")


class MaybeAutoPublishSetupTests(SetupMergeBase):
    def test_auto_publish_attempts_when_org_opted_in(self):
        self.config.auto_publish = True
        self.config.save(update_fields=["auto_publish"])
        with mock.patch.object(
            views, "_attempt_setup_publish_merge",
            return_value={"outcome": "merged", "detail": "ok", "checks": {}, "run": self.run},
        ) as attempt:
            views._maybe_auto_publish_setup(run=self.run, context=self.context)
        attempt.assert_called_once()

    def test_no_auto_publish_when_org_not_opted_in(self):
        with mock.patch.object(views, "_attempt_setup_publish_merge") as attempt:
            views._maybe_auto_publish_setup(run=self.run, context=self.context)
        attempt.assert_not_called()

    def test_no_auto_publish_when_already_publishing(self):
        self.config.auto_publish = True
        self.config.save(update_fields=["auto_publish"])
        self.run = self._setup_run(run_id="setup-2", status="publishing")
        # native auto-merge already enabled → must not re-attempt
        result = dict(self.run.result)
        result["article_system_setup"]["native_auto_merge_enabled"] = True
        result["article_system_setup"]["mergeStatus"] = "publishing"
        self.run.result = result
        self.run.save(update_fields=["result"])
        with mock.patch.object(views, "_attempt_setup_publish_merge") as attempt:
            views._maybe_auto_publish_setup(run=self.run, context=self.context)
        attempt.assert_not_called()
