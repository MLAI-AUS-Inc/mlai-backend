"""Actual control/poll seams with synthetic persistence; no DB or network."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import vibe_marketing_views as views
from .portable_drafts import portable_run_control_allowed
from .website_contract import WebsiteAuthorityError


def draft_run():
    return SimpleNamespace(
        pk=1, run_id="synthetic-paid-draft", domain="publisher.example", github_repo="",
        workflow="article_generation", status="failed", current_step="plan_article",
        error="Original corpus diagnostic", approval_state="not_required", resume_available=True,
        run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True,
                     "roo_points_ledger_id": "synthetic-charge", "roo_points_cost": 6},
        result={"failure": {"code": "CORPUS_INCOMPLETE"}},
        save=Mock(), refresh_from_db=Mock(),
    )


class PortableRunControlTests(SimpleTestCase):
    def test_editorial_controls_use_original_intent_and_reject_authority_claims(self):
        run = draft_run()
        for action in ("resume", "cancel", "deny", "revise", "regenerate-image", "regenerate-images"):
            self.assertTrue(portable_run_control_allowed(run, action, {}))
        for action in ("approve", "publish-pr", "promote-bundle", "merge-publish-pr", "retry-preview-quality", "unknown"):
            self.assertFalse(portable_run_control_allowed(run, action, {}))
        for payload in (
            {"deliveryMode": "publish_code"}, {"github_repo": "other/site"},
            {"domain": "other.example"}, {"connectionGeneration": 3},
            {"result": {"defaultPublishTargetId": "native"}},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(portable_run_control_allowed(run, "resume", payload))
        run.run_request["connection_generation"] = 3
        self.assertFalse(portable_run_control_allowed(run, "resume", {}))
        run.run_request["website_connection_id"] = "fbc09c73-e449-4c43-88ea-385b249a7a20"
        self.assertFalse(portable_run_control_allowed(run, "resume", {}))
        run.run_request = {"delivery_mode": "content_only"}
        self.assertFalse(portable_run_control_allowed(run, "resume", {}))

    def test_actual_portable_resume_dispatches_without_repository_guard(self):
        run = draft_run()
        original = deepcopy(run.run_request)
        response = SimpleNamespace(status_code=202, content=b"{}", json=lambda: {"status": "queued", "run_id": run.run_id})
        with patch.object(views.ContentFactoryRun.objects, "filter") as query, \
                patch.object(views, "scoped_run_contract", side_effect=AssertionError("No website authority")), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://factory.example"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views.http_client, "post", return_value=response) as post:
            query.return_value.first.return_value = run
            result = views._call_content_factory_run_action(run_id=run.run_id, action="resume", payload={"request_source": "synthetic"})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(post.call_args.args[0], f"https://factory.example/api/runs/{run.run_id}/resume")
        self.assertEqual(post.call_args.kwargs["json"], {"request_source": "synthetic"})
        self.assertEqual(run.run_request, original)

    def test_bound_or_promoted_dispatch_keeps_repository_guard(self):
        for action, changes, payload in (
            ("resume", {"website_connection_id": "fbc09c73-e449-4c43-88ea-385b249a7a20", "connection_generation": 2}, {}),
            ("publish-pr", {}, {}), ("resume", {}, {"delivery_mode": "publish_code"}),
        ):
            run = draft_run()
            run.run_request.update(changes)
            with self.subTest(action=action, changes=changes, payload=payload), \
                    patch.object(views.ContentFactoryRun.objects, "filter") as query, \
                    patch.object(views, "scoped_run_contract", side_effect=WebsiteAuthorityError("website_connection_required", "Reconnect.")), \
                    patch.object(views.http_client, "post") as post:
                query.return_value.first.return_value = run
                result = views._call_content_factory_run_action(run_id=run.run_id, action=action, payload=payload)
                self.assertEqual(result["content_factory_status_code"], 409)
                self.assertFalse(result["allowed"])
                post.assert_not_called()

    def control(self, run, response):
        context = SimpleNamespace(organization=SimpleNamespace(domain=run.domain))
        request = SimpleNamespace(data={}, user=SimpleNamespace(pk=7))
        with patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
                patch.object(views, "get_object_or_404", return_value=run), \
                patch.object(views, "_run_belongs_to_context", return_value=True), \
                patch.object(views, "_get_config", return_value=SimpleNamespace()), \
                patch.object(views, "_setup_blocked_response_for_generation", return_value=None), \
                patch.object(views, "founder_actor_id_for_user", return_value="synthetic-actor"), \
                patch.object(views, "_call_content_factory_run_action", return_value=response), \
                patch.object(views, "_remote_response_write_guard", return_value=nullcontext()), \
                patch.object(views, "_serialize_run", side_effect=lambda value, **kw: {"status": value.status}):
            return views.VibeMarketingRunControlView.post.__wrapped__(views.VibeMarketingRunControlView(), request, run.run_id, "resume")

    def test_rejected_controls_never_become_queued_or_change_original_paid_intent(self):
        for rejection in (
            {"status": "blocked", "allowed": False, "code": "website_connection_required", "detail": "Reconnect.", "content_factory_status_code": 409},
            {"error": "Worker unavailable", "content_factory_status_code": 503},
            {"status": "noop", "message": "No resumable required step."},
        ):
            run = draft_run()
            original = deepcopy(run.run_request)
            with self.subTest(rejection=rejection):
                response = self.control(run, rejection)
                self.assertIn(response.status_code, (409, 503))
                self.assertEqual(run.status, "failed")
                self.assertEqual(run.error, "Original corpus diagnostic")
                self.assertEqual(run.run_request, original)
                run.save.assert_not_called()

    def test_real_queued_acknowledgement_updates_status_without_rebilling(self):
        run = draft_run()
        original = deepcopy(run.run_request)
        response = self.control(run, {"status": "queued", "run_id": run.run_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.run_request, original)
        run.save.assert_called_once()

    def test_portable_poll_accepts_safe_snapshot_and_preserves_version_fence(self):
        run = draft_run()
        run.result = {"generation": 1, "state_version": 3}
        snapshot = {"workflow": "confirmed_topic", "domain": run.domain, "status": "failed", "generation": 1, "state_version": 3}
        with patch.object(views, "scoped_run_contract", side_effect=AssertionError("No repository guard")), \
                patch.object(views.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as query, \
                patch.object(views, "_sync_local_run_from_remote_locked", return_value=run) as sync:
            query.return_value.get.return_value = run
            self.assertIs(views._sync_local_run_from_remote(run, snapshot), run)
            sync.assert_called_once_with(run, snapshot)
            sync.reset_mock()
            self.assertIs(views._sync_local_run_from_remote(run, {**snapshot, "state_version": 2}), run)
            sync.assert_not_called()

    def test_portable_poll_cannot_adopt_website_delivery_from_worker(self):
        run = draft_run()
        with patch.object(views, "scoped_run_contract", side_effect=WebsiteAuthorityError("website_connection_required", "Reconnect.")), \
                patch.object(views, "_sync_local_run_from_remote_locked") as sync:
            self.assertIs(views._sync_local_run_from_remote(run, {"result": {"pr_url": "https://github.com/example/site/pull/1"}}), run)
            sync.assert_not_called()
