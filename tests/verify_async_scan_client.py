
import os
import django
import sys
import unittest
from unittest.mock import MagicMock, patch

# Setup Django environment
sys.path.append('.')
from django.conf import settings
if not settings.configured:
    settings.configure(
        SECRET_KEY='test_key',
        INSTALLED_APPS=[
            'core.apps.CoreConfig',
            'integrations.apps.IntegrationsConfig',
            'django.contrib.admin',
            'django.contrib.auth',
            'django.contrib.contenttypes',
            'django.contrib.sessions',
            'django.contrib.messages',
            'django.contrib.staticfiles',
        ],
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': ':memory:',
            }
        },
        USE_TZ=True,
        CONTENT_FACTORY_URL='http://example.com',
        CONTENT_FACTORY_API_KEY='test_key',
        GITHUB_OAUTH_CLIENT_ID='test_client_id',
        GITHUB_OAUTH_CLIENT_SECRET='test_secret',
    )
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0)

from integrations.services.github import scan_github_project, ScanError
from integrations.models import UserIntegration

class TestAsyncScanClient(unittest.TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_ASYNC"
        self.repo_name = "test-owner/test-async-repo"
        
        # Ensure clean state
        UserIntegration.objects.filter(slack_user_id=self.slack_user_id).delete()
        
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_access_token="test_token",
            github_user_name="test-owner",
            github_repo=self.repo_name
        )

    def tearDown(self):
        self.integration.delete()

    @patch('integrations.services.github.get_latest_repo_sha')
    @patch('integrations.services.github.http_requests.post')
    @patch('integrations.services.github.http_requests.get')
    def test_200_ok_queued_triggers_polling(self, mock_get, mock_post, mock_sha):
        """
        Test that a 200 OK response with 'job_id' and 'status': 'queued'
        correctly triggers the polling loop.
        """
        print("\n--- Testing 200 OK Queued Response ---")
        mock_sha.return_value = "dummy_sha"
        
        # 1. Mock Initial POST Response (200 OK but queued)
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "job_id": "job_200_queued",
            "status": "queued",
            "message": "Queued successfully"
        }
        mock_post.return_value = post_resp

        # 2. Mock Polling GET Responses
        # Sequence: Queued -> Processing -> Completed
        r1 = MagicMock(); r1.status_code=200; r1.json.return_value = {"job_id": "job_200_queued", "status": "processing", "progress": "Analyzing..."}
        r2 = MagicMock(); r2.status_code=200; r2.json.return_value = {
            "job_id": "job_200_queued", 
            "status": "completed", 
            "result": {
                "config": {"github_repo": self.repo_name},
                "article_template": "ASYNC_TEMPLATE"
            }
        }
        
        mock_get.side_effect = [r1, r2]

        # 3. Setup Progress Callback
        progress_msgs = []
        def on_progress(msg):
            progress_msgs.append(msg)
            print(f"Callback received: {msg}")

        # 4. Run Scan
        result = scan_github_project(self.slack_user_id, progress_callback=on_progress)

        # 5. Assertions
        self.assertEqual(result['content_factory_response']['article_template'], "ASYNC_TEMPLATE")
        self.assertTrue(len(progress_msgs) > 0)
        self.assertIn("⏳ Analyzing...", progress_msgs[0])
        print("✅ Polling triggered and completed successfully for 200 OK response.")

    @patch('integrations.services.github.get_latest_repo_sha')
    @patch('integrations.services.github.http_requests.post')
    @patch('integrations.services.github.http_requests.get')
    def test_202_accepted_triggers_polling(self, mock_get, mock_post, mock_sha):
        """
        Test that a 202 Accepted response correctly triggers the polling loop (Legacy/Standard support).
        """
        print("\n--- Testing 202 Accepted Response ---")
        mock_sha.return_value = "dummy_sha"
        
        # 1. Mock Initial POST Response (202 Accepted)
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {
            "job_id": "job_202_accepted",
            "status": "queued"
        }
        mock_post.return_value = post_resp

        # 2. Mock Polling GET Responses
        r1 = MagicMock(); r1.status_code=200; r1.json.return_value = {
            "job_id": "job_202_accepted", 
            "status": "completed", 
            "result": {"config": {"github_repo": self.repo_name}}
        }
        mock_get.side_effect = [r1]

        # 3. Run Scan
        result = scan_github_project(self.slack_user_id)

        # 4. Assertions
        self.assertEqual(result['status'], 'scan_completed')
        print("✅ Polling triggered and completed successfully for 202 Accepted response.")

    @patch('integrations.services.github.get_latest_repo_sha')
    @patch('integrations.services.github.http_requests.post')
    def test_200_ok_sync_legacy(self, mock_post, mock_sha):
        """
        Test that a 200 OK response WITHOUT job_id is treated as immediate synchronous success.
        """
        print("\n--- Testing 200 OK Synchronous (Legacy) ---")
        mock_sha.return_value = "dummy_sha"
        
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "config": {"github_repo": self.repo_name},
            "article_template": "SYNC_TEMPLATE"
        }
        mock_post.return_value = post_resp

        result = scan_github_project(self.slack_user_id)
        
        self.assertEqual(result['content_factory_response']['article_template'], "SYNC_TEMPLATE")
        print("✅ Synchronous legacy response handled correctly.")

if __name__ == "__main__":
    unittest.main()
