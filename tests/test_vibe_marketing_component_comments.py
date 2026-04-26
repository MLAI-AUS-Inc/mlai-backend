from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from integrations import http_client
from content_factory.models import OrganizationContentConfig, VibeMarketingComponentComment
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


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
