from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from integrations import http_client
from content_factory.models import KeywordStatus, OrganizationContentConfig, ResearchedKeyword, VibeMarketingComponentComment
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStatus
from content_factory.vibe_marketing_views import _call_content_factory_live_preview


User = get_user_model()


class _Response(SimpleNamespace):
    @property
    def content(self):
        return b"{}"

    def json(self):
        return self.payload


class VibeMarketingComponentCommentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-comments@example.com",
            password="password",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au")
        self.run = ContentFactoryRun.objects.create(
            run_id="article-run-comments",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={},
        )
        self.client.force_authenticate(user=self.user)

    def test_completed_article_run_auto_prepares_live_preview(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                        "selector": '[data-cf-component-id="title"]',
                    }
                ]
            }
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {
            "available": True,
            "status": "running",
            "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
            "exactRender": True,
            "inspectorProtocolVersion": 2,
            "inspectorMode": "comment",
            "verificationSkippedForPreview": True,
            "failedPhase": "verify",
            "failedCommand": "bun run typecheck",
            "logExcerpt": "[verify] command: bun run typecheck\nerror: script exited",
            "proofWarnings": ["Vite optimized dependencies during preview proof."],
            "browserWarnings": ["Failed to fetch dynamically imported module: /app/entry.client.tsx"],
            "assetWarnings": ["http://127.0.0.1:4321/node_modules/.vite/deps/react-dom_client.js"],
            "proofAttempts": [{"attempt": 1, "exact": True}],
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="POST", payload={"force": False})
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])
        self.assertTrue(response.data["livePreview"]["verificationSkippedForPreview"])
        self.assertEqual(response.data["livePreview"]["failedPhase"], "verify")
        self.assertEqual(response.data["livePreview"]["failedCommand"], "bun run typecheck")
        self.assertIn("script exited", response.data["livePreview"]["logExcerpt"])
        self.assertEqual(response.data["livePreview"]["proofWarnings"], preview_payload["proofWarnings"])
        self.assertEqual(response.data["livePreview"]["browserWarnings"], preview_payload["browserWarnings"])
        self.assertEqual(response.data["livePreview"]["assetWarnings"], preview_payload["assetWarnings"])
        self.assertEqual(response.data["livePreview"]["proofAttempts"], preview_payload["proofAttempts"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(self.run.result["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])
        self.assertEqual(self.run.result["livePreview"]["failedCommand"], "bun run typecheck")

    def test_completed_article_run_auto_prepare_forwards_org_github_token(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "org-live-preview-token"
        config.save(update_fields=["github_token_encrypted", "updated_at"])
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            }
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {"available": False, "status": "starting", "previewUrl": ""}

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={"force": False, "github_token": "org-live-preview-token"},
        )

    def test_completed_article_run_refreshes_starting_preview_failure(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": False,
                "status": "starting",
                "previewUrl": "",
                "error": "",
                "errorCode": "preview_start_timeout",
                "retryable": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])

        preview_payload = {
            "available": False,
            "status": "failed",
            "previewUrl": "",
            "error": "Preview process was terminated by SIGKILL. This often indicates the container ran out of memory.",
            "errorCode": "dev_server_startup_failed",
            "retryable": True,
            "failedPhase": "preview",
            "failedCommand": "bun run dev -- --host 127.0.0.1 --port 40547",
            "logExcerpt": 'error: script "dev" was terminated by signal SIGKILL',
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="GET")
        self.assertEqual(response.data["livePreview"]["status"], "failed")
        self.assertEqual(response.data["livePreview"]["errorCode"], "dev_server_startup_failed")
        self.assertIn("SIGKILL", response.data["livePreview"]["logExcerpt"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["status"], "failed")

    def test_running_article_run_does_not_auto_prepare_live_preview(self):
        self.run.status = ContentFactoryRunStatus.RUNNING
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            }
        }
        self.run.save(update_fields=["status", "result", "updated_at"])

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        self.assertFalse(response.data["livePreview"]["previewUrl"])

    def test_completed_article_run_with_ready_preview_does_not_restart_preview(self):
        preview_url = "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1"
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": True,
                "status": "running",
                "previewUrl": preview_url,
                "exactRender": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_url)

    def test_completed_article_run_refreshes_running_live_preview(self):
        self.run.result = {
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": False,
                "status": "running",
                "previewUrl": "",
                "exactRender": True,
            },
        }
        self.run.save(update_fields=["result", "updated_at"])
        preview_payload = {
            "available": True,
            "status": "ready",
            "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
            "exactRender": True,
            "inspectorProtocolVersion": 2,
            "inspectorMode": "comment",
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(run_id=self.run.run_id, method="GET")
        expected_preview_url = (
            f"{settings.DEFAULT_BACKEND_URL}/api/v1/vibe-marketing/runs/{self.run.run_id}"
            "/live-preview/proxy/articles/featured/generated?cfInspector=1"
        )
        self.assertEqual(response.data["livePreview"]["previewUrl"], expected_preview_url)
        self.assertEqual(response.data["livePreview"]["internalPreviewUrl"], preview_payload["previewUrl"])

    def test_live_preview_proxy_forwards_authenticated_run_asset_request(self):
        remote_response = SimpleNamespace(
            status_code=200,
            content=b"console.log('preview')",
            headers={"Content-Type": "text/javascript", "Content-Length": "999", "X-Preview": "ok"},
        )

        with (
            patch(
                "content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "http://content-factory-web:8000"},
            ),
            patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "test-key"}),
            patch("content_factory.vibe_marketing_views.http_client.request", return_value=remote_response) as request_call,
        ):
            response = self.client.get(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}"
                "/live-preview/proxy/node_modules/.vite/deps/react-dom_client.js?v=test",
                HTTP_ACCEPT="text/javascript",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"console.log('preview')")
        self.assertEqual(response["Content-Type"], "text/javascript")
        self.assertEqual(response["X-Preview"], "ok")
        request_call.assert_called_once()
        args, kwargs = request_call.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(
            args[1],
            f"http://content-factory-web:8000/api/runs/{self.run.run_id}"
            "/live-preview/proxy/node_modules/.vite/deps/react-dom_client.js?v=test",
        )
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-key")

    def test_remote_not_started_preview_does_not_overwrite_local_failed_preview(self):
        manifest = {
            "components": [
                {
                    "id": "title",
                    "type": "title",
                    "label": "Title",
                }
            ]
        }
        self.run.result = {
            "componentManifest": manifest,
            "livePreview": {
                "available": False,
                "status": "failed",
                "previewUrl": "",
                "error": "Missing required mlai.au featured components in catalog.",
            },
        }
        self.run.save(update_fields=["result", "updated_at"])
        remote_data = {
            "run_id": self.run.run_id,
            "workflow": self.run.workflow,
            "status": ContentFactoryRunStatus.COMPLETED,
            "result": {
                "componentManifest": manifest,
                "livePreview": {
                    "available": False,
                    "status": "not_started",
                    "previewUrl": "",
                    "error": "",
                },
            },
        }

        with (
            patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value=remote_data),
            patch("content_factory.vibe_marketing_views._call_content_factory_live_preview") as preview_call,
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        preview_call.assert_not_called()
        self.assertEqual(response.data["livePreview"]["status"], "failed")
        self.assertIn("Missing required", response.data["livePreview"]["error"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["livePreview"]["status"], "failed")

    def test_live_preview_retry_posts_force_restart(self):
        preview_payload = {
            "available": False,
            "status": "failed",
            "previewUrl": "",
            "error": "Preview failed.",
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview",
                {"force": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={"force": True, "local_repo_path": ""},
        )
        self.assertEqual(response.data["livePreview"]["status"], "failed")

    def test_live_preview_retry_forwards_org_github_token(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "org-live-preview-token"
        config.save(update_fields=["github_token_encrypted", "updated_at"])
        preview_payload = {
            "available": False,
            "status": "starting",
            "previewUrl": "",
        }

        with patch("content_factory.vibe_marketing_views._call_content_factory_live_preview", return_value=preview_payload) as preview_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{self.run.run_id}/live-preview",
                {"force": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        preview_call.assert_called_once_with(
            run_id=self.run.run_id,
            method="POST",
            payload={"force": True, "local_repo_path": "", "github_token": "org-live-preview-token"},
        )

    @override_settings(
        CONTENT_FACTORY_URL="https://content-factory.test",
        CONTENT_FACTORY_API_KEY="secret-key",
        CONTENT_FACTORY_LIVE_PREVIEW_START_READ_TIMEOUT_SECONDS=12,
        IS_LOCAL_ENV=False,
    )
    def test_live_preview_post_timeout_stays_starting(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["timeout"] = timeout
            raise http_client.exceptions.ReadTimeout("Read timed out.")

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            payload = _call_content_factory_live_preview(
                run_id=self.run.run_id,
                method="POST",
                payload={"force": False},
            )

        self.assertEqual(payload["status"], "starting")
        self.assertEqual(payload["errorCode"], "preview_start_timeout")
        self.assertEqual(payload["error"], "")
        self.assertTrue(payload["retryable"])
        self.assertEqual(captured["timeout"], (3, 12))

    def test_comment_crud_serializes_anchor(self):
        response = self.client.post(
            f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments",
            {
                "componentId": "toc",
                "componentType": "toc",
                "componentLabel": "Table of contents",
                "selector": '[data-cf-component-id="toc"]',
                "anchor": {"x": 0.42, "y": 0.23, "createdFrom": "live_preview_click"},
                "body": "Make this easier to scan.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["anchor"]["x"], 0.42)
        self.assertEqual(response.data["anchor"]["y"], 0.23)

        comment_id = response.data["id"]
        patch_response = self.client.patch(
            f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/{comment_id}",
            {
                "componentId": "toc",
                "componentType": "toc",
                "componentLabel": "Table of contents",
                "selector": '[data-cf-component-id="toc"]',
                "body": "Make this easier to scan and shorter.",
            },
            format="json",
        )

        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["anchor"]["x"], 0.42)
        self.assertEqual(patch_response.data["anchor"]["y"], 0.23)

        list_response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["comments"][0]["anchor"]["createdFrom"], "live_preview_click")

    def test_revision_run_does_not_serialize_source_run_comments(self):
        VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Original title feedback.",
            status="submitted",
            batch_id="batch-original",
        )
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-original"},
        )

        response = self.client.get(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["comments"], [])
        self.assertEqual(response.data["latestBatch"]["id"], "batch-original")
        self.assertEqual(response.data["latestBatch"]["sourceRunId"], self.run.run_id)
        self.assertEqual(response.data["latestBatch"]["revisionRunId"], revision_run.run_id)
        self.assertEqual(response.data["latestBatch"]["status"], "completed")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_cancel_article_run_marks_tombstone_and_hides_from_bootstrap(self):
        self.run.status = ContentFactoryRunStatus.RUNNING
        self.run.current_step = "draft_article"
        self.run.run_request = {"target_keyword": "ai marketing"}
        self.run.result = {
            "target_keyword": "ai marketing",
            "delivery_package": {
                "title": "AI Marketing",
                "target_keyword": "ai marketing",
                "article_markdown": "steps/package/article.md",
            },
        }
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.save(update_fields=["status", "current_step", "run_request", "result", "acceptance_summary", "updated_at"])
        ContentFactoryRunStep.objects.create(
            run=self.run,
            step_key="draft_article",
            display_order=1,
            status="running",
            artifacts=["steps/draft_article/attempt-01/artifacts/article.md"],
        )
        VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Tighten this title.",
        )
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="ai marketing",
            keyword_normalized="ai marketing",
            status=KeywordStatus.IN_PROGRESS,
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertTrue(url.endswith(f"/api/runs/{self.run.run_id}/cancel"))
            return _Response(
                status_code=202,
                payload={"run_id": self.run.run_id, "status": "cancelled", "cleanup": {"artifacts": {"deleted_paths": ["article.md"]}}},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/cancel", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(response.data["contentPackage"])
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(self.run.current_step, "cancelled")
        self.assertTrue(self.run.result["cancelled"])
        self.assertEqual(self.run.steps.get(step_key="draft_article").status, "cancelled")
        keyword = ResearchedKeyword.objects.get(organization=self.organization, keyword_normalized="ai marketing")
        self.assertEqual(keyword.status, KeywordStatus.PENDING)

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertNotIn(self.run.run_id, [item["runId"] for item in bootstrap.data["latestRuns"]])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_from_completed_revision_uses_revision_draft_comments(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-original"},
        )
        source_comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Original title feedback.",
            status="submitted",
            batch_id="batch-original",
        )
        revision_comment = VibeMarketingComponentComment.objects.create(
            run=revision_run,
            actor=self.user,
            component_id="section:section-2",
            component_type="section",
            component_label="Section 2",
            selector='[data-cf-component-id="section:section-2"]',
            body="Tighten the revised section.",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision-2", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments-revision/component-revisions")
        self.assertEqual(captured["payload"]["source_run_id"], revision_run.run_id)
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(revision_comment.id))
        self.assertNotEqual(captured["payload"]["comments"][0]["comment_id"], str(source_comment.id))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_sends_anchor_to_content_factory_revision(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="section:section-1",
            component_type="section",
            component_label="Intro section",
            selector='[data-cf-component-id="section:section-1"]',
            anchor={"x": 0.25, "y": 0.75, "createdFrom": "live_preview_click"},
            body="Tighten this section.",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/component-revisions")
        self.assertEqual(captured["timeout"], (5, 90))
        self.assertTrue(captured["payload"]["requested_run_id"].startswith("component-revision-"))
        remote_comment = captured["payload"]["comments"][0]
        self.assertEqual(remote_comment["comment_id"], str(comment.id))
        self.assertEqual(remote_comment["anchor"], {"x": 0.25, "y": 0.75, "createdFrom": "live_preview_click"})

        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_retries_latest_submitted_batch(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
            status="submitted",
            batch_id="batch-retry-1",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-retry", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["payload"]["feedback_batch_id"], "batch-retry-1")
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(comment.id))

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_from_failed_revision_retries_source_batch(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.FAILED,
            run_request={"source_run_id": self.run.run_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-retry-2"},
        )
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
            status="submitted",
            batch_id="batch-retry-2",
        )

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-run-comments-revision-retry-2", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/article-run-comments/component-revisions")
        self.assertEqual(captured["payload"]["source_run_id"], self.run.run_id)
        self.assertEqual(captured["payload"]["feedback_batch_id"], "batch-retry-2")
        self.assertEqual(captured["payload"]["comments"][0]["comment_id"], str(comment.id))
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["component_feedback_revision_run_id"], "article-run-comments-revision-retry-2")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_submit_keeps_retryable_timeout_batch_submitted(self):
        comment = VibeMarketingComponentComment.objects.create(
            run=self.run,
            actor=self.user,
            component_id="title",
            component_type="title",
            component_label="Title",
            selector='[data-cf-component-id="title"]',
            body="Make the title sharper.",
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            raise http_client.RequestException("Read timed out.")

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/comments/submit", {}, format="json")

        self.assertEqual(response.status_code, 202)
        comment.refresh_from_db()
        self.assertEqual(comment.status, "submitted")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["component_feedback_latest_batch"]["status"], "submitted")
        self.assertTrue(self.run.result["component_feedback_latest_batch"]["retryable"])

    def test_workflow_progress_marks_package_complete_before_publish(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(steps["package"]["status"], "complete")
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")
        self.assertNotEqual(steps["publish"]["status"], "complete")

    def test_workflow_progress_keeps_completed_article_on_review_step(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {"delivery_mode": "content_only"}
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "promote_bundle_url": f"/api/runs/{self.run.run_id}/promote-bundle",
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "livePreview": {
                "available": True,
                "status": "ready",
                "previewUrl": "http://127.0.0.1:4321/articles/featured/generated?cfInspector=1",
                "exactRender": True,
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "target_keyword": "australian founders",
                "article_markdown": "steps/package_content_delivery/attempt-01/artifacts/article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        with patch("content_factory.vibe_marketing_views._call_content_factory_run_status", return_value={}):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{self.run.run_id}")

        self.assertEqual(response.status_code, 200)
        progress = response.data["workflowProgress"]
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertEqual(progress["currentStepId"], "review")
        self.assertEqual(steps["generate"]["status"], "complete")
        self.assertEqual(steps["review"]["status"], "ready")
        self.assertEqual(steps["publish"]["status"], "ready")
        self.assertEqual(steps["publish"]["primaryAction"]["intent"], "promote-bundle")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_creates_local_publish_child_run(self):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.github_token_encrypted = "encrypted-token"
        config.github_repo = "MLAI-AUS-Inc/mlai-au"
        config.company_context = "MLAI helps Australian founders adopt AI."
        config.article_delivery_mode = "content_only"
        config.baseline_skipped_at = config.updated_at
        config.article_system = {"state": "existing", "confidence": "high", "directory_name": "articles"}
        config.publish_targets = [{"id": "articles", "label": "Articles"}]
        config.save(
            update_fields=[
                "github_token_encrypted",
                "github_repo",
                "company_context",
                "article_delivery_mode",
                "baseline_skipped_at",
                "article_system",
                "publish_targets",
                "updated_at",
            ]
        )
        self.organization.seed_keywords = ["australian founders"]
        self.organization.save(update_fields=["seed_keywords"])
        ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="australian founders",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        self.run.run_request = {
            "domain": "mlai.au",
            "topic": "Australian founders",
            "target_keyword": "australian founders",
            "delivery_mode": "content_only",
        }
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result = {
            "delivery_mode": "content_only",
            "componentManifest": {
                "components": [
                    {
                        "id": "title",
                        "type": "title",
                        "label": "Title",
                    }
                ]
            },
            "delivery_package": {
                "title": "Australian Founders and What the Term Means Today",
                "article_markdown": "article.md",
            },
        }
        self.run.save(update_fields=["run_request", "acceptance_summary", "result", "updated_at"])

        def fake_post(url, json=None, headers=None, timeout=None):
            return _Response(status_code=202, payload={"run_id": "article-publish-child-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-publish-child-1")
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-1")
        self.assertEqual(publish_run.workflow, "article_generation")
        self.assertEqual(publish_run.run_request["source_run_id"], self.run.run_id)
        self.assertEqual(publish_run.run_request["delivery_mode"], "publish_code")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-1")
        steps = {step["id"]: step for step in response.data["workflowProgress"]["steps"]}
        self.assertEqual(response.data["workflowProgress"]["currentStepId"], "publish")
        self.assertEqual(steps["publish"]["status"], "running")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_promote_bundle_targets_accepted_component_revision(self):
        revision_run = ContentFactoryRun.objects.create(
            run_id="article-run-comments-revision-accepted",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-accepted"},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": "batch-accepted"},
        )
        self.run.run_request = {"delivery_mode": "review_draft"}
        self.run.result = {
            "delivery_mode": "review_draft",
            "component_feedback_latest_batch": {
                "id": "batch-accepted",
                "sourceRunId": self.run.run_id,
                "revisionRunId": revision_run.run_id,
                "status": "accepted",
            },
        }
        self.run.save(update_fields=["run_request", "result", "updated_at"])
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Response(status_code=202, payload={"run_id": "article-publish-child-from-revision", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{self.run.run_id}/promote-bundle", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            captured["url"],
            "https://content-factory.test/api/runs/article-run-comments-revision-accepted/promote-bundle",
        )
        publish_run = ContentFactoryRun.objects.get(run_id="article-publish-child-from-revision")
        self.assertEqual(publish_run.run_request["source_run_id"], revision_run.run_id)
        self.assertEqual(publish_run.run_request["review_source_run_id"], self.run.run_id)
        self.run.refresh_from_db()
        revision_run.refresh_from_db()
        self.assertEqual(self.run.result["publish_child_run_id"], "article-publish-child-from-revision")
        self.assertEqual(revision_run.result["publish_child_run_id"], "article-publish-child-from-revision")
