from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import patch, MagicMock
from core.models import ContentFactoryJob
import sys
import uuid

@override_settings(ROO_API_KEY='test-key')
class ContentJobConfirmTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY='test-key')
        self.job_id = str(uuid.uuid4())
        self.job = ContentFactoryJob.objects.create(
            job_id=self.job_id,
            domain="example.com",
            status="awaiting_confirmation",
            slack_user_id="U123"
        )
        self.url = f'/api/v1/content/jobs/{self.job_id}/confirm'

    def test_confirm_success(self):
        # Mock the missing module
        mock_tasks = MagicMock()
        mock_run_task = MagicMock()
        mock_tasks.run_article_generation = mock_run_task
        
        # Inject mock into sys.modules
        with patch.dict(sys.modules, {'content_factory': MagicMock(), 'content_factory.tasks': mock_tasks}):
            # Setup request data
            data = {
                "keyword": "new keyword",
                "action": "write",
                "slack_user_id": "U999", # New user
                "domain": "example.com"
            }
            
            response = self.client.post(self.url, data, format='json')
            
            print(f"Response: {response.status_code} {response.data}")
            
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['status'], 'confirmed')
            
            # Verify DB update
            self.job.refresh_from_db()
            self.assertEqual(self.job.status, 'confirmed')
            self.assertEqual(self.job.selected_keyword, 'new keyword')
            self.assertEqual(self.job.slack_user_id, 'U999')
            
            # Verify Task Trigger
            mock_run_task.delay.assert_called_once_with(self.job_id)

    def test_confirm_job_not_found(self):
        # Mock module again
        mock_tasks = MagicMock()
        with patch.dict(sys.modules, {'content_factory': MagicMock(), 'content_factory.tasks': mock_tasks}):
            data = {"slack_user_id": "U123"}
            response = self.client.post('/api/v1/content/jobs/nonexistent/confirm', data, format='json')
            self.assertEqual(response.status_code, 404)

    def test_confirm_missing_data(self):
         # Mock module again
        mock_tasks = MagicMock()
        with patch.dict(sys.modules, {'content_factory': MagicMock(), 'content_factory.tasks': mock_tasks}):
            data = {} # Missing slack_user_id
            response = self.client.post(self.url, data, format='json')
            self.assertEqual(response.status_code, 400)
