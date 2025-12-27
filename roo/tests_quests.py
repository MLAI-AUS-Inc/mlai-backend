from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from django.utils import timezone
from .models import QuestProgress

class QuestPersistenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY='test-api-key')
        
        # Override settings for testing
        from django.conf import settings
        self.original_api_key = getattr(settings, 'INTERNAL_API_KEY', None)
        settings.INTERNAL_API_KEY = 'test-api-key'
        
        self.slack_user_id = 'U12345'
        self.quest_id = 'connector'

    def tearDown(self):
        from django.conf import settings
        if self.original_api_key:
            settings.INTERNAL_API_KEY = self.original_api_key
        else:
            delattr(settings, 'INTERNAL_API_KEY')

    def test_increment_quest_progress(self):
        """Test incrementing quest progress creates record and updates count."""
        url = reverse('quest_increment')
        data = {
            'slack_user_id': self.slack_user_id,
            'quest_id': self.quest_id,
            'increment_by': 2
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['current_count'], 2)
        self.assertFalse(response.data['completed'])
        
        # Verify DB
        progress = QuestProgress.objects.get(slack_user_id=self.slack_user_id, quest_id=self.quest_id)
        self.assertEqual(progress.current_count, 2)
        self.assertFalse(progress.completed)
        
        # Increment again
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['current_count'], 4)
        
        # Verify DB updated
        progress.refresh_from_db()
        self.assertEqual(progress.current_count, 4)

    def test_complete_quest(self):
        """Test marking a quest as completed."""
        url = reverse('quest_complete')
        data = {
            'slack_user_id': self.slack_user_id,
            'quest_id': self.quest_id
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['completed'])
        self.assertIsNotNone(response.data['completed_at'])
        
        # Verify DB
        progress = QuestProgress.objects.get(slack_user_id=self.slack_user_id, quest_id=self.quest_id)
        self.assertTrue(progress.completed)
        self.assertIsNotNone(progress.completed_at)

    def test_complete_already_completed_quest(self):
        """Test that completing an already completed quest returns 409."""
        # Setup completed quest
        QuestProgress.objects.create(
            slack_user_id=self.slack_user_id,
            quest_id=self.quest_id,
            completed=True,
            completed_at=timezone.now()
        )
        
        url = reverse('quest_complete')
        data = {
            'slack_user_id': self.slack_user_id,
            'quest_id': self.quest_id
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('Quest already completed', response.data['detail'])

    def test_increment_already_completed_quest(self):
        """Test that incrementing an already completed quest returns 409."""
        # Setup completed quest
        QuestProgress.objects.create(
            slack_user_id=self.slack_user_id,
            quest_id=self.quest_id,
            completed=True,
            completed_at=timezone.now()
        )
        
        url = reverse('quest_increment')
        data = {
            'slack_user_id': self.slack_user_id,
            'quest_id': self.quest_id,
            'increment_by': 1
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('Quest already completed', response.data['detail'])

    def test_get_completion_status(self):
        """Test checking completion status."""
        url = reverse('quest_completed', args=[self.slack_user_id, self.quest_id])
        
        # Case 1: No record
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['completed'])
        self.assertEqual(response.data['current_count'], 0)
        
        # Case 2: In progress
        QuestProgress.objects.create(
            slack_user_id=self.slack_user_id,
            quest_id=self.quest_id,
            current_count=3
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['completed'])
        self.assertEqual(response.data['current_count'], 3)
        
        # Case 3: Completed
        QuestProgress.objects.filter(slack_user_id=self.slack_user_id, quest_id=self.quest_id).update(
            completed=True,
            completed_at=timezone.now()
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['completed'])

    def test_get_user_quests(self):
        """Test retrieving all quests for a user."""
        QuestProgress.objects.create(
            slack_user_id=self.slack_user_id,
            quest_id='quest1',
            completed=True
        )
        QuestProgress.objects.create(
            slack_user_id=self.slack_user_id,
            quest_id='quest2',
            current_count=5
        )
        
        url = reverse('user_quests', args=[self.slack_user_id])
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['quests']), 2)
        
        # Test filter
        response = self.client.get(url + '?completed=true')
        self.assertEqual(len(response.data['quests']), 1)
        self.assertEqual(response.data['quests'][0]['quest_id'], 'quest1')
