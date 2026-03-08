from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch, MagicMock
from integrations.models import UserIntegration
from core.article_system import resolve_article_system_with_source
from core.models import Organization, OrganizationContentConfig
from integrations.services.article_generation import (
    ArticleSystemActionRequiredError,
    publish_article,
    trigger_article_generation,
)

class ArticleGenerationServiceTest(TestCase):
    def setUp(self):
        self.slack_user_id = "U_TEST_123"
        self.repo_name = "test/repo"
        
        # Create user integration
        self.integration = UserIntegration.objects.create(
            slack_user_id=self.slack_user_id,
            github_repo=self.repo_name,
            github_access_token="gh_token_123",
            github_token_expires_at=timezone.now() + timezone.timedelta(days=1),
            project_scanned=True
        )
        
        # Create org config
        self.org = Organization.objects.create(domain="mlai.au", name="Test Org")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo=self.repo_name,
            article_template="## Template Content",
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
        self.assertIn('/api/runs/article', args[0])
        payload = kwargs['json']
        
        self.assertEqual(payload['domain'], "mlai.au")
        self.assertEqual(payload['topic'], "AI Agents")
        self.assertEqual(payload['target_keyword'], "agentic")
        self.assertEqual(payload['context'], "Context info")
        self.assertEqual(payload['github_repo'], self.repo_name)
        self.assertNotIn('github_token', payload)

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
            result = publish_article("job_123", self.slack_user_id, domain="mlai.au")
            
        self.assertEqual(result['status'], "published")
        self.assertEqual(result['preview_url'], "http://p.com")
        
        # Verify URL
        args, _ = mock_post.call_args
        self.assertIn('/api/runs/job_123/approve', args[0])

    def test_trigger_generation_requires_article_system_when_missing(self):
        self.config.article_system = {}
        self.config.save(update_fields=["article_system"])

        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info"
        }

        with self.assertRaises(ArticleSystemActionRequiredError) as exc:
            trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(exc.exception.recommended_action, "scaffold")
        self.assertEqual(exc.exception.resolution_source, "default_missing")

    @patch('integrations.services.article_generation.get_github_credentials_for_domain')
    @patch('integrations.services.article_generation.http_requests.post')
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
        mock_response.json.return_value = {"job_id": "job_scan_fallback", "status": "queued"}
        mock_post.return_value = mock_response

        article_request = {
            "domain": "mlai.au",
            "topic": "AI Agents",
            "target_keyword": "agentic",
            "context": "Context info"
        }

        with self.settings(CONTENT_FACTORY_API_KEY="test-key"):
            result = trigger_article_generation(self.slack_user_id, article_request)

        self.assertEqual(result["job_id"], "job_scan_fallback")
