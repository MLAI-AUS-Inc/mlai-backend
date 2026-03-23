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

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
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

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
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

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
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


class PointsAdminManagementViewSetTests(APITestCase):
    def setUp(self):
        self.super_admin_slack_id = 'U05QPB483K9'
        self.other_slack_id = 'UNOTSUPER'
        self.target_slack_id = 'UTARGET456'
        self.list_url = reverse('points-admin-list')
        self.detail_url = reverse(
            'points-admin-detail',
            kwargs={'slack_user_id': self.target_slack_id},
        )
        self.target_user = User.objects.create_user(
            email='target@example.com',
            slack_id=self.target_slack_id,
        )

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_creates_admin(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.role, 'committee')
        self.assertEqual(admin.user, self.target_user)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertFalse(response.data['already_admin'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_is_idempotent_when_already_active(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            added_by_slack_id='UOLDER',
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).count(), 1)
        self.assertTrue(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_reactivates_inactive_admin_and_preserves_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
            added_by_slack_id='UOLDER',
            weekly_allowance=175,
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.weekly_allowance, 175)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertFalse(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_requires_super_admin_requester(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.other_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).exists())
        self.assertIn('super admin', response.data['error'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            weekly_allowance=100,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertEqual(admin.weekly_allowance, 150)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertEqual(response.data['weekly_allowance'], 150)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_rejects_non_positive_values(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'weekly_allowance must be positive')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_requires_existing_admin(self, mock_permission):
        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_revokes_active_admin(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertFalse(admin.is_active)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_is_idempotent_when_already_inactive(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['already_revoked'])
        self.assertFalse(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_existing_admin(self, mock_permission):
        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_super_admin_requester(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.other_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertIn('super admin', response.data['error'])
