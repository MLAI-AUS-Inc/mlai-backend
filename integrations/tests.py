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
        
        # Create a user integration record
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo="owner/repo",
            github_access_token="gh_token_123",
            project_scanned=False
        )

    @patch('requests.post')
    def test_scan_success(self, mock_post):
        # Mock Content Factory response
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": "started", "job_id": "job_123"}

        # Configure headers for auth
        headers = {'HTTP_X_API_KEY': self.api_key}
        
        # Override settings for auth check
        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.post(
                self.url, 
                {'slack_user_id': self.slack_user_id}, 
                format='json',
                **headers
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'scan_triggered')
        self.assertEqual(response.data['github_repo'], 'owner/repo')
        
        # Verify DB update
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.project_scanned)
        
        # Verify external call
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn('/api/pipeline/scan', args[0])
        self.assertEqual(kwargs['json']['slack_user_id'], self.slack_user_id)
        self.assertEqual(kwargs['json']['github_repo'], "owner/repo")
        self.assertEqual(kwargs['json']['github_token'], "gh_token_123")

    def test_scan_unauthorized(self):
        response = self.client.post(
            self.url, 
            {'slack_user_id': self.slack_user_id}, 
            format='json'
        )
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_scan_no_integration(self):
        headers = {'HTTP_X_API_KEY': self.api_key}
        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.post(
                self.url, 
                {'slack_user_id': 'unknown_user'}, 
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
                {'slack_user_id': self.slack_user_id}, 
                format='json',
                **headers
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
