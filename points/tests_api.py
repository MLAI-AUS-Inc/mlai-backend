from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from .models import PointsAdmin
from django.contrib.auth import get_user_model
from unittest.mock import patch

User = get_user_model()

class ManualAwardViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('manual-award')
        self.admin_slack_id = 'UADMIN123'
        self.target_slack_id = 'UTARGET456'
        
        # Create admin
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_award_with_slack_user_id_success(self, mock_permission):
        """Test award works with slack_user_id parameter."""
        data = {
            'slack_user_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10,
            'reason': 'Test award'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_award_with_whitespace_stripped(self, mock_permission):
        """Test that whitespace is stripped from IDs."""
        data = {
            'slack_user_id': f"  {self.admin_slack_id}  ",
            'target_slack_id': f" {self.target_slack_id} ",
            'points': 10,
            'reason': 'Test whitespace'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_award_with_legacy_admin_slack_id(self, mock_permission):
        """Test that legacy admin_slack_id still works."""
        data = {
            'admin_slack_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])
