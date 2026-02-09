"""
Tests for GetOrCreateSlackUserView endpoint.
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from core.models import User


@override_settings(ROO_API_KEY='test-slack-user-key')
class GetOrCreateSlackUserTests(TestCase):
    """Test automatic user creation from Slack data."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = 'test-slack-user-key'
        self.headers = {'HTTP_X_API_KEY': self.api_key}
        self.url = '/api/v1/users/slack-user/'

    def test_create_new_user_from_slack(self):
        """Test creating a brand new user from Slack data."""
        response = self.client.post(
            self.url,
            {
                'slack_id': 'U12345678',
                'email': 'newuser@example.com',
                'first_name': 'Jane',
                'last_name': 'Doe',
                'avatar_url': 'https://example.com/avatar.jpg'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data['created'])
        self.assertEqual(data['slack_id'], 'U12345678')
        self.assertEqual(data['email'], 'newuser@example.com')
        self.assertEqual(data['first_name'], 'Jane')
        self.assertEqual(data['last_name'], 'Doe')

        # Verify user was created in database
        user = User.objects.get(slack_id='U12345678')
        self.assertEqual(user.email, 'newuser@example.com')
        self.assertEqual(user.first_name, 'Jane')
        self.assertEqual(user.last_name, 'Doe')
        self.assertEqual(user.avatar_url, 'https://example.com/avatar.jpg')
        self.assertTrue(user.is_active)  # Auto-activated

    def test_get_existing_user_by_slack_id(self):
        """Test getting an existing user by Slack ID."""
        # Create user first
        user = User.objects.create_user(
            email='existing@example.com',
            slack_id='U87654321',
            first_name='John',
            last_name='Smith'
        )
        user.is_active = True
        user.save()

        response = self.client.post(
            self.url,
            {
                'slack_id': 'U87654321',
                'email': 'existing@example.com',
                'first_name': 'John',
                'last_name': 'Smith'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['created'])
        self.assertEqual(data['user_id'], user.id)
        self.assertEqual(data['slack_id'], 'U87654321')

    def test_link_slack_id_to_existing_email(self):
        """Test linking a Slack ID to an existing user found by email."""
        # Create user without slack_id
        user = User.objects.create_user(
            email='linkit@example.com',
            first_name='Link',
            last_name='Me'
        )
        user.is_active = True
        user.save()

        response = self.client.post(
            self.url,
            {
                'slack_id': 'UNEW12345',
                'email': 'linkit@example.com',
                'first_name': 'Link',
                'last_name': 'Me'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['created'])
        self.assertTrue(data.get('linked'))
        self.assertEqual(data['slack_id'], 'UNEW12345')

        # Verify slack_id was added
        user.refresh_from_db()
        self.assertEqual(user.slack_id, 'UNEW12345')

    def test_missing_required_fields(self):
        """Test that missing slack_id or email returns 400."""
        # Missing slack_id
        response = self.client.post(
            self.url,
            {
                'email': 'test@example.com'
            },
            **self.headers
        )
        self.assertEqual(response.status_code, 400)

        # Missing email
        response = self.client.post(
            self.url,
            {
                'slack_id': 'U12345678'
            },
            **self.headers
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_api_key(self):
        """Test that the endpoint requires API key authentication."""
        response = self.client.post(
            self.url,
            {
                'slack_id': 'U12345678',
                'email': 'test@example.com'
            }
        )
        # Should be forbidden without API key
        self.assertEqual(response.status_code, 403)

    def test_case_insensitive_email_lookup(self):
        """Test that email lookup is case-insensitive."""
        # Create user with lowercase email
        user = User.objects.create_user(
            email='lowercase@example.com',
            first_name='Test'
        )
        user.is_active = True
        user.save()

        # Send uppercase email
        response = self.client.post(
            self.url,
            {
                'slack_id': 'UCASE123',
                'email': 'LOWERCASE@EXAMPLE.COM'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('linked'))

        # Verify it linked to the same user
        user.refresh_from_db()
        self.assertEqual(user.slack_id, 'UCASE123')

    def test_minimal_data_creation(self):
        """Test creating user with only required fields."""
        response = self.client.post(
            self.url,
            {
                'slack_id': 'UMIN12345',
                'email': 'minimal@example.com'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data['created'])
        self.assertEqual(data['first_name'], '')
        self.assertEqual(data['last_name'], '')

        # Verify in database
        user = User.objects.get(slack_id='UMIN12345')
        self.assertEqual(user.email, 'minimal@example.com')
        self.assertTrue(user.is_active)
