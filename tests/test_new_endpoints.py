import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from django.urls import reverse
from requests import Response

from core.models import Organization, OrganizationContentConfig, ResearchedKeyword
from integrations.models import UserIntegration
from roo.models import ChannelFirstPost, PointsAccount
from core.models import ContentFactoryJob

User = get_user_model()

class EndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_api_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key
        from django.conf import settings
        settings.INTERNAL_API_KEY = self.api_key
        settings.ROO_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        
        # Create a test user
        self.user = User.objects.create_user(email="test@example.com", password="password")

    def test_integrations_endpoints(self):
        # 1. Create Token
        url = reverse('github_integration_list')
        data = {
            "slack_user_id": "U123",
            "token": "gh_token",
            "user_name": "gh_user",
            "scopes": ["repo"]
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(UserIntegration.objects.filter(slack_user_id="U123").exists())

        # 2. Get Integration
        url_detail = reverse('github_integration_detail', args=["U123"])
        response = self.client.get(url_detail)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['token'], "gh_token")

        # 3. Patch Integration
        response = self.client.patch(url_detail, {"project_scanned": True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(UserIntegration.objects.get(slack_user_id="U123").project_scanned)

    def test_pending_intent_endpoints(self):
        # 1. Save Intent
        url = reverse('pending_intent_list')
        data = {"slack_user_id": "U456", "intent": "some intent"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(UserIntegration.objects.get(slack_user_id="U456").pending_intent, "some intent")

        # 2. Clear Intent
        url_clear = reverse('pending_intent_detail', args=["U456"])
        response = self.client.delete(url_clear)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(UserIntegration.objects.get(slack_user_id="U456").pending_intent)

    def test_channel_activity_endpoints(self):
        # Setup: Link user to Slack ID
        self.user.slack_id = "U789"
        self.user.save()

        # 1. Record First Post
        url = reverse('first_post_record')
        data = {"slack_user_id": "U789", "channel_id": "C123"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ChannelFirstPost.objects.filter(slack_user_id="U789", channel_id="C123").exists())
        self.assertTrue(response.data['points_awarded'])

        # 2. Duplicate Check
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        # 3. Check Status
        url_check = reverse('first_post_check', args=["U789", "C123"])
        response = self.client.get(url_check)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['has_posted'])

    def test_channel_activity_allows_roo_api_key_when_internal_differs(self):
        """
        Roo should be able to call channel activity endpoints even if
        INTERNAL_API_KEY is different from ROO_API_KEY.
        """
        from django.conf import settings

        os.environ['ROO_API_KEY'] = 'roo_only_key'
        os.environ['INTERNAL_API_KEY'] = 'internal_only_key'
        settings.ROO_API_KEY = 'roo_only_key'
        settings.INTERNAL_API_KEY = 'internal_only_key'

        client = APIClient()
        client.credentials(HTTP_X_API_KEY='roo_only_key')

        url_check = reverse('first_post_check', args=["U000", "C000"])
        response = client.get(url_check)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['has_posted'])

    def test_user_linking_endpoint(self):
        # 1. Link Valid User
        url = reverse('link_slack')
        data = {"slack_id": "U999", "email": "test@example.com"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.slack_id, "U999")

        # 2. Invalid User
        data = {"slack_id": "U888", "email": "notfound@example.com"}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ContentGenerateAutoWriteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = "test_internal_key"
        os.environ['MLAI_API_KEY'] = "test_mlai_key"

        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = "test_internal_key"
        settings.MLAI_API_KEY = "test_mlai_key"
        settings.CONTENT_FACTORY_URL = "http://content-factory.test"

        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.user_email = "auto@example.com"
        self.user = User.objects.create_user(
            email=self.user_email,
            password="password",
            slack_id="U-AUTO",
        )
        PointsAccount.objects.create(user=self.user, balance=20)

        self.integration = UserIntegration.objects.create(
            slack_user_id="U-AUTO",
            github_access_token="gh_token",
            github_repo="owner/repo",
        )
        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="owner/repo",
            github_token_encrypted="org-token",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
            scan_summary="scan complete",
        )

    def _generate_request_data(self, **overrides):
        data = {
            "slack_user_id": self.integration.slack_user_id,
            "domain": self.organization.domain,
            "request_source": "roo_slackbot",
            "client_request_id": "content-factory-test-request",
            "user_email": self.user_email,
            "user_first_name": "Auto",
            "user_last_name": "Writer",
            "user_avatar_url": "https://avatar.test/auto.png",
        }
        data.update(overrides)
        return data

    def _mock_response(self, status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    def test_generate_auto_write_uses_discovery_endpoint_after_scan(self):
        url = reverse('content_generate')
        data = self._generate_request_data(topic=None)

        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.side_effect = [
                self._mock_response(202, {"job_id": "job-123"}),  # generate
            ]

            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-123")
        self.assertEqual(response.data['workflow'], "auto_discovery")
        
        # Verify call count (should be 1 call to discovery)
        self.assertEqual(mock_post.call_count, 1)

        generate_call = mock_post.call_args_list[0]
        self.assertIn("/api/runs/discovery", generate_call.args[0])

        generate_payload = generate_call.kwargs.get('json') or {}
        self.assertEqual(generate_payload.get('domain'), self.organization.domain)
        self.assertEqual(generate_payload.get('slack_user_id'), self.integration.slack_user_id)
        self.assertEqual(generate_payload.get('request_source'), 'roo_slackbot')

    def test_generate_with_topic_returns_article_system_action_required_when_missing(self):
        url = reverse('content_generate')
        data = self._generate_request_data(topic="best ai coding agents")

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_412_PRECONDITION_FAILED)
        self.assertEqual(response.data['error_code'], 'ARTICLE_SYSTEM_ACTION_REQUIRED')
        self.assertEqual(response.data['recommended_action'], 'scaffold')
        self.assertEqual(response.data['article_system_resolution_source'], 'default_missing')
        self.assertTrue(response.data['pending_intent_stored'])

    @patch('integrations.api_views_content.trigger_article_generation')
    def test_generate_passes_slack_thread_context_to_service(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-thread", "status": "queued"}

        url = reverse('content_generate')
        data = self._generate_request_data(
            topic="best ai coding agents",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
        )

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_trigger.assert_called_once_with(
            self.integration.slack_user_id,
            {
                "domain": self.organization.domain,
                "topic": "best ai coding agents",
                "target_keyword": None,
                "context": None,
                "slack_channel_id": "C123",
                "slack_thread_ts": "123.456",
                "slack_root_message_ts": "123.456",
                "progress_message_ts": None,
                "request_source": "roo_slackbot",
                "client_request_id": "content-factory-test-request",
                "user_email": self.user_email,
                "user_first_name": "Auto",
                "user_last_name": "Writer",
                "user_avatar_url": "https://avatar.test/auto.png",
            },
        )

    @patch('integrations.services.article_generation.get_github_credentials_for_domain')
    @patch('integrations.services.article_generation.http_requests.post')
    def test_generate_with_topic_uses_scan_summary_article_system_fallback(self, mock_post, mock_get_credentials):
        config = OrganizationContentConfig.objects.get(organization=self.organization)
        config.scan_summary = {
            "articles_status": {
                "has_articles_system": True,
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "detected_type": "tsx",
            }
        }
        config.save(update_fields=['scan_summary'])

        mock_get_credentials.return_value = {
            "token": "gh_token",
            "repo": "owner/repo",
            "source": "org",
        }
        mock_response = self._mock_response(202, {"job_id": "job-article-123"})
        mock_post.return_value = mock_response

        url = reverse('content_generate')
        data = self._generate_request_data(topic="best ai coding agents")

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], 'job-article-123')

    def test_generate_rejects_missing_request_source(self):
        response = self.client.post(
            reverse('content_generate'),
            {
                "slack_user_id": self.integration.slack_user_id,
                "domain": self.organization.domain,
                "client_request_id": "content-factory-test-request",
                "user_email": self.user_email,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_generate_rejects_non_roo_key(self):
        other_client = APIClient()
        other_client.credentials(HTTP_X_API_KEY="test_internal_key")

        response = other_client.post(
            reverse('content_generate'),
            self._generate_request_data(),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ArticleSystemDecisionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key

        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key

        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.integration = UserIntegration.objects.create(
            slack_user_id="U-DECIDE",
            pending_intent={
                "type": "write_article",
                "article_request": {
                    "domain": "mlai.au",
                    "topic": "best ai coding agents",
                },
            },
        )
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            scan_summary="scan complete",
            article_system={
                "state": "ambiguous",
                "directory_name": "articles",
                "directory_path": "app/articles/content",
                "confidence": "low",
                "reason": "Possible article directory detected",
                "source": "scan",
                "verified_at": "2026-03-08T00:00:00+00:00",
            },
        )

    @patch('integrations.api_views_content.trigger_article_generation')
    def test_article_system_decision_use_detected_resumes_pending_intent(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-123", "status": "queued"}
        url = reverse('content_article_system_decision')

        response = self.client.post(
            url,
            {
                "domain": "mlai.au",
                "slack_user_id": "U-DECIDE",
                "decision": "use_detected",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.config.refresh_from_db()
        self.integration.refresh_from_db()
        self.assertEqual(self.config.article_system['state'], 'existing')
        self.assertEqual(self.config.article_system['source'], 'manual_confirmed')
        self.assertIsNone(self.integration.pending_intent)
        self.assertTrue(response.data['resume_triggered'])
        mock_trigger.assert_called_once()


class ContentFactoryCallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_callback(self, mock_send_dm):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "topic_selection",
            "job_id": "job-123",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "selection": {
                "selected_keyword": "ai agents",
                "selection_reason": "High volume",
                "total_opportunities": 5,
                "volume": 2400,
                "difficulty": 35,
                "tier": "tier_1",
                "opportunity_index": 85.2
            }
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="job-123").exists())
        
        job = ContentFactoryJob.objects.get(job_id="job-123")
        self.assertEqual(job.status, 'awaiting_confirmation')
        self.assertEqual(job.selected_keyword, "ai agents")
        self.assertEqual(job.slack_user_id, "U123")
        
        mock_send_dm.assert_called_once()
        call_args = mock_send_dm.call_args
        self.assertEqual(call_args[0][0], "U123")
        self.assertIn("Topic selection ready", call_args[0][1])

    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_callback_posts_to_thread_when_context_exists(self, mock_send_dm, mock_send_message):
        mock_send_message.return_value = (True, "live-topic-card")
        ContentFactoryJob.objects.create(
            job_id="job-topic-thread",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "topic_selection",
                "job_id": "job-topic-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "selection": {
                    "selected_keyword": "ai agents",
                    "options": [
                        {"keyword": "option 1", "volume": 1000},
                        {"keyword": "option 2", "volume": 800},
                    ],
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Research complete. Choose one of the topic options below to continue.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertEqual(mock_send_message.call_args_list[1][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[1][1]["thread_ts"], "123.456")
        mock_send_dm.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_topic_selection_multi_options(self, mock_send_dm):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "topic_selection",
            "job_id": "job-multi",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "selection": {
                "selected_keyword": "top pick",
                "options": [
                    {"keyword": "option 1", "volume": 1000},
                    {"keyword": "option 2", "volume": 2000},
                ]
            }
        }
        
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify job stores options
        job = ContentFactoryJob.objects.get(job_id="job-multi")
        self.assertEqual(len(job.selection_data['options']), 2)
        
        # Verify Slack message contains buttons for options
        # We need to find the blocks argument. It might be in kwargs 'blocks' or positional arg
        call_args = mock_send_dm.call_args
        blocks = call_args[1].get('blocks')
        if not blocks and len(call_args[0]) > 2:
            blocks = call_args[0][2]
        
        # Check action elements
        found_actions = False
        if blocks:
            for block in blocks:
                if block['type'] == 'actions':
                    found_actions = True
                    elements = block['elements']
                    # Should have 2 options + 1 cancel
                    self.assertEqual(len(elements), 3)
                    self.assertEqual(elements[0]['action_id'], "confirm_topic_btn_0")
                    self.assertEqual(elements[1]['action_id'], "confirm_topic_btn_1")
                    self.assertEqual(elements[2]['action_id'], "cancel_topic_btn")
        self.assertTrue(found_actions, "Action block not found in Slack message")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_article_complete_callback(self, mock_send_dm):
        # Create job first
        ContentFactoryJob.objects.create(job_id="job-456", domain="mlai.au", status="generating")
        
        url = reverse('content_factory_callback')
        data = {
            "event_type": "article_complete",
            "job_id": "job-456",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "article_url": "https://mlai.au/article",
            "pr_url": "https://github.com/pr/1",
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-456")
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.pr_url, "https://github.com/pr/1")
        
        mock_send_dm.assert_called_once()
        self.assertEqual(mock_send_dm.call_args[0][0], "U123")

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_scan_complete_callback_mentions_scaffold_when_already_queued(self, mock_send_dm):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scan_complete",
                "job_id": "scan-run-queued",
                "run_id": "scan-run-queued",
                "workflow": "repo_scan",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "components_generated": True,
                "components_count": 3,
                "component_names": ["ArticleHeroHeader", "ArticleFAQ", "ArticleFooterNav"],
                "scaffold_queued": True,
                "scaffold_job_id": "scaffold-run-1",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("queued article-directory setup", message)
        self.assertNotIn("Create Articles Directory", message)
        self.assertIsNone(mock_send_dm.call_args[1].get("blocks"))

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_generation_failed_auto_discovery_no_opportunities_mentions_research_scope(self, mock_send_dm):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "generation_failed",
                "job_id": "discovery-run-1",
                "run_id": "discovery-run-1",
                "workflow": "auto_discovery",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "error_code": "NO_OPPORTUNITIES",
                "error": "No relevant keywords found after filtering.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_dm.assert_called_once()
        message = mock_send_dm.call_args[0][1]
        self.assertIn("Research for mlai.au", message)
        self.assertIn("doesn't affect any scan or scaffold work already in progress", message)
        self.assertNotIn("Task failed for", message)

    @patch('integrations.services.slack.SlackService.send_message')
    def test_scaffold_complete_uses_parent_run_thread_context(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="scan-parent-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="completed",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "scaffold_complete",
                "job_id": "scaffold-run-1",
                "run_id": "scaffold-run-1",
                "workflow": "scaffold",
                "parent_run_id": "scan-parent-1",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "already_exists": True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")

    def test_error_callback(self):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "error",
            "job_id": "job-error",
            "domain": "mlai.au",
            "error_message": "Generation failed"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-error")
        self.assertEqual(job.status, 'error')
        self.assertEqual(job.error_message, "Generation failed")

    @patch('integrations.services.article_generation.set_article_delivery_mode')
    def test_delivery_mode_required_callback_auto_selects_mode(self, mock_set_delivery_mode):
        mock_set_delivery_mode.return_value = {
            "job_id": "job-delivery-mode",
            "status": "queued",
            "delivery_mode": "publish_code",
        }

        url = reverse('content_factory_callback')
        data = {
            "event_type": "delivery_mode_required",
            "job_id": "job-delivery-mode",
            "domain": "mlai.au",
            "slack_user_id": "U123",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-delivery-mode")
        self.assertEqual(job.status, "generating")
        mock_set_delivery_mode.assert_called_once_with("job-delivery-mode", "publish_code")

    @patch('integrations.services.article_generation.publish_article')
    def test_preview_ready_callback_auto_approves(self, mock_publish_article):
        mock_publish_article.return_value = {
            "job_id": "job-preview-ready",
            "status": "queued",
            "approval_state": "approved",
        }

        url = reverse('content_factory_callback')
        data = {
            "event_type": "preview_ready",
            "job_id": "job-preview-ready",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "preview_url": "https://preview.example.com",
            "pr_url": "https://github.com/example/pr/1",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-preview-ready")
        self.assertEqual(job.status, "generating")
        mock_publish_article.assert_called_once_with(
            "job-preview-ready",
            slack_user_id="U123",
            domain="mlai.au",
        )

    @patch('integrations.services.slack.SlackService.send_dm')
    def test_content_ready_callback_marks_job_completed(self, mock_send_dm):
        url = reverse('content_factory_callback')
        data = {
            "event_type": "content_ready",
            "job_id": "job-content-ready",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "title": "How to Find a Technical Cofounder",
            "article_markdown_path": "/tmp/run/article.md",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-content-ready")
        self.assertEqual(job.status, "completed")
        mock_send_dm.assert_called_once()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_posts_thread_reply_and_records_progress(self, mock_send_message):
        mock_send_message.return_value = (True, "live-progress-card")
        ContentFactoryJob.objects.create(
            job_id="job-progress",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        url = reverse('content_factory_callback')
        data = {
            "event_type": "article_progress",
            "job_id": "job-progress",
            "domain": "mlai.au",
            "slack_user_id": "U123",
            "progress_id": "job-progress:research_locked",
            "milestone_key": "research_locked",
            "milestone_index": 1,
            "milestone_count": 3,
            "message": "Research complete. Sources are gathered and the outline is locked.",
        }

        response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-progress")
        self.assertEqual(job.posted_progress_ids, ["job-progress:research_locked"])
        self.assertEqual(job.last_progress_milestone_index, 1)
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertIn(
            "Research complete. Sources are gathered and the outline is locked.",
            mock_send_message.call_args[0][1],
        )
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")
        blocks = mock_send_message.call_args[1]["blocks"]
        self.assertIn("Content Factory for mlai.au", blocks[0]["text"]["text"])
        self.assertIn(
            "Research complete. Sources are gathered and the outline is locked.",
            blocks[0]["text"]["text"],
        )

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_posts_thread_reply_and_records_progress(self, mock_send_message):
        mock_send_message.return_value = (True, "live-discovery-card")
        ContentFactoryJob.objects.create(
            job_id="job-discovery-progress",
            domain="mlai.au",
            slack_user_id="U123",
            status="queued",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-progress",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-progress:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "milestone_count": 2,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-discovery-progress")
        self.assertEqual(job.status, "researching")
        self.assertEqual(job.posted_progress_ids, ["job-discovery-progress:research_started"])
        self.assertEqual(job.last_progress_milestone_index, 1)
        mock_send_message.assert_called_once()
        self.assertEqual(mock_send_message.call_args[0][0], "C123")
        self.assertIn(
            "Research started. Context, competitors, and prior topic history are loaded.",
            mock_send_message.call_args[0][1],
        )
        self.assertEqual(mock_send_message.call_args[1]["thread_ts"], "123.456")
        self.assertIn(
            "Research started. Context, competitors, and prior topic history are loaded.",
            mock_send_message.call_args[1]["blocks"][0]["text"]["text"],
        )

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_dedupes_progress_id(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-discovery-dup",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-discovery-dup:research_started"],
            last_progress_milestone_index=1,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-dup",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-dup:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "duplicate_progress_id")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_ignores_stale_milestone(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-discovery-stale",
            domain="mlai.au",
            slack_user_id="U123",
            status="researching",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-discovery-stale:candidate_pool_ready"],
            last_progress_milestone_index=2,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-stale",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-stale:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "stale_milestone")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_discovery_progress_callback_missing_thread_context_is_safe(self, mock_send_message):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "discovery_progress",
                "job_id": "job-discovery-missing-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-discovery-missing-thread:research_started",
                "milestone_key": "research_started",
                "milestone_index": 1,
                "message": "Research started. Context, competitors, and prior topic history are loaded. I'm now researching candidate topics.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "missing_thread_context")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_dedupes_progress_id(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-progress-dup",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-progress-dup:research_locked"],
            last_progress_milestone_index=1,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-dup",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-dup:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "duplicate_progress_id")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_ignores_stale_milestone(self, mock_send_message):
        ContentFactoryJob.objects.create(
            job_id="job-progress-stale",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
            posted_progress_ids=["job-progress-stale:draft_grounded"],
            last_progress_milestone_index=2,
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-stale",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-stale:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "stale_milestone")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_article_progress_callback_missing_thread_context_is_safe(self, mock_send_message):
        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "article_progress",
                "job_id": "job-progress-missing-thread",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "progress_id": "job-progress-missing-thread:research_locked",
                "milestone_key": "research_locked",
                "milestone_index": 1,
                "message": "Research complete. Sources are gathered and the outline is locked.",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "missing_thread_context")
        mock_send_message.assert_not_called()

    @patch('integrations.services.slack.SlackService.send_message')
    def test_content_ready_callback_uses_root_message_ts_as_thread_fallback(self, mock_send_message):
        mock_send_message.return_value = (True, "live-content-card")
        ContentFactoryJob.objects.create(
            job_id="job-content-threaded",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "content_ready",
                "job_id": "job-content-threaded",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "title": "How to Find a Technical Cofounder",
                "article_markdown_path": "/tmp/run/article.md",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Article content is ready.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertEqual(mock_send_message.call_args_list[1][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[1][1]["thread_ts"], "123.456")

    @patch('integrations.services.slack.SlackService.send_message')
    def test_publish_bundle_ready_callback_posts_thread_reply(self, mock_send_message):
        mock_send_message.return_value = (True, "live-bundle-card")
        ContentFactoryJob.objects.create(
            job_id="job-bundle-ready",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C123",
            slack_root_message_ts="123.456",
            slack_thread_ts="123.456",
        )

        response = self.client.post(
            reverse('content_factory_callback'),
            {
                "event_type": "publish_bundle_ready",
                "job_id": "job-bundle-ready",
                "domain": "mlai.au",
                "slack_user_id": "U123",
                "title": "How to Find a Technical Cofounder",
                "publish_resolution": "patch_bundle",
                "suggested_target_path": "content/blog/how-to-find-a-technical-cofounder.mdx",
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="job-bundle-ready")
        self.assertEqual(job.status, "completed")
        self.assertEqual(mock_send_message.call_count, 2)
        self.assertEqual(mock_send_message.call_args_list[0][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[0][1]["thread_ts"], "123.456")
        self.assertIn(
            "Publish bundle is ready.",
            mock_send_message.call_args_list[0][1]["blocks"][0]["text"]["text"],
        )
        self.assertEqual(mock_send_message.call_args_list[1][0][0], "C123")
        self.assertEqual(mock_send_message.call_args_list[1][1]["thread_ts"], "123.456")


class TopicConfirmTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.CONTENT_FACTORY_URL = "http://content-factory.test"
        
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        
        self.integration = UserIntegration.objects.create(
            slack_user_id="U-CONFIRM",
            github_access_token="gh_token",
            github_repo="owner/repo",
        )
        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="owner/repo",
        )

    def _mock_response(self, status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    def test_confirm_topic_success(self):
        url = reverse('content_confirm')
        data = {
            "domain": "mlai.au",
            "confirmed_keyword": "ai agents",
            "slack_user_id": "U-CONFIRM",
            "request_source": "roo_slackbot",
        }
        
        with patch('integrations.api_views_content.confirm_topic') as mock_confirm_topic:
            mock_confirm_topic.return_value = {"job_id": "job-new", "status": "queued"}
            
            response = self.client.post(url, data, format='json')
            
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-new")
        
        mock_confirm_topic.assert_called_once_with(
            domain="mlai.au",
            confirmed_keyword="ai agents",
            slack_user_id="U-CONFIRM",
            custom_title=None,
            skip_alternatives=None,
            source_run_id=None,
            slack_channel_id=None,
            slack_thread_ts=None,
            slack_root_message_ts=None,
            progress_message_ts=None,
            request_source="roo_slackbot",
        )

    def test_confirm_topic_with_index(self):
        # Setup job with options
        job = ContentFactoryJob.objects.create(
            job_id="job-index",
            domain="mlai.au",
            slack_user_id="U-CONFIRM",
            status="awaiting_confirmation",
            selection_data={
                "options": [
                    {"keyword": "keyword 0"},
                    {"keyword": "keyword 1"}
                ]
            }
        )

        url = reverse('content_job_confirm', args=["job-index"])
        data = {
            "slack_user_id": "U-CONFIRM",
            "option_index": 1,
            "request_source": "roo_slackbot",
        }

        job.billing_status = "charged"
        job.billing_amount = 6
        job.billing_source_job_id = "job-index"
        job.save(update_fields=["billing_status", "billing_amount", "billing_source_job_id"])

        with patch('integrations.services.article_generation.confirm_topic') as mock_confirm_topic:
            mock_confirm_topic.return_value = {"job_id": "job-article", "status": "queued"}

            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], "job-article")
        self.assertEqual(response.data["run_id"], "job-article")
        self.assertEqual(response.data["source_run_id"], "job-index")
        job.refresh_from_db()
        self.assertEqual(job.selected_keyword, "keyword 1")
        self.assertEqual(job.status, "confirmed")

        article_job = ContentFactoryJob.objects.get(job_id="job-article")
        self.assertEqual(article_job.request_meta["source_run_id"], "job-index")

        mock_confirm_topic.assert_called_once()
        call_args = mock_confirm_topic.call_args
        self.assertEqual(call_args.kwargs['confirmed_keyword'], "keyword 1")


class SEOResearchMemoryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ['ROO_API_KEY'] = self.api_key
        os.environ['INTERNAL_API_KEY'] = self.api_key
        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.organization = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
        )
        self.keyword_a = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="ai agents",
            volume=2400,
            difficulty=35,
            opportunity_index=82.0,
            cluster_fingerprint="ai-agents",
        )
        self.keyword_b = ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword="agent workflows",
            volume=1800,
            difficulty=28,
            opportunity_index=74.0,
        )

    def test_research_feedback_updates_memory_fields(self):
        payload = {
            "domain": self.organization.domain,
            "session_id": "00000000-0000-0000-0000-000000000001",
            "shown_keywords": ["ai agents", "agent workflows"],
            "selected_keyword": "ai agents",
            "rejected_keywords": ["agent workflows"],
        }

        response = self.client.post("/api/seo/keywords/research-feedback/", payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.keyword_a.refresh_from_db()
        self.keyword_b.refresh_from_db()

        self.assertEqual(self.keyword_a.times_shown, 1)
        self.assertEqual(self.keyword_a.times_selected, 1)
        self.assertIsNotNone(self.keyword_a.last_shown_at)
        self.assertIsNotNone(self.keyword_a.last_selected_at)

        self.assertEqual(self.keyword_b.times_shown, 1)
        self.assertEqual(self.keyword_b.times_rejected, 1)
        self.assertIsNotNone(self.keyword_b.cooldown_until)

    def test_keyword_list_supports_offset(self):
        response = self.client.get(
            f"/api/seo/keywords/?domain={self.organization.domain}&limit=1&offset=1"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["keywords"]), 1)
