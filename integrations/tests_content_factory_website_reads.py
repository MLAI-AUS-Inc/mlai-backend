"""Legacy app polling cannot grant ownership or restore revoked run authority."""

from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from content_factory.tests_website_connections import WebsiteDatabaseFixture
from content_factory.website_connections import transition_connection
from workflow_runs.models import ContentFactoryRun
from .api_views_content_factory_app import (
    ContentFactoryAppRunArtifactsView,
    ContentFactoryAppRunControlView,
    ContentFactoryAppRunView,
    _content_factory_request,
    _sync_remote_run_payload,
)


class WebsiteAppRunReadTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.run = ContentFactoryRun.objects.create(
            run_id="legacy-owned-run", workflow="direct_generate", domain=self.org.domain,
            organization=self.org, github_repo=self.website.github_repo,
            slack_user_id="test-actor", run_request=self.binding, status="running",
            result={"retained": "original history"},
        )
        self.request = SimpleNamespace(user=SimpleNamespace(pk=17), data={}, query_params={"refresh": "true"})
        actor_ids = patch("integrations.api_views_content_factory_app.actor_ids_for_user", return_value=["test-actor"])
        actor_ids.start()
        self.addCleanup(actor_ids.stop)

    def snapshot(self, **overrides):
        return {"workflow": self.run.workflow, "status": "completed", "domain": self.run.domain,
            "github_repo": self.run.github_repo, "run_request": self.binding,
            "result": {"new_worker_result": True}, **overrides}

    @staticmethod
    def response(payload):
        return SimpleNamespace(status_code=200, content=b"{}", json=lambda: payload)

    def test_refresh_cannot_resurrect_revoked_unversioned_run(self):
        transition_connection(self.config, action="disconnect", expected=self.binding)
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot())
        self.assertEqual(saved.status, "cancelled")
        self.assertEqual(saved.result, {"retained": "original history"})
        self.assertEqual(saved.run_request, self.binding)

    def test_reconnect_does_not_rebind_old_run_even_with_newer_execution_generation(self):
        transition_connection(self.config, action="disconnect", expected=self.binding)
        self.website.refresh_from_db()
        from content_factory.website_connections import contract_for
        transition_connection(self.config, action="reconnect", expected=contract_for(self.website), verified_reconnect=True)
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(generation=99, state_version=99))
        self.assertEqual(saved.status, "cancelled")
        self.assertEqual(saved.run_request, self.binding)
        self.assertNotIn("new_worker_result", saved.result)

    def test_current_snapshot_preserves_original_request_and_owner(self):
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(run_request={}))
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.run_request, self.binding)
        self.assertEqual(saved.slack_user_id, "test-actor")
        self.assertTrue(saved.result["new_worker_result"])

    def test_incoming_identity_and_binding_changes_are_not_adopted(self):
        for overrides in ({"slack_user_id": "other-owner"}, {"domain": "other.example.test"},
                          {"run_request": {**self.binding, "connection_generation": 99}},
                          {"workflow": "article_system_setup"}):
            with self.subTest(overrides=overrides):
                saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(**overrides))
                self.assertEqual(saved.status, "running")
                self.assertEqual(saved.run_request, self.binding)

    def test_unknown_remote_run_is_not_imported(self):
        self.assertIsNone(_sync_remote_run_payload("unknown-run", self.snapshot()))
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="unknown-run").exists())

    def test_run_and_artifact_reads_require_local_ownership_before_http(self):
        for view in (ContentFactoryAppRunView(), ContentFactoryAppRunArtifactsView()):
            for run_id in ("unknown-run", self.run.run_id):
                with self.subTest(view=type(view).__name__, run_id=run_id):
                    if run_id == self.run.run_id:
                        self.run.slack_user_id = "other-owner"
                        self.run.save(update_fields=["slack_user_id"])
                    with patch("integrations.api_views_content_factory_app._content_factory_request") as remote:
                        response = view.get(self.request, run_id)
                    self.assertEqual(response.status_code, 404)
                    remote.assert_not_called()

    def test_control_requires_local_ownership_before_http_or_delivery_change(self):
        for action in ("approve", "resume", "deny", "delivery-mode", "publish-pr"):
            with self.subTest(action=action), \
                    patch("integrations.api_views_content_factory_app._content_factory_request") as remote, \
                    patch("integrations.api_views_content_factory_app.set_article_delivery_mode") as delivery, \
                    patch("integrations.api_views_content_factory_app.publish_article_as_pr") as publish:
                response = ContentFactoryAppRunControlView().post(self.request, "unknown-run", action)
                self.assertEqual(response.status_code, 404)
                remote.assert_not_called()
                delivery.assert_not_called()
                publish.assert_not_called()

    def test_refresh_late_response_retains_cancelled_history(self):
        def disconnect_before_reply(*args, **kwargs):
            transition_connection(self.config, action="disconnect", expected=self.binding)
            return self.response(self.snapshot())
        with patch("integrations.api_views_content_factory_app._content_factory_request", side_effect=disconnect_before_reply):
            response = ContentFactoryAppRunView().get(self.request, self.run.run_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")

    def test_control_sparse_late_response_cannot_restore_running_status(self):
        def disconnect_before_reply(*args, **kwargs):
            transition_connection(self.config, action="disconnect", expected=self.binding)
            return self.response({"accepted": True})
        with patch("integrations.api_views_content_factory_app._content_factory_request", side_effect=disconnect_before_reply):
            response = ContentFactoryAppRunControlView().post(self.request, self.run.run_id, "resume")
        self.assertEqual(response.status_code, 409)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")

    def test_revoked_control_does_not_dispatch(self):
        transition_connection(self.config, action="disconnect", expected=self.binding)
        with patch("integrations.api_views_content_factory_app._content_factory_request") as remote:
            response = ContentFactoryAppRunControlView().post(self.request, self.run.run_id, "approve")
        self.assertEqual(response.status_code, 409)
        remote.assert_not_called()

    def test_original_portable_run_can_sync_content_only_result(self):
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.github_repo = ""
        self.run.save(update_fields=["run_request", "github_repo"])
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(run_request={}, result={"content_package": {"article_markdown": "Draft"}}))
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.run_request, {"delivery_mode": "content_only"})

    def test_incoming_portable_mode_cannot_bypass_original_consent(self):
        transition_connection(self.config, action="disconnect", expected=self.binding)
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(run_request={"delivery_mode": "content_only"}))
        self.assertEqual(saved.status, "cancelled")

    def test_portable_snapshot_cannot_claim_publication(self):
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.github_repo = ""
        self.run.save(update_fields=["run_request", "github_repo"])
        saved = _sync_remote_run_payload(self.run.run_id, self.snapshot(run_request={}, result={"pr_url": "https://github.com/example/site/pull/7"}))
        self.assertEqual(saved.status, "running")
        self.assertNotIn("pr_url", saved.result)

    def test_remote_transport_fails_before_http_inside_explicit_transaction(self):
        with transaction.atomic(), patch("integrations.api_views_content_factory_app.http_requests.get") as remote:
            with self.assertRaisesRegex(RuntimeError, "outside database transactions"):
                _content_factory_request("get", "/api/runs/test")
        remote.assert_not_called()
