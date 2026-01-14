from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch
from .models import UserIntegration

class GithubScanEndpointTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('github_scan')
        self.api_key = "test-api-key"
        self.slack_user_id = "U123456"
        self.domain = "mlai.au"
        
        # Create a user integration record
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo="owner/repo",
            github_access_token="gh_token_123",
            project_scanned=False
        )

    @patch('integrations.api_views.trigger_scan_async')
    def test_scan_success(self, mock_trigger_scan):
        mock_trigger_scan.return_value = None

        # Configure headers for auth
        headers = {'HTTP_X_API_KEY': self.api_key}
        
        # Override settings for auth check
        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.post(
                self.url, 
                {'slack_user_id': self.slack_user_id, 'domain': self.domain}, 
                format='json',
                **headers
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['status'], 'scan_initiated')
        self.assertEqual(response.data['github_repo'], 'owner/repo')
        self.assertEqual(response.data['domain'], self.domain)
        
        mock_trigger_scan.assert_called_once_with(
            self.slack_user_id,
            slack_channel_id=None,
            slack_thread_ts=None,
            domain=self.domain,
        )

    def test_scan_unauthorized(self):
        response = self.client.post(
            self.url, 
            {'slack_user_id': self.slack_user_id, 'domain': self.domain}, 
            format='json'
        )
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_scan_no_integration(self):
        headers = {'HTTP_X_API_KEY': self.api_key}
        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.post(
                self.url, 
                {'slack_user_id': 'unknown_user', 'domain': self.domain}, 
                format='json',
                **headers
            )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_scan_no_repo(self):
        # Unset repo
        self.integration.github_repo = None
        self.integration.save()
        
        headers = {'HTTP_X_API_KEY': self.api_key}
        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.post(
                self.url, 
                {'slack_user_id': self.slack_user_id, 'domain': self.domain}, 
                format='json',
                **headers
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
