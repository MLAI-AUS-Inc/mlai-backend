"""A current preparation may read build progress without creating a preview."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_preview_progress as progress


class PreviewBuildProgressTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk="11111111-1111-4111-8111-111111111111", generation=7,
            configuration_version=7, state="connected", organization_id=3, repository_id=123,
            github_repo="example/site", branch="main", app_root="")
        self.op = SimpleNamespace(action="prepare", state="pending", generation=7,
            receipt={"child_run_id": "original-child"})
        self.run = SimpleNamespace(status="running", run_request={"website_connection_id": self.connection.pk,
            "connection_generation": 7, "repository_id": 123, "github_repo": "example/site", "branch": "main", "app_root": ""})
        self.payload = {"runId": "original-child", "phase": "building", "builderRunId": "123",
            "url": "https://github.com/service/builder/actions/runs/123",
            "lastProgressAt": "2026-01-01T00:00:00Z", "observedAt": "2026-01-01T00:01:00Z",
            "provider_body": "secret"}
        self.rows = self.enterContext(patch("workflow_runs.models.ContentFactoryRun.objects.filter"))
        self.rows.return_value.first.return_value = self.run
        self.cached = self.enterContext(patch.object(progress.cache, "get", return_value=None))
        self.enterContext(patch.object(progress.cache, "add", return_value=True))
        self.store = self.enterContext(patch.object(progress.cache, "set"))
        self.enterContext(patch.object(progress.cache, "delete"))
        self.enterContext(patch("content_factory.vibe_marketing_views._content_factory_remote_config",
            return_value={"enabled": True, "base_url": "https://worker.example"}))
        self.enterContext(patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "synthetic"}))
        self.get = self.enterContext(patch("integrations.http_client.get",
            return_value=Mock(status_code=200, json=lambda: self.payload)))

    def test_exact_current_child_uses_only_observation_endpoint_and_bounded_fields(self):
        result = progress.prepare_build_progress(self.op, self.connection)
        self.assertEqual(result["phase"], "building")
        self.assertNotIn("secret", str(result))
        self.get.assert_called_once_with("https://worker.example/api/runs/original-child/preview-build-progress",
            headers={"X-API-Key": "synthetic"}, timeout=(3, 6))
        self.rows.assert_called_once_with(run_id="original-child", organization_id=3, workflow="article_system_setup",
            run_request__website_connection_id=self.connection.pk, run_request__connection_generation__in=[7, "7"])
        self.cached.return_value = result
        self.assertEqual(progress.prepare_build_progress(self.op, self.connection), result)
        self.get.assert_called_once()

    def test_old_or_terminal_preparation_and_foreign_child_do_not_contact_worker(self):
        for changed in ("generation", "terminal", "repository", "branch", "child_generation"):
            with self.subTest(changed=changed):
                self.op.generation, self.op.state = 7, "pending"
                self.run.run_request.update(repository_id=123, branch="main", connection_generation=7)
                if changed == "generation":
                    self.op.generation = 6
                elif changed == "terminal":
                    self.op.state = "completed"
                elif changed == "child_generation":
                    self.run.run_request["connection_generation"] = 6
                else:
                    self.run.run_request["repository_id" if changed == "repository" else "branch"] = 456 if changed == "repository" else "other"
                self.assertIsNone(progress.prepare_build_progress(self.op, self.connection))
        self.get.assert_not_called()

    def test_current_string_generation_is_a_valid_original_binding(self):
        self.run.run_request["connection_generation"] = "7"
        self.assertEqual(progress.prepare_build_progress(self.op, self.connection)["phase"], "building")

    def test_old_cf_or_unconfirmed_response_never_falls_back_to_live_preview_get(self):
        for changed in ("old_server", "foreign_run", "unsafe_url", "unknown_phase"):
            with self.subTest(changed=changed):
                self.get.return_value.status_code = 404 if changed == "old_server" else 200
                saved = dict(self.payload)
                if changed == "foreign_run":
                    self.payload["runId"] = "another-child"
                elif changed == "unsafe_url":
                    self.payload["url"] += "?token=secret"
                elif changed == "unknown_phase":
                    self.payload["phase"] = "ready"
                self.assertIsNone(progress.prepare_build_progress(self.op, self.connection))
                self.payload = saved
        self.assertTrue(all(call.args[0].endswith("/preview-build-progress") for call in self.get.call_args_list))
