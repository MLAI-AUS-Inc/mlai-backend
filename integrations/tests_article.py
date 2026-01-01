from django.test import TestCase
from unittest.mock import patch, MagicMock
from integrations.models import UserIntegration
from core.models import Organization, OrganizationContentConfig
from integrations.services.article_generation import trigger_article_generation, publish_article, ArticleGenerationError

class ArticleGenerationServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_123"
        self.repo_name = "test/repo"
        
        # Create user integration
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo=self.repo_name,
            github_access_token="gh_token_123",
            project_scanned=True
        )
        
        # Create org config
        self.org = Organization.objects.create(domain="mlai.au", name="Test Org")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo=self.repo_name,
            article_template="## Template Content",
        )
        
    @patch('integrations.services.article_generation.http_requests.post')
    def test_trigger_generation_payload(self, mock_post):
        # Mock Response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"job_id": "job_123", "status": "queued"}
        mock_post.return_value = mock_response
        
        # Request params
        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info"
        }
        
        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)
            
        self.assertEqual(result['job_id'], 'job_123')
        
        # Check Payload
        args, kwargs = mock_post.call_args
        payload = kwargs['json']
        
        self.assertEqual(payload['domain'], "mlai.au")
        self.assertEqual(payload['topic'], "AI Agents")
        self.assertEqual(payload['target_keyword'], "agentic")
        self.assertEqual(payload['context'], "Context info")
        
        # Check Artifacts
        self.assertEqual(payload['existing_artifacts']['article_template'], "## Template Content")

    @patch('integrations.services.article_generation.http_requests.post')
    def test_publish_article(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "published", 
            "preview_url": "http://p.com", 
            "pr_url": "http://gh.com/pr/1"
        }
        mock_post.return_value = mock_response
        
        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = publish_article("job_123")
            
        self.assertEqual(result['status'], "published")
        self.assertEqual(result['preview_url'], "http://p.com")
        
        # Verify URL
        args, _ = mock_post.call_args
        self.assertIn('/api/pipeline/publish/job_123', args[0])
