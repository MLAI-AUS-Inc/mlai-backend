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
        self.assertEqual(steps["publish"]["status"], "locked")
        self.assertIsNone(steps["publish"]["primaryAction"])
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


class PortablePreviewTests(SimpleTestCase):
    def setUp(self):
        self.run = SimpleNamespace(
            pk=1, run_id="synthetic-draft", domain="publisher.example", github_repo="",
            workflow="article_generation", status="completed",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True},
            result={},
        )

    def artifacts(self, package=True, manifest=True):
        stack = ExitStack()
        stack.enter_context(patch.object(views, "_content_package_from_run", return_value={"contentPackaged": package}))
        stack.enter_context(patch.object(views, "_component_manifest_from_run", return_value={"components": [{"id": "title"}]} if manifest else None))
        return stack

    def test_private_portable_preview_never_mints_repository_authority(self):
        with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")):
            self.assertEqual(views._live_preview_github_token_payload(self.run), {})

    def test_incomplete_bound_unconfirmed_and_nonarticle_runs_keep_normal_authority_guard(self):
        for changes, intent in (
            ({"status": "running"}, {}),
            ({"github_repo": "owner/site"}, {}),
            ({"workflow": "article_system_setup"}, {}),
            ({}, {"delivery_mode_confirmed": False}),
            ({}, {"website_connection_id": "connection"}),
        ):
            with self.subTest(changes=changes, intent=intent):
                run = SimpleNamespace(**{**vars(self.run), **changes, "run_request": {**self.run.run_request, **intent}})
                with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=views.WebsiteAuthorityError("connection_required", "Reconnect.")) as authority:
                    with self.assertRaises(views.WebsiteAuthorityError):
                        views._live_preview_github_token_payload(run)
                    authority.assert_called_once_with(run)
        for package, manifest in ((False, True), (True, False)):
            with self.artifacts(package, manifest), patch.object(views, "scoped_run_contract", side_effect=views.WebsiteAuthorityError("connection_required", "Reconnect.")):
                with self.assertRaises(views.WebsiteAuthorityError):
                    views._live_preview_github_token_payload(self.run)

    def test_actual_preview_post_cannot_forward_local_checkout_or_credentials_for_portable_copy(self):
        request = SimpleNamespace(data={"force": True, "localRepoPath": "/private/checkout", "github_token": "untrusted"})
        view = views.VibeMarketingRunLivePreviewView()
        from . import website_views
        with self.artifacts(), patch.object(website_views, "_context", return_value=(object(), None, None)), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(view, "_resolve_run", return_value=(object(), self.run, None)), \
                patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_call_content_factory_live_preview", return_value={"available": True}) as dispatch, \
                patch.object(view, "_persist_preview", return_value=self.run), \
                patch.object(views, "_serialize_run", return_value={"runId": self.run.run_id}):
            runs.return_value.first.return_value = self.run
            result = view.post(request, self.run.run_id)
        self.assertEqual(result.status_code, 200)
        dispatch.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": True, "local_repo_path": ""})

    def test_automatic_private_preview_retains_the_exact_run_and_uses_no_repository_authority(self):
        with self.artifacts(), patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository authority")), \
                patch.object(views, "_article_preview_should_refresh", return_value=False), \
                patch.object(views, "_article_preview_should_auto_prepare", return_value=True), \
                patch.object(views, "_call_content_factory_live_preview", return_value={"available": True}) as dispatch, \
                patch.object(views, "_persist_live_preview_payload", return_value=self.run):
            self.assertIs(views._ensure_article_live_preview(self.run), self.run)
        dispatch.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": False})


    def test_foreign_portable_preview_cannot_dispatch_or_resolve_saved_artifacts(self):
        from . import website_views
        request = SimpleNamespace(data={})
        view = views.VibeMarketingRunLivePreviewView()
        with patch.object(website_views, "_context", return_value=(object(), None, None)), \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_run_belongs_to_context", return_value=False), \
                patch.object(view, "_resolve_run") as resolve, \
                patch.object(views, "_call_content_factory_live_preview") as dispatch:
            runs.return_value.first.return_value = self.run
            result = view.post(request, self.run.run_id)
        self.assertEqual(result.status_code, 404)
        resolve.assert_not_called()
        dispatch.assert_not_called()
