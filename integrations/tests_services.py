from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch, MagicMock
from integrations.models import UserIntegration
from core.models import Organization, OrganizationContentConfig
from integrations.services.github import scan_github_project, ScanError

class GithubServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U123456"
        self.domain = "mlai.au"
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo="owner/repo",
            github_access_token="gh_token_123",
            project_scanned=False
        )
        self.organization = Organization.objects.create(domain=self.domain, name="MLAI")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="owner/repo",
            github_token_encrypted="org_token_123",
            github_token_expires_at=timezone.now() + timedelta(hours=1),
        )

    @patch('integrations.services.github.get_latest_repo_sha', return_value="sha_123")
    @patch('integrations.services.github.http_requests.post')
    def test_scan_github_project_success(self, mock_post, _mock_get_latest_sha):
        # Mock Content Factory response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "completed", "article_template": "tpl"}
        mock_post.return_value = mock_response

        # Call service with setting override
        from django.test.utils import override_settings
        with override_settings(CONTENT_FACTORY_API_KEY="test-content-factory-key"):
            result = scan_github_project(self.slack_user_id, domain=self.domain)

        # Verify result
        self.assertEqual(result['status'], 'scan_completed')
        self.assertEqual(result['content_factory_response']['status'], 'completed')
        self.assertEqual(result['domain'], self.domain)

        # Verify DB update
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.project_scanned)

        # Verify API call
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn('/api/runs/scan', args[0])
        self.assertEqual(kwargs['json']['slack_user_id'], self.slack_user_id)
        self.assertEqual(kwargs['json']['domain'], self.domain)
        
        # Verify Headers
        headers = kwargs['headers']
        self.assertEqual(headers['X-API-KEY'], "test-content-factory-key")

    @patch('integrations.services.github.get_latest_repo_sha', return_value="sha_123")
    @patch('integrations.services.github.http_requests.post')
    def test_scan_github_project_failure(self, mock_post, _mock_get_latest_sha):
        # Mock failure
        import requests
        mock_post.side_effect = requests.exceptions.RequestException("Connection refused")

        with self.assertRaises(ScanError) as context:
            scan_github_project(self.slack_user_id, domain=self.domain)
        
        self.assertIn("Failed to trigger scan", str(context.exception))

    @patch('integrations.services.github.get_latest_repo_sha', return_value="sha_123")
    @patch('integrations.services.github.http_requests.post')
    def test_scan_github_project_with_thread_context(self, mock_post, _mock_get_latest_sha):
        """Thread context stays in local job tracking and is not sent to Content Factory."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        # Return a legacy synchronous response without job_id to skip polling
        mock_response.json.return_value = {"status": "completed", "article_template": "test"}
        mock_post.return_value = mock_response

        # Call with thread params
        scan_github_project(
            self.slack_user_id, 
            domain=self.domain,
            slack_channel_id="C123", 
            slack_thread_ts="123.456"
        )

        args, kwargs = mock_post.call_args
        payload = kwargs['json']
        self.assertNotIn('slack_channel_id', payload)
        self.assertNotIn('slack_thread_ts', payload)
        self.assertEqual(payload['domain'], self.domain)
