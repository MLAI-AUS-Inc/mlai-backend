"""Run-scoped portable progress tests with synthetic seams; no DB or network."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import vibe_marketing_views as views


class PortableWorkflowProgressTests(SimpleTestCase):
    def run_progress(self, *, status="completed", request=None, bound_repo="", scoped=True):
        run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo=bound_repo,
            workflow="article_generation", status=status, current_step="finalize", result={},
            run_request=request if request is not None else {
                "delivery_mode": "content_only", "delivery_mode_confirmed": True,
            },
            component_comments=Mock(),
        )
        checks = {name: {"passed": True} for name in ("websiteProfile", "baseline", "github", "research")}
        checks["scaffold"] = {
            "passed": False, "setupBlocked": True, "setupRunId": "separate-website-setup",
            "setupStatus": "blocked",
        }
        patches = {
            "_workflow_progress_context": (None, None, [run], checks),
            "_latest_publish_child_run": None,
            "_accepted_revision_source_run": None,
            "_publish_evidence_from_run": {},
            "_content_package_from_run": {"contentPackaged": True, "title": "Saved draft"},
            "_component_feedback_from_run": {"comments": [], "latestBatch": None},
            "_source_accepts_revision": False,
            "_run_can_promote_package": False,
            "_publish_handoff_stale_for_run": False,
            "_publish_child_run_recoverable": False,
            "_component_manifest_from_run": {"components": [{"id": "title"}]},
        }
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(views, name, return_value=value))
            stack.enter_context(patch.object(views, "_latest_run_matching", side_effect=lambda rows, workflows: run if run.workflow in workflows else None))
            result = views._workflow_progress(run=run if scoped else None, topic_candidates=[])
        return result, {step["id"]: step for step in result["steps"]}

    def test_completed_original_portable_run_keeps_its_own_review_without_unlocking_publication(self):
        result, steps = self.run_progress()
        self.assertEqual(result["currentStepId"], "review")
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "ready")
        self.assertEqual(steps["package"]["status"], "complete")
        for name in ("generate", "review", "package"):
            self.assertEqual(steps[name]["runId"], "synthetic-draft")
            self.assertIn("synthetic-draft", steps[name]["href"])
            self.assertNotIn("setup", steps[name]["summary"].lower())
        self.assertEqual(steps["publish"]["status"], "blocked")
        self.assertEqual(steps["publish"]["primaryAction"]["label"], "Configure publishing")
        self.assertEqual(steps["automation"]["status"], "locked")

    def test_failed_original_portable_run_keeps_its_own_failure(self):
        result, steps = self.run_progress(status="failed")
        self.assertEqual(result["currentStepId"], "generate")
        self.assertEqual(steps["generate"]["status"], "blocked")
        self.assertEqual(steps["generate"]["runId"], "synthetic-draft")
        self.assertIn("synthetic-draft", steps["generate"]["href"])

    def test_unconfirmed_bound_or_promoted_intent_cannot_hide_website_failure(self):
        for request, repo in (
            ({"delivery_mode": "content_only"}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": False}, ""),
            ({"delivery_mode": "publish_code", "delivery_mode_confirmed": True}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True}, "owner/site"),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True, "repository_id": 42}, ""),
            ({"delivery_mode": "content_only", "delivery_mode_confirmed": True, "website_connection_id": "connection"}, ""),
        ):
            with self.subTest(request=request, repo=repo):
                _, steps = self.run_progress(request=request, bound_repo=repo)
                self.assertEqual(steps["review"]["status"], "blocked")
                self.assertEqual(steps["review"]["runId"], "separate-website-setup")
                self.assertEqual(steps["package"]["status"], "locked")

    def test_company_overview_keeps_real_setup_failure_despite_portable_history(self):
        _, steps = self.run_progress(scoped=False)
        self.assertEqual(steps["review"]["status"], "blocked")
        self.assertEqual(steps["review"]["runId"], "separate-website-setup")
