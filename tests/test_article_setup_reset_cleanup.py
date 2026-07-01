"""Tests for article-setup reset behavior:

1. ``_delete_article_setup_scaffold_branches`` asks content-factory to cancel
   recent article_system_setup runs (which deletes their generated scaffold
   branches), is best-effort, and never raises.
2. A scan-detected *existing* article surface must NOT populate ``routePath`` in
   the article-setup state — only an explicit user selection / setup run does.
   This is what lets "Reset articles setup" return the wizard to a clean picker
   instead of a stale "scaffold is live" view.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from content_factory.article_setup_reset import reset_article_setup_config
from content_factory.models import OrganizationContentConfig, WrittenArticle
from content_factory.vibe_marketing_views import (
    _article_generation_history_exists,
    _article_setup_state_for_config,
    _delete_article_setup_scaffold_branches,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


class ArticleSetupResetBranchCleanupTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="statdoctor.app", name="StatDoctor")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="DrAnuG1995/website",
        )

    def _make_setup_run(self, run_id, *, status="completed", repo="DrAnuG1995/website"):
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=repo,
            status=status,
        )

    def test_cancels_recent_setup_runs_to_delete_branches(self):
        self._make_setup_run("setup-1")
        self._make_setup_run("setup-2")
        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_action",
            return_value={"status": "cancelled"},
        ) as call_mock:
            result = _delete_article_setup_scaffold_branches(self.config)
        self.assertEqual(result["status"], "requested")
        cancelled = {call.kwargs["run_id"] for call in call_mock.call_args_list}
        self.assertEqual(cancelled, {"setup-1", "setup-2"})
        for call in call_mock.call_args_list:
            self.assertEqual(call.kwargs["action"], "cancel")
            self.assertEqual(call.kwargs["workflow"], "article_system_setup")

    def test_ignores_other_repos_and_cancelled_runs(self):
        self._make_setup_run("other-repo", repo="someone/else")
        self._make_setup_run("already-cancelled", status=ContentFactoryRunStatus.CANCELLED)
        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_action",
            return_value={},
        ) as call_mock:
            result = _delete_article_setup_scaffold_branches(self.config)
        call_mock.assert_not_called()
        self.assertEqual(result["status"], "skipped")

    def test_best_effort_never_raises_on_relay_failure(self):
        self._make_setup_run("setup-x")
        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_run_action",
            side_effect=RuntimeError("relay down"),
        ):
            result = _delete_article_setup_scaffold_branches(self.config)
        self.assertEqual(result["status"], "requested")
        self.assertEqual(result["runs"][0]["status"], "error")

    def test_skips_when_repo_missing(self):
        org2 = Organization.objects.create(domain="norepo.example", name="NoRepo")
        config2 = OrganizationContentConfig.objects.create(organization=org2, github_repo="")
        result = _delete_article_setup_scaffold_branches(config2)
        self.assertEqual(result["status"], "skipped")


class ArticleSetupExistingSurfaceStateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="statdoctor.app", name="StatDoctor")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="DrAnuG1995/website",
        )

    def test_existing_scan_surface_does_not_set_route_path(self):
        # Scan detected an existing blog surface, but the user has not run setup:
        # there is no pending selection and no article_system_setup run.
        self.config.article_system = {
            "state": "existing",
            "path": "app/blog/[slug]/page.tsx",
            "route_template": "/blog/{slug}",
            "scan": {"status": "completed"},
        }
        self.config.save(update_fields=["article_system"])
        state = _article_setup_state_for_config(self.config)
        # The detected surface must not read as a saved/selected route, otherwise
        # the wizard reports setup as complete and reset appears to do nothing.
        self.assertFalse(state.get("routePath"), state.get("routePath"))
        self.assertFalse(state.get("setupRunId"), state.get("setupRunId"))

    def test_pending_user_selection_sets_route_path(self):
        self.config.article_system = {
            "state": "missing",
            "scan": {"status": "completed"},
            "pending_article_system_setup": {
                "routePath": "/blog",
                "route_path": "/blog",
            },
        }
        self.config.save(update_fields=["article_system"])
        state = _article_setup_state_for_config(self.config)
        self.assertEqual(state.get("routePath"), "/blog")

    def test_setup_run_hint_sets_route_path_when_pending_state_is_missing(self):
        self.config.article_system = {
            "state": "missing",
            "scan": {"status": "completed"},
            "pending_article_system_setup": {
                "setupRunId": "setup-with-route-hint",
            },
        }
        self.config.save(update_fields=["article_system"])

        ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id="setup-with-route-hint",
            workflow="article_system_setup",
            github_repo="DrAnuG1995/website",
            status=ContentFactoryRunStatus.BLOCKED,
            result={
                "article_surface_hint": {
                    "source": "user_input",
                    "route_path": "/articles",
                },
                "article_system_setup": {
                    "setup_run_id": "setup-with-route-hint",
                    "status": "failed",
                },
            },
        )

        state = _article_setup_state_for_config(self.config)

        self.assertEqual(state.get("setupRunId"), "setup-with-route-hint")
        self.assertEqual(state.get("routePath"), "/articles")


class GenerationHistorySurvivesResetTests(TestCase):
    """Pre-reset article history must not keep the (now-cleared) scaffold reported
    as "ready" — that hid the Build button after "Reset everything". `since` (the
    reset timestamp) restricts the WrittenArticle/run evidence to work done after
    the reset; the start-page caller leaves it None and still remembers history."""

    def setUp(self):
        self.org = Organization.objects.create(domain="statdoctor.app", name="StatDoctor")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="DrAnuG1995/website",
        )

    def _written_article(self, *, when):
        article = WrittenArticle.objects.create(
            organization=self.org,
            title="Old article",
            slug="old-article",
            category="guide",
            primary_keyword="old article",
        )
        WrittenArticle.objects.filter(pk=article.pk).update(created_at=when)
        return article

    def test_history_excluded_when_since_postdates_the_article(self):
        article_time = timezone.now() - timedelta(days=2)
        self._written_article(when=article_time)

        # No cutoff → history counts (e.g. start-page topic-picker still remembers).
        self.assertTrue(_article_generation_history_exists(self.org))
        # Reset stamped AFTER the article → excluded.
        self.assertFalse(
            _article_generation_history_exists(self.org, since=article_time + timedelta(days=1))
        )
        # Reset stamped BEFORE the article (i.e. a post-reset article) → counts again.
        self.assertTrue(
            _article_generation_history_exists(self.org, since=article_time - timedelta(days=1))
        )

    def test_article_setup_state_drops_generation_ready_after_reset(self):
        self._written_article(when=timezone.now() - timedelta(days=2))

        before = _article_setup_state_for_config(self.config, organization=self.org)
        self.assertTrue(before.get("generationReady"))

        reset_article_setup_config(self.config, github_repo="DrAnuG1995/website")
        self.config.refresh_from_db()

        after = _article_setup_state_for_config(self.config, organization=self.org)
        self.assertFalse(after.get("generationReady"))
