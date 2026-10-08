"""Failed setup approval projection with synthetic persistence; no DB/network."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import vibe_marketing_views as views


class SetupPrFailureTests(SimpleTestCase):
    def test_actual_approval_failure_is_visible_and_keeps_preview_and_authority(self):
        run = SimpleNamespace(pk=1, run_id="synthetic-setup", domain="fixture.test",
            workflow="article_system_setup", status="awaiting_approval", current_step="await_review",
            approval_state="approval_required", resume_available=False, error="",
            run_request={"operation_id": "original-operation", "operation_attempt": 2},
            result={"status": "preview_ready", "article_system_setup": {"status": "preview_ready"},
                    "live_preview": {"previewUrl": "https://preview.example/stories"}},
            save=Mock(), refresh_from_db=Mock())
        config = SimpleNamespace(article_system={"pending_article_system_setup": {
            "setupRunId": run.run_id, "status": "preview_ready", "routePath": "/stories"}}, save=Mock())
        original = deepcopy(run.run_request)
        context = SimpleNamespace(organization=SimpleNamespace(domain=run.domain))
        request = SimpleNamespace(data={}, user=SimpleNamespace(pk=7))
        failure = {"status": "setup_pr_create_failed", "approval_state": "approved",
                   "message": "This operation is terminal. Start a new reviewed attempt."}
        with patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
                patch.object(views, "get_object_or_404", return_value=run), \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(views, "_get_config", return_value=config), \
                patch.object(views, "founder_actor_id_for_user", return_value="synthetic-actor"), \
                patch.object(views, "_call_content_factory_run_action", return_value=failure), \
                patch.object(views, "_remote_response_write_guard", return_value=nullcontext()):
            response = views.VibeMarketingRunControlView.post.__wrapped__(
                views.VibeMarketingRunControlView(), request, run.run_id, "approve")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], failure["message"])
        self.assertEqual((run.status, run.current_step, run.approval_state),
                         ("blocked", "create_pull_request", "approved"))
        self.assertEqual(run.result["article_system_setup"]["status"], "setup_pr_create_failed")
        self.assertEqual(run.result["live_preview"]["previewUrl"], "https://preview.example/stories")
        self.assertEqual(run.run_request, original)
        self.assertEqual(config.article_system["pending_article_system_setup"]["routePath"], "/stories")
        self.assertEqual(config.article_system["pending_article_system_setup"]["status"], "setup_pr_create_failed")

    def test_failed_status_snapshot_supersedes_stale_nested_preview_phase(self):
        remote = {"status": "setup_pr_create_failed", "result": {
            "article_system_setup": {"status": "preview_ready", "preview_url": "https://preview.example"}}}
        original = deepcopy(remote)
        result = views._run_result_from_remote(remote)
        self.assertEqual(views._normalize_remote_run_status(remote["status"]), "blocked")
        self.assertEqual(result["article_system_setup"]["status"], "setup_pr_create_failed")
        self.assertEqual(result["article_system_setup"]["preview_url"], "https://preview.example")
        self.assertEqual(remote, original)

    def test_failure_cannot_replace_a_newer_pending_setup(self):
        run = SimpleNamespace(run_id="old-setup", result={}, save=Mock())
        config = SimpleNamespace(article_system={"pending_article_system_setup": {
            "setupRunId": "new-setup", "status": "running"}}, save=Mock())
        views._project_setup_pr_creation_failure(run, config, {"status": "setup_pr_create_failed"})
        self.assertEqual(config.article_system["pending_article_system_setup"]["status"], "running")
        config.save.assert_not_called()

    def test_legacy_failed_approval_is_projected_without_rewriting_history(self):
        run = SimpleNamespace(workflow="article_system_setup", status="queued", current_step="await_review",
            approval_state="approved", resume_available=True, error="",
            result={"status": "preview_ready", "article_system_setup": {"status": "preview_ready"},
                    "live_preview": {"previewUrl": "https://preview.example/stories"},
                    "latest_control_response": {"status": "setup_pr_create_failed",
                                                "message": "Repository branch changed"}}, save=Mock())
        before = deepcopy(run.result)
        projected = views._setup_pr_creation_failure_view(run)
        self.assertIsNot(projected, run)
        self.assertEqual((projected.status, projected.current_step), ("blocked", "create_pull_request"))
        self.assertFalse(projected.resume_available)
        self.assertEqual(projected.result["setup_status"], "setup_pr_create_failed")
        self.assertEqual(projected.result["live_preview"]["previewUrl"], "https://preview.example/stories")
        self.assertEqual(run.result, before)
        self.assertEqual((run.status, run.current_step), ("queued", "await_review"))
        run.save.assert_not_called()

    def test_created_pr_cannot_be_hidden_by_an_older_failed_control(self):
        for scope in ("result", "setup"):
            with self.subTest(scope=scope):
                result = {"latest_control_response": {"status": "setup_pr_create_failed"}}
                target = result if scope == "result" else result.setdefault("article_system_setup", {})
                target["pr_url"] = "https://github.com/owner/site/pull/1"
                run = SimpleNamespace(workflow="article_system_setup", approval_state="approved", result=result)
                self.assertIs(views._setup_pr_creation_failure_view(run), run)

    def test_new_control_or_unapproved_state_does_not_restore_old_failure(self):
        for control_status, approval in (("queued", "approved"), ("setup_pr_create_failed", "approval_required")):
            with self.subTest(control_status=control_status, approval=approval):
                run = SimpleNamespace(workflow="article_system_setup", approval_state=approval,
                    result={"latest_control_response": {"status": control_status}})
                self.assertIs(views._setup_pr_creation_failure_view(run), run)
