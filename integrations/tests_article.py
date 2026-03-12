from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from unittest.mock import MagicMock, patch
import requests

from core.article_system import resolve_article_system_with_source
from core.models import ContentFactoryRun, Organization, OrganizationContentConfig
from integrations.models import UserIntegration
from integrations.services.article_generation import (
    ArticleSystemActionRequiredError,
    check_generation_status,
    confirm_topic,
    publish_article,
    trigger_article_generation,
)


class ArticleGenerationServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_123"
        self.repo_name = "test/repo"

        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo=self.repo_name,
            github_access_token="gh_token_123",
            github_token_expires_at=timezone.now() + timezone.timedelta(days=1),
            project_scanned=True,
        )

        self.org = Organization.objects.create(domain="mlai.au", name="Test Org")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo=self.repo_name,
            article_template="## Template Content",
            github_token_encrypted="org-token",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
            scan_summary="scan complete",
            article_system={
                "state": "existing",
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "confidence": "high",
                "reason": "Detected existing article system",
                "source": "scan",
                "verified_at": "2026-03-08T00:00:00+00:00",
            },
        )

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info",
        }

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_123")

        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/article", args[0])
        payload = kwargs["json"]

        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["topic"], "AI Agents")
        self.assertEqual(payload["target_keyword"], "agentic")
        self.assertEqual(payload["context"], "Context info")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertNotIn("github_token", payload)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_payload_includes_delivery_mode(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_confirm_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            result = confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                custom_title="Agentic AI",
            )

        self.assertEqual(result["job_id"], "job_confirm_123")

        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/article", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "publish_code")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_publish_article(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "queued",
            "approval_state": "approved",
            "job_id": "job_123",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = publish_article("job_123", self.slack_user_id, domain="mlai.au")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["approval_state"], "approved")

        args, _ = mock_post.call_args
        self.assertIn("/api/runs/job_123/approve", args[0])

    def test_trigger_generation_requires_article_system_when_missing(self):
        self.config.article_system = {}
        self.config.save(update_fields=["article_system"])

        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info",
        }

        with self.assertRaises(ArticleSystemActionRequiredError) as exc:
            trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(exc.exception.recommended_action, "scaffold")
        self.assertEqual(exc.exception.resolution_source, "default_missing")

    @patch("integrations.services.article_generation.get_github_credentials_for_domain")
    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_uses_scan_summary_article_system_fallback(self, mock_post, mock_get_credentials):
        self.config.article_system = {}
        self.config.scan_summary = {
            "articles_status": {
                "has_articles_system": True,
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "detected_type": "tsx",
                "existing_files": ["app/articles/content/index.tsx"],
            }
        }
        self.config.save(update_fields=["article_system", "scan_summary"])

        resolved, source = resolve_article_system_with_source(self.config)
        self.assertEqual(resolved["state"], "existing")
        self.assertEqual(source, "scan_summary_fallback")

        mock_get_credentials.return_value = {
            "token": "gh_token_123",
            "repo": self.repo_name,
            "source": "user",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "job_id": "job_scan_fallback",
            "status": "queued",
        }
        mock_post.return_value = mock_response

        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info",
        }

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_scan_fallback")

    @patch("integrations.services.article_generation.set_article_delivery_mode")
    @patch("integrations.services.article_generation.http_requests.get")
    def test_check_generation_status_auto_selects_delivery_mode(self, mock_get, mock_set_delivery_mode):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "job_id": "job_waiting_mode",
            "status": "awaiting_delivery_mode",
        }
        mock_get.return_value = mock_response
        mock_set_delivery_mode.return_value = {
            "job_id": "job_waiting_mode",
            "status": "queued",
            "delivery_mode": "publish_code",
        }

        result = check_generation_status("job_waiting_mode")

        self.assertEqual(result["status"], "queued")
        mock_set_delivery_mode.assert_called_once_with("job_waiting_mode")

    @patch("integrations.services.article_generation.http_requests.get")
    def test_check_generation_status_falls_back_to_local_run_when_cf_unavailable(self, mock_get):
        ContentFactoryRun.objects.create(
            run_id="run-local-1",
            workflow="direct_generate",
            domain="mlai.au",
            status="awaiting_delivery_mode",
            current_step="awaiting_delivery_mode",
            run_request={"domain": "mlai.au"},
        )
        mock_get.side_effect = requests.exceptions.RequestException("boom")

        result = check_generation_status("run-local-1")

        self.assertEqual(result["job_id"], "run-local-1")
        self.assertEqual(result["status"], "awaiting_delivery_mode")
