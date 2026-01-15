import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.urls import reverse
from requests import Response

from core.models import Organization, OrganizationContentConfig
from integrations.models import UserIntegration
from roo.models import ChannelFirstPost
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
        os.environ['INTERNAL_API_KEY'] = self.api_key

        from django.conf import settings
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        settings.CONTENT_FACTORY_URL = "http://content-factory.test"

        self.client.credentials(HTTP_X_API_KEY=self.api_key)

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
        )

    def _mock_response(self, status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    def test_generate_falls_back_when_discovery_returns_no_opportunities(self):
        url = reverse('content_generate')
        data = {
            "slack_user_id": self.integration.slack_user_id,
            "domain": self.organization.domain,
            "topic": None,
        }

        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.side_effect = [
                self._mock_response(200, {"opportunities": []}),  # discovery
                self._mock_response(202, {"job_id": "job-123"}),  # generate
            ]

            response = self.client.post(url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-123")
        self.assertIn("/api/pipeline/publish/status/job-123", response.data.get('job_status_url', ''))
        self.assertEqual(mock_post.call_count, 2)

        discovery_call = mock_post.call_args_list[0]
        generate_call = mock_post.call_args_list[1]

        self.assertIn("/api/pipeline/discover", discovery_call.args[0])
        self.assertIn("/api/pipeline/generate", generate_call.args[0])

        generate_payload = generate_call.kwargs.get('json') or {}
        self.assertEqual(generate_payload.get('topic'), "")
        self.assertEqual(generate_payload.get('target_keyword'), "")


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
        }
        
        with patch('integrations.services.article_generation.http_requests.post') as mock_post:
            mock_post.return_value = self._mock_response(200, {"job_id": "job-new", "status": "queued"})
            
            response = self.client.post(url, data, format='json')
            
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['job_id'], "job-new")
        
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("/api/pipeline/confirm-topic", call_args[0][0])
        payload = call_args[1]['json']
        self.assertEqual(payload['confirmed_keyword'], "ai agents")
