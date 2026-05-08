from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from unittest.mock import MagicMock, patch
import requests

from content_factory.article_system import resolve_article_system_with_source
from content_factory.models import ContentFactoryJob, OrganizationContentConfig
from core.models import User
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from integrations.models import UserIntegration
from integrations.services.article_generation import (
    ArticleGenerationError,
    ContentFactoryBackendUnavailableError,
    GitHubReconnectRequiredError,
    _append_refund_instruction,
    check_generation_status,
    confirm_topic,
    get_content_factory_article_cost_points,
    publish_article,
    publish_article_as_pr,
    promote_article_bundle,
    trigger_article_generation,
)
from roo.models import PointsAccount


class ArticleGenerationServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_123"
        self.repo_name = "test/repo"
        self.user_email = "writer@example.com"
        self.user = User.objects.create_user(
            email=self.user_email,
            password="password",
            slack_id=self.slack_user_id,
            first_name="Test",
            last_name="Writer",
        )
        PointsAccount.objects.create(user=self.user, balance=20)

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

    def _article_request(self, **overrides):
        request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info",
            "request_source": "roo_slackbot",
            "client_request_id": "content-factory-service-request",
            "user_email": self.user_email,
            "user_first_name": "Test",
            "user_last_name": "Writer",
            "user_avatar_url": "https://avatar.test/writer.png",
        }
        request.update(overrides)
        return request

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request()

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
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
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertEqual(payload["request_source"], "roo_slackbot")
        self.assertNotIn("github_token", payload)

        job = ContentFactoryJob.objects.get(job_id="job_123")
        self.assertEqual(job.client_request_id, "content-factory-service-request")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_amount, 0)
        self.assertEqual(job.billing_source_job_id, "job_123")
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], (3, 8))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_payload_includes_requested_by_slack_user_id(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_delegated_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(requested_by_slack_user_id="U_REQUESTER")

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_delegated_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["requested_by_slack_user_id"], "U_REQUESTER")

        job = ContentFactoryJob.objects.get(job_id="job_delegated_123")
        self.assertEqual(job.request_meta["requested_by_slack_user_id"], "U_REQUESTER")

    def test_paid_article_cost_is_four_points(self):
        self.assertEqual(get_content_factory_article_cost_points("example.com"), 4)
        self.assertEqual(get_content_factory_article_cost_points("mlai.au"), 0)

    def test_paid_article_refund_instruction_uses_four_points(self):
        message = _append_refund_instruction("The article run failed.", "example.com")

        self.assertIn("4 Roo points", message)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_paid_domain_charges_four_points(self, mock_post):
        paid_org = Organization.objects.create(domain="example.com", name="Paid Org")
        OrganizationContentConfig.objects.create(
            organization=paid_org,
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
                "source": "test",
            },
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_paid_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            domain="example.com",
            client_request_id="content-factory-paid-request",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_paid_123")
        job = ContentFactoryJob.objects.get(job_id="job_paid_123")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_amount, 4)
        self.assertEqual(job.billing_ledger.delta, -4)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 16)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_stores_thread_context_without_forwarding_it(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_thread_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_thread_123")

        payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("slack_channel_id", payload)
        self.assertNotIn("slack_thread_ts", payload)
        self.assertNotIn("slack_root_message_ts", payload)

        job = ContentFactoryJob.objects.get(job_id="job_thread_123")
        self.assertEqual(job.slack_channel_id, "C123")
        self.assertEqual(job.slack_thread_ts, "123.456")
        self.assertEqual(job.slack_root_message_ts, "123.456")
        self.assertEqual(job.request_meta["slack_thread_ts"], "123.456")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_payload_includes_delivery_mode(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_confirm_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                custom_title="Agentic AI",
                request_source="roo_slackbot",
            )

        self.assertEqual(result["job_id"], "job_confirm_123")

        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/article", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertEqual(payload["request_source"], "roo_slackbot")
        self.assertEqual(kwargs["timeout"], (3, 8))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_payload_includes_requested_by_slack_user_id(self, mock_post):
        self.config.article_delivery_mode = "publish_code"
        self.config.save(update_fields=["article_delivery_mode"])
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_confirm_delegated_123", "status": "queued"}
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                requested_by_slack_user_id="U_REQUESTER",
                custom_title="Agentic AI",
                request_source="roo_slackbot",
            )

        self.assertEqual(result["job_id"], "job_confirm_delegated_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)
        self.assertEqual(payload["requested_by_slack_user_id"], "U_REQUESTER")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_reuses_existing_non_terminal_child_without_post(self, mock_post):
        source_job = ContentFactoryJob.objects.create(
            job_id="job-source-active-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="awaiting_confirmation",
        )
        child_job = ContentFactoryJob.objects.create(
            job_id="job-child-active-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="queued",
            request_meta={
                "source_run_id": source_job.job_id,
                "topic": "Agentic AI",
                "target_keyword": "agentic ai",
            },
        )

        result = confirm_topic(
            domain="mlai.au",
            confirmed_keyword="agentic ai",
            slack_user_id=self.slack_user_id,
            custom_title="Agentic AI",
            source_run_id=source_job.job_id,
            request_source="roo_slackbot",
        )

        self.assertEqual(result["job_id"], child_job.job_id)
        self.assertEqual(result["status"], "queued")
        mock_post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_raises_structured_backend_unavailable_on_queue_timeout(self, mock_post):
        mock_post.side_effect = requests.exceptions.ReadTimeout("timed out")

        with self.assertRaises(ContentFactoryBackendUnavailableError) as exc:
            confirm_topic(
                domain="mlai.au",
                confirmed_keyword="agentic ai",
                slack_user_id=self.slack_user_id,
                request_source="roo_slackbot",
            )

        self.assertEqual(exc.exception.payload["error_code"], "CONTENT_FACTORY_UNAVAILABLE")
        self.assertEqual(exc.exception.payload["operation"], "confirm_topic")
        self.assertEqual(mock_post.call_count, 2)

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

    @patch("integrations.services.article_generation.http_requests.post")
    def test_promote_article_bundle_copies_thread_context_to_child_job(self, mock_post):
        ContentFactoryJob.objects.create(
            job_id="job_content_ready",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="completed",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
            request_meta={"requested_by_slack_user_id": "U_REQUESTER"},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "queued",
            "job_id": "job_publish_child",
            "source_run_id": "job_content_ready",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = promote_article_bundle("job_content_ready", slack_user_id=self.slack_user_id, domain="mlai.au")

        self.assertEqual(result["job_id"], "job_publish_child")
        args, _ = mock_post.call_args
        self.assertIn("/api/runs/job_content_ready/promote-bundle", args[0])

        child_job = ContentFactoryJob.objects.get(job_id="job_publish_child")
        self.assertEqual(child_job.slack_channel_id, "C123")
        self.assertEqual(child_job.slack_thread_ts, "123.456")
        self.assertEqual(child_job.slack_root_message_ts, "123.456")
        self.assertEqual(child_job.request_meta["source_run_id"], "job_content_ready")
        self.assertEqual(child_job.request_meta["promotion_source"], "promote_bundle")
        self.assertEqual(child_job.request_meta["requested_by_slack_user_id"], "U_REQUESTER")

    @patch("integrations.services.article_generation.promote_article_bundle")
    def test_publish_article_as_pr_delegates_to_promote_bundle(self, mock_promote_article_bundle):
        mock_promote_article_bundle.return_value = {
            "status": "queued",
            "job_id": "job_publish_child",
        }

        result = publish_article_as_pr(
            "job_content_ready",
            slack_user_id=self.slack_user_id,
            requested_by_slack_user_id="U_REQUESTER",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )
        self.assertEqual(result["job_id"], "job_publish_child")
        mock_promote_article_bundle.assert_called_once_with(
            "job_content_ready",
            slack_user_id=self.slack_user_id,
            requested_by_slack_user_id="U_REQUESTER",
            domain="mlai.au",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            slack_root_message_ts="123.456",
        )

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_raises_structured_auth_required_on_content_factory_412(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 412
        mock_response.json.return_value = {
            "status": "precondition_failed",
            "error_code": "AUTH_REQUIRED",
            "missing_step": "github_auth",
            "next_action": "reconnect_github",
            "requires_user_action": True,
            "resume_hint": "reconnect_github_then_retry",
            "domain": "mlai.au",
            "github_repo": self.repo_name,
            "reason_code": "missing_or_expired_credentials",
            "message": "Reconnect GitHub before publishing.",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            with self.assertRaises(GitHubReconnectRequiredError) as exc_info:
                trigger_article_generation(self.slack_user_id, self._article_request())

        payload = exc_info.exception.payload
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["missing_step"], "github_auth")
        self.assertEqual(payload["next_action"], "reconnect_github")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertTrue(payload["auth_url"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_promote_article_bundle_raises_structured_auth_required_on_content_factory_412(self, mock_post):
        ContentFactoryJob.objects.create(
            job_id="job_content_ready_auth",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="completed",
        )

        mock_response = MagicMock()
        mock_response.status_code = 412
        mock_response.json.return_value = {
            "status": "precondition_failed",
            "error_code": "AUTH_REQUIRED",
            "missing_step": "github_auth",
            "next_action": "reconnect_github",
            "requires_user_action": True,
            "resume_hint": "reconnect_github_then_retry",
            "domain": "mlai.au",
            "github_repo": self.repo_name,
            "reason_code": "missing_or_expired_credentials",
            "message": "Reconnect GitHub before promoting this bundle.",
        }
        mock_post.return_value = mock_response

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            with self.assertRaises(GitHubReconnectRequiredError) as exc_info:
                promote_article_bundle("job_content_ready_auth", slack_user_id=self.slack_user_id, domain="mlai.au")

        payload = exc_info.exception.payload
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertTrue(payload["auth_url"])

    @patch("content_factory.content_views.trigger_article_generation")
    def test_content_generate_view_returns_structured_auth_required(self, mock_trigger):
        mock_trigger.side_effect = GitHubReconnectRequiredError(
            {
                "status": "precondition_failed",
                "error_code": "AUTH_REQUIRED",
                "missing_step": "github_auth",
                "next_action": "reconnect_github",
                "requires_user_action": True,
                "resume_hint": "reconnect_github_then_retry",
                "domain": "mlai.au",
                "github_repo": self.repo_name,
                "reason_code": "missing_or_expired_credentials",
                "message": "Reconnect GitHub before continuing.",
                "auth_url": "https://github.example/reconnect",
            }
        )

        with self.settings(ROO_API_KEY="roo-test-key"):
            response = self.client.post(
                "/api/v1/content/generate",
                data={
                    "slack_user_id": self.slack_user_id,
                    "domain": "mlai.au",
                    "topic": "AI Agents",
                    "target_keyword": "agentic ai",
                    "request_source": "roo_slackbot",
                    "client_request_id": "content-generate-auth-required",
                },
                HTTP_X_API_KEY="roo-test-key",
            )

        self.assertEqual(response.status_code, 412)
        payload = response.json()
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["github_repo"], self.repo_name)
        self.assertEqual(payload["auth_url"], "https://github.example/reconnect")
        self.assertTrue(payload["pending_intent_stored"])

    @patch("content_factory.content_views.trigger_article_generation")
    def test_content_generate_view_delegated_auth_required_does_not_store_pending_intent(self, mock_trigger):
        mock_trigger.side_effect = GitHubReconnectRequiredError(
            {
                "status": "precondition_failed",
                "error_code": "AUTH_REQUIRED",
                "missing_step": "github_auth",
                "next_action": "reconnect_github",
                "requires_user_action": True,
                "resume_hint": "reconnect_github_then_retry",
                "domain": "mlai.au",
                "github_repo": self.repo_name,
                "reason_code": "missing_or_expired_credentials",
                "message": "Reconnect GitHub before continuing.",
                "auth_url": "https://github.example/reconnect",
            }
        )

        with self.settings(ROO_API_KEY="roo-test-key"):
            response = self.client.post(
                "/api/v1/content/generate",
                data={
                    "slack_user_id": self.slack_user_id,
                    "requested_by_slack_user_id": "U_REQUESTER",
                    "domain": "mlai.au",
                    "topic": "AI Agents",
                    "target_keyword": "agentic ai",
                    "request_source": "roo_slackbot",
                    "client_request_id": "content-generate-auth-required-delegated",
                },
                HTTP_X_API_KEY="roo-test-key",
            )

        self.assertEqual(response.status_code, 412)
        payload = response.json()
        self.assertEqual(payload["requested_by_slack_user_id"], "U_REQUESTER")
        self.assertFalse(payload["pending_intent_stored"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_omits_delivery_mode_when_no_preference_exists(self, mock_post):
        self.config.article_system = {}
        self.config.article_delivery_mode = None
        self.config.save(update_fields=["article_system", "article_delivery_mode"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_content_only_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request()
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_content_only_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertIsNone(payload.get("delivery_mode"))
        self.assertIsNone(payload.get("delivery_mode_confirmed"))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_uses_saved_article_delivery_mode_without_repo(self, mock_post):
        self.config.github_repo = ""
        self.config.article_delivery_mode = "content_only"
        self.config.save(update_fields=["github_repo", "article_delivery_mode"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_saved_content_only_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request()
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_saved_content_only_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertIsNone(payload.get("github_repo"))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_allows_repo_less_domain_without_scan_summary(self, mock_post):
        self.config.github_repo = ""
        self.config.scan_summary = None
        self.config.article_system = {}
        self.config.article_delivery_mode = "content_only"
        self.config.save(
            update_fields=[
                "github_repo",
                "scan_summary",
                "article_system",
                "article_delivery_mode",
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_repo_less_direct_123", "status": "queued"}
        mock_post.return_value = mock_response

        result = trigger_article_generation(self.slack_user_id, self._article_request())

        self.assertEqual(result["job_id"], "job_repo_less_direct_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertFalse(payload["delivery_mode_confirmed"])
        self.assertIsNone(payload.get("github_repo"))

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_discovery_allows_repo_less_domain_without_scan_summary(self, mock_post):
        self.config.github_repo = ""
        self.config.scan_summary = None
        self.config.article_system = {}
        self.config.article_delivery_mode = "content_only"
        self.config.save(
            update_fields=[
                "github_repo",
                "scan_summary",
                "article_system",
                "article_delivery_mode",
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job_repo_less_discovery_123", "status": "queued"}
        mock_post.return_value = mock_response

        result = trigger_article_generation(self.slack_user_id, self._article_request(topic=None))

        self.assertEqual(result["job_id"], "job_repo_less_discovery_123")
        args, kwargs = mock_post.call_args
        self.assertIn("/api/runs/discovery", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["domain"], "mlai.au")
        self.assertEqual(payload["slack_user_id"], self.slack_user_id)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_preserves_explicit_delivery_mode_confirmation(self, mock_post):
        self.config.article_system = {}
        self.config.save(update_fields=["article_system"])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_explicit_publish_123", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = self._article_request(
            delivery_mode="publish_code",
            delivery_mode_confirmed=True,
        )
        result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_explicit_publish_123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "publish_code")
        self.assertTrue(payload["delivery_mode_confirmed"])

    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_reuses_saved_delivery_mode_from_source_job(self, mock_post):
        source_job = ContentFactoryJob.objects.create(
            job_id="job-source-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="awaiting_confirmation",
            request_meta={
                "domain": "mlai.au",
                "delivery_mode": "content_only",
                "delivery_mode_confirmed": True,
            },
        )

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"job_id": "job-confirm-source-123", "status": "queued"}
        mock_post.return_value = mock_response

        result = confirm_topic(
            domain="mlai.au",
            confirmed_keyword="agentic ai",
            slack_user_id=self.slack_user_id,
            source_run_id=source_job.job_id,
            request_source="roo_slackbot",
        )

        self.assertEqual(result["job_id"], "job-confirm-source-123")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["delivery_mode"], "content_only")
        self.assertTrue(payload["delivery_mode_confirmed"])

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

        article_request = self._article_request()

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_scan_fallback")

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_sync_failure_auto_refunds(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"
        mock_post.return_value = mock_response

        with self.settings(
            CONTENT_FACTORY_API_KEY="test-key",
            CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="publish_code",
        ):
            with self.assertRaises(ArticleGenerationError):
                trigger_article_generation(self.slack_user_id, self._article_request())

        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    @patch("integrations.services.article_generation.http_requests.post")
    def test_trigger_generation_reuses_existing_billed_job_for_same_client_request_id(self, mock_post):
        ContentFactoryJob.objects.create(
            job_id="existing-job-123",
            domain="mlai.au",
            slack_user_id=self.slack_user_id,
            status="awaiting_confirmation",
            client_request_id="content-factory-service-request",
            billing_source_job_id="existing-job-123",
            billing_amount=0,
            billing_status="charged",
            request_meta={"domain": "mlai.au"},
        )

        result = trigger_article_generation(self.slack_user_id, self._article_request(topic=None))

        self.assertEqual(result["job_id"], "existing-job-123")
        mock_post.assert_not_called()

    @patch("integrations.services.article_generation.http_requests.get")
    def test_check_generation_status_preserves_awaiting_delivery_mode(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "job_id": "job_waiting_mode",
            "status": "awaiting_delivery_mode",
        }
        mock_get.return_value = mock_response

        result = check_generation_status("job_waiting_mode")

        self.assertEqual(result["status"], "awaiting_delivery_mode")

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
