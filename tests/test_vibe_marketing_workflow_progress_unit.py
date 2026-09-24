"""Run-page workflow projection regressions without a database or migration."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from content_factory.vibe_marketing_views import _workflow_progress


def _article_progress(*, quality_status="", run_status="approval_required", packaged=True, run_scoped=True):
    result = {
        "status": "preview_ready",
        "promote_bundle_url": "/api/runs/article-1/promote-bundle",
    }
    if quality_status:
        result["article_preview_quality"] = {"status": quality_status}
    run = SimpleNamespace(
        pk=1,
        run_id="article-1",
        workflow="article_generation",
        domain="mlai.au",
        status=run_status,
        approval_state="approval_required",
        run_request={"delivery_mode": "content_only"},
        result=result,
        component_comments=SimpleNamespace(exists=lambda: False),
    )
    checks = {
        "websiteProfile": {"passed": True},
        "baseline": {"passed": False},
        "github": {"passed": True},
        "scaffold": {"passed": True, "generationReady": True},
        "research": {"passed": True},
        "write": {"passed": packaged},
    }
    with (
        patch(
            "content_factory.vibe_marketing_views._workflow_progress_context",
            return_value=(None, None, [run], checks),
        ),
        patch(
            "content_factory.vibe_marketing_views._content_package_from_run",
            return_value={"contentPackaged": packaged},
        ),
        patch(
            "content_factory.vibe_marketing_views._component_manifest_from_run",
            return_value={"components": [{"id": "hero"}]} if packaged else None,
        ),
        patch(
            "content_factory.vibe_marketing_views._component_feedback_from_run",
            return_value={"comments": [], "latestBatch": None},
        ),
        patch(
            "content_factory.vibe_marketing_views._publish_evidence_from_run",
            return_value={},
        ),
    ):
        return _workflow_progress(run=run if run_scoped else None, topic_candidates=[])


def _step(progress, step_id):
    return next(step for step in progress["steps"] if step["id"] == step_id)


class ArticleRunWorkflowProgressTests(TestCase):
    def test_organization_wizard_still_requires_its_baseline(self):
        progress = _article_progress(run_scoped=False)

        self.assertEqual(progress["currentStepId"], "baseline")
        self.assertEqual(_step(progress, "baseline")["status"], "ready")

    def test_run_page_prioritizes_review_without_erasing_the_real_baseline_requirement(self):
        progress = _article_progress()

        self.assertEqual(_step(progress, "baseline")["status"], "ready")
        self.assertEqual(_step(progress, "review")["status"], "ready")
        self.assertEqual(progress["currentStepId"], "review")
        self.assertEqual(_step(progress, "publish")["status"], "ready")

    def test_blocking_quality_finding_keeps_publish_unavailable(self):
        progress = _article_progress(quality_status="blocking_findings")

        self.assertEqual(progress["currentStepId"], "review")
        publish = _step(progress, "publish")
        self.assertEqual(publish["status"], "blocked")
        self.assertEqual(publish["primaryAction"]["label"], "Review preview findings")
        self.assertIn("articleStep=review", publish["href"])

    def test_quality_recheck_must_settle_before_publish_is_ready(self):
        for quality_status in ("queued", "running", "transient_findings"):
            with self.subTest(quality_status=quality_status):
                progress = _article_progress(quality_status=quality_status)
                self.assertEqual(_step(progress, "publish")["status"], "locked")
                self.assertEqual(progress["currentStepId"], "review")

        progress = _article_progress(quality_status="passed")
        self.assertEqual(_step(progress, "publish")["status"], "ready")

    def test_failed_run_prioritizes_its_generation_block(self):
        progress = _article_progress(run_status="failed", packaged=False)

        self.assertEqual(_step(progress, "baseline")["status"], "ready")
        self.assertEqual(_step(progress, "generate")["status"], "blocked")
        self.assertEqual(progress["currentStepId"], "generate")
