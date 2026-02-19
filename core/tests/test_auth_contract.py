from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


User = get_user_model()


class AuthContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link', return_value='http://localhost:5173/verify?token=abc')
    def test_create_user_returns_contract_shape(self, mock_generate, mock_send):
        response = self.client.post(
            '/api/v1/auth/create-user/',
            {
                'email': 'new@example.com',
                'firstName': 'Jane',
                'lastName': 'Doe',
                'phone': '412345678',
                'role': 'participant',
                'app': 'hospital',
                'next': '/hospital/app',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertIn('id', response.data)
        self.assertEqual(response.data['email'], 'new@example.com')
        self.assertEqual(response.data['full_name'], 'Jane Doe')
        self.assertIn('magic_link', response.data)
        self.assertIn('app=hospital', response.data['magic_link'])
        self.assertIn('next=/hospital/app', response.data['magic_link'])

        created_user = User.objects.get(email='new@example.com')
        self.assertFalse(created_user.is_active)
        mock_generate.assert_called_once()
        mock_send.assert_called_once()

    @override_settings(MEDHACK_URL='http://localhost:3000')
    @patch('core.views.send_magic_link_email')
    @patch('core.views.generate_magic_link')
    def test_send_magic_link_uses_hospital_frontend_origin(self, mock_generate, mock_send):
        user = User.objects.create_user(email='origin@example.com', role='participant')

        self.client.post(
            '/api/v1/auth/send-magic-link/',
            {'email': 'origin@example.com', 'app': 'hospital', 'next': '/hospital/app'},
            format='json',
        )

        mock_generate.assert_called_once_with(user, base_url='http://localhost:3000')
        mock_send.assert_called_once()

    @patch('core.views.verify_magic_link', return_value='verify@example.com')
    def test_verify_magic_link_returns_redirect_and_user_id(self, mock_verify):
        user = User.objects.create_user(
            email='verify@example.com',
            role='participant',
            first_name='Verify',
            last_name='User',
        )
        user.is_active = False
        user.save(update_fields=['is_active'])

        response = self.client.get(
            '/api/v1/auth/verify-magic-link/?token=test-token&app=hospital&next=/hospital/app'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.id)
        self.assertEqual(response.data['user']['email'], 'verify@example.com')
        self.assertEqual(response.data['redirect'], '/hospital/app')
        self.assertTrue(response.data['next_url'].endswith('/hospital/app'))
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        mock_verify.assert_called_once_with('test-token')
