from django.test import TestCase
from unittest.mock import patch, MagicMock
from integrations.models import UserIntegration
from integrations.services.github import scan_github_project, ScanError

class GithubServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U123456"
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo="owner/repo",
            github_access_token="gh_token_123",
            project_scanned=False
        )

    @patch('integrations.services.github.http_requests.post')
    def test_scan_github_project_success(self, mock_post):
        # Mock Content Factory response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "started", "job_id": "job_123"}
        mock_post.return_value = mock_response

        # Call service with setting override
        from django.test.utils import override_settings
        with override_settings(INTERNAL_API_KEY="test-secret-key"):
            result = scan_github_project(self.slack_user_id)

        # Verify result
        self.assertEqual(result['status'], 'scan_triggered')
        self.assertEqual(result['content_factory_response']['status'], 'started')

        # Verify DB update
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.project_scanned)

        # Verify API call
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn('/api/pipeline/scan', args[0])
        self.assertEqual(kwargs['json']['slack_user_id'], self.slack_user_id)
        
        # Verify Headers
        headers = kwargs['headers']
        self.assertEqual(headers['X-API-KEY'], "test-secret-key")

    @patch('integrations.services.github.http_requests.post')
    def test_scan_github_project_failure(self, mock_post):
        # Mock failure
        import requests
        mock_post.side_effect = requests.exceptions.RequestException("Connection refused")

        with self.assertRaises(ScanError) as context:
            scan_github_project(self.slack_user_id)
        
        self.assertIn("Failed to trigger scan", str(context.exception))
