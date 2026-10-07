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


class ArticleSetupWorkflowProgressTests(TestCase):
    def test_failed_setup_stays_at_build_until_a_reviewable_preview_exists(self):
        for setup_status in ("failed", "blocked", "preview_failed"):
            with self.subTest(setup_status=setup_status):
                checks = {
                    key: {"passed": True}
                    for key in ("websiteProfile", "baseline", "github")
                }
                checks["scaffold"] = {
                    "passed": False,
                    "setupBlocked": True,
                    "setupRunId": "failed-setup",
                    "setupStatus": setup_status,
                }
                with patch(
                    "content_factory.vibe_marketing_views._workflow_progress_context",
                    return_value=(None, None, [], checks),
                ):
                    progress = _workflow_progress(topic_candidates=[])
                build = _step(progress, "generate")
                self.assertEqual(build["status"], "blocked")
                self.assertEqual(build["runId"], "failed-setup")
                self.assertEqual(build["primaryAction"]["label"], "Open setup diagnostics")
                self.assertIn("failed-setup", build["primaryAction"]["href"])
                self.assertEqual(progress["currentStepId"], "generate")
                for key in ("review", "publish"):
                    self.assertEqual(_step(progress, key)["status"], "locked")
                    self.assertIsNone(_step(progress, key)["primaryAction"])


class DiscoveryTopicSelectionTests(TestCase):
    """A reviewed source run survives keyword deduplication without bypassing availability."""

    def resolve(self, *, declined=False, keyword_status=None, source_exists=True, requested_source="fresh-discovery"):
        from contextlib import ExitStack
        from content_factory import vibe_marketing_views as views

        organization = SimpleNamespace(pk=7, domain="fixture.example")
        source = SimpleNamespace(
            run_id="fresh-discovery", workflow="auto_discovery", status="awaiting_confirmation",
            run_request={}, result={"topic_candidates": [{
                "id": "0", "keyword": "team workflows", "title": "A fresh workflow guide",
                "volume": 1000, "difficulty": 10, "difficulty_source": "dataforseo_labs",
                "opportunityScore": 100,
            }]},
        )
        keyword = SimpleNamespace(status=keyword_status, written_article_id=None, written_article=None, cooldown_until=None) if keyword_status else None
        memory = {"keywords": {"team workflows": keyword} if keyword else {},
                  "written_by_keyword": {}, "written_by_slug": {}}
        with ExitStack() as stack:
            query = stack.enter_context(patch.object(views.ContentFactoryRun.objects, "filter"))
            query.return_value.first.return_value = source if source_exists else None
            pool = stack.enter_context(patch.object(views, "_topic_selection_candidate_pool", side_effect=AssertionError("Historical keyword merge replaced the reviewed run")))
            stack.enter_context(patch.object(views, "list_topic_feedback", return_value=[SimpleNamespace(keyword="team workflows")] if declined else []))
            stack.enter_context(patch.object(views, "_written_topic_memory", return_value=memory))
            stack.enter_context(patch.object(views, "build_topic_coverage_memory", return_value={}))
            stack.enter_context(patch.object(views, "match_covered_topic", return_value=None))
            result = views._resolve_topic_selection_candidate(
                organization, SimpleNamespace(), f"topic:run:{requested_source}:team-workflows",
                submitted={"source_run_id": "fresh-discovery", "target_keyword": "team workflows",
                           "selected_title": "A fresh workflow guide"},
            )
            pool.assert_not_called()
            if requested_source == "fresh-discovery":
                query.assert_called_once_with(
                    organization=organization, domain=organization.domain, run_id="fresh-discovery",
                    workflow__in=views.DISCOVERY_WORKFLOWS, status__in=views.DISCOVERY_TOPIC_CANDIDATE_STATUSES,
                )
            else:
                query.assert_not_called()
            return result

    def test_fresh_reviewed_title_and_source_survive_historical_keyword_merge(self):
        selected = self.resolve()
        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], "topic:run:fresh-discovery:team-workflows")
        self.assertEqual(selected["sourceRunId"], "fresh-discovery")
        self.assertEqual(selected["title"], "A fresh workflow guide")

    def test_declined_topic_remains_unavailable(self):
        self.assertIsNone(self.resolve(declined=True))

    def test_active_skipped_or_written_keyword_remains_unavailable(self):
        for state in ("in_progress", "skipped", "written"):
            with self.subTest(state=state):
                self.assertIsNone(self.resolve(keyword_status=state))

    def test_missing_or_other_tenant_source_cannot_fall_back_to_historical_topic(self):
        self.assertIsNone(self.resolve(source_exists=False))

    def test_requested_source_must_match_reviewed_source(self):
        self.assertIsNone(self.resolve(requested_source="another-discovery"))
