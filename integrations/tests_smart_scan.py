from django.test import TestCase
from unittest.mock import patch, MagicMock
from integrations.models import UserIntegration
from integrations.services.github import scan_github_project, get_latest_repo_sha

class SmartScanTests(TestCase):
    def setUp(self):
        self.user_id = "U12345"
        self.domain = "mlai.au"
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.user_id,
            github_access_token="gh_token",
            github_repo="owner/repo",
            project_scanned=False
        )

    @patch('integrations.services.github.http_requests.get')
    def test_get_latest_repo_sha(self, mock_get):
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'sha': 'new_sha_123'}
        mock_get.return_value = mock_response

        sha = get_latest_repo_sha("token", "repo")
        self.assertEqual(sha, 'new_sha_123')
        mock_get.assert_called_with(
            "https://api.github.com/repos/repo/commits/HEAD",
            headers={
                "Authorization": "Bearer token",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10
        )

    @patch('integrations.services.github.get_latest_repo_sha')
    @patch('integrations.services.github.http_requests.post')
    def test_scan_updates_sha(self, mock_post, mock_get_sha):
        # Mock GitHub SHA fetch
        mock_get_sha.return_value = 'commit_sha_xyz'

        # Mock Content Factory response
        mock_cf_response = MagicMock()
        mock_cf_response.status_code = 200
        mock_cf_response.json.return_value = {'status': 'ok'}
        mock_post.return_value = mock_cf_response

        # Run scan
        scan_github_project(self.user_id, domain=self.domain)

        # Reload and check
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.project_scanned)
        self.assertEqual(self.integration.last_scanned_sha, 'commit_sha_xyz')
        self.assertIsNotNone(self.integration.last_scanned_at)

    @patch('integrations.services.github.get_latest_repo_sha')
    @patch('integrations.services.github.http_requests.post')
    def test_scan_handles_sha_failure(self, mock_post, mock_get_sha):
        # Mock SHA fetch failing
        mock_get_sha.side_effect = Exception("GitHub API Error")

        # Mock Content Factory success (should still proceed)
        mock_cf_response = MagicMock()
        mock_cf_response.status_code = 200
        mock_cf_response.json.return_value = {'status': 'ok'}
        mock_post.return_value = mock_cf_response

        # Run scan
        scan_github_project(self.user_id, domain=self.domain)

        # Reload
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.project_scanned)
        # Should be None if fetch failed
        self.assertIsNone(self.integration.last_scanned_sha)

    @patch('integrations.services.github.get_latest_repo_sha')
    def test_status_endpoint_has_updates(self, mock_get_sha):
        from rest_framework.test import APIRequestFactory
        from integrations.api_views import GithubTokenIdentityView

        # Setup: Last scan was old
        self.integration.last_scanned_sha = 'old_sha_111'
        self.integration.save()

        # Mock: GitHub has new SHA
        mock_get_sha.return_value = 'new_sha_999'

        factory = APIRequestFactory()
        view = GithubTokenIdentityView.as_view()
        request = factory.get(f'/api/v1/integrations/github/{self.user_id}/')
        
        # We need to manually set user/auth if permission classes enforce it
        # But HasRooApiKey checks headers.
        # For unit test, we can bypass or set header if needed.
        # Let's try mocking the permission check or just setting the header.
        request.META['HTTP_X_API_KEY'] = 'test_key' # assumes settings.internal_api_key matches or is mocked

        # Force authentication bypass for unit test simplicity if needed, 
        # or use force_authenticate if using APIClient. 
        # Since we use RequestFactory, we rely on view processing.
        # Permission check might fail if we don't mock settings.
        
        with patch('core.permissions.HasRooApiKey.has_permission', return_value=True):
             response = view(request, slack_user_id=self.user_id)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['has_updates'])
        self.assertEqual(response.data['current_sha'], 'new_sha_999')
        self.assertEqual(response.data['last_scanned_sha'], 'old_sha_111')

    @patch('integrations.services.github.get_latest_repo_sha')
    def test_status_endpoint_no_updates(self, mock_get_sha):
        from rest_framework.test import APIRequestFactory
        from integrations.api_views import GithubTokenIdentityView

        # Setup: Synced
        self.integration.last_scanned_sha = 'same_sha_123'
        self.integration.save()
        mock_get_sha.return_value = 'same_sha_123'

        factory = APIRequestFactory()
        view = GithubTokenIdentityView.as_view()
        request = factory.get(f'/api/v1/integrations/github/{self.user_id}/')
        
        with patch('core.permissions.HasRooApiKey.has_permission', return_value=True):
             response = view(request, slack_user_id=self.user_id)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['has_updates'])

    @patch('integrations.services.github.get_latest_repo_sha')
    def test_status_endpoint_token_expired(self, mock_get_sha):
        from rest_framework.test import APIRequestFactory
        from integrations.api_views import GithubTokenIdentityView
        import requests

        # Setup: Last scan was old
        self.integration.last_scanned_sha = 'old_sha_111'
        self.integration.save()

        # Mock: GitHub raises 401
        error_response = MagicMock()
        error_response.status_code = 401
        
        # requests.exceptions.HTTPError requires (msg, response=response)
        mock_get_sha.side_effect = requests.exceptions.HTTPError("Unauthorized", response=error_response)

        factory = APIRequestFactory()
        view = GithubTokenIdentityView.as_view()
        request = factory.get(f'/api/v1/integrations/github/{self.user_id}/')
        
        with patch('core.permissions.HasRooApiKey.has_permission', return_value=True):
             response = view(request, slack_user_id=self.user_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['error'], "GitHub token expired")
        self.assertTrue("auth-url" in response.data or "auth_url" in response.data)
        self.assertIn(f"?slack_user_id={self.user_id}", response.data['auth_url'])
