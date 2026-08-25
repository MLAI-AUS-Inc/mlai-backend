"""
Tests for GetOrCreateSlackUserView endpoint.
"""
from unittest.mock import patch

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

    @patch('core.views.logger')
    def test_slack_registration_logs_only_internal_identity(self, mock_logger):
        slack_id = 'ULOGSAFE12'
        email = 'private-member@example.com'

        response = self.client.post(
            self.url,
            {'slack_id': slack_id, 'email': email},
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        logged = repr(mock_logger.method_calls)
        self.assertNotIn(slack_id, logged)
        self.assertNotIn(email, logged)
        mock_logger.info.assert_called_once_with(
            'slack_backed_user_created user_pk=%s',
            response.json()['user_id'],
        )

    def test_slack_registration_failure_log_omits_identity_and_exception_text(self):
        slack_id = 'ULOGFAIL12'
        email = 'private-failure@example.com'

        with (
            patch(
                'core.slack_users.ensure_slack_user',
                side_effect=RuntimeError(f'{slack_id} {email}'),
            ),
            patch('core.views.logger') as mock_logger,
        ):
            response = self.client.post(
                self.url,
                {'slack_id': slack_id, 'email': email},
                **self.headers,
            )

        self.assertEqual(response.status_code, 500)
        logged = repr(mock_logger.method_calls)
        self.assertNotIn(slack_id, logged)
        self.assertNotIn(email, logged)
        mock_logger.error.assert_called_once_with(
            'slack_user_creation_failed reason=%s',
            'RuntimeError',
        )

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

    def test_existing_direct_slack_identity_is_not_reassigned_by_email(self):
        existing = User.objects.create_user(
            email='claimed@example.com',
            slack_id='UCLAIMED12',
        )

        response = self.client.post(
            self.url,
            {
                'slack_id': 'UNEWUSER12',
                'email': existing.email,
                'first_name': 'New',
            },
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()['user_id'], existing.pk)
        self.assertEqual(
            response.json()['email'],
            'unewuser12@slack.placeholder.com',
        )
        existing.refresh_from_db()
        self.assertEqual(existing.slack_id, 'UCLAIMED12')

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


@override_settings(ROO_API_KEY='test-slack-user-key')
class LinkSlackUserTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_API_KEY': 'test-slack-user-key'}
        self.url = '/api/v1/users/link-slack/'

    def test_links_existing_account_by_case_insensitive_email(self):
        user = User.objects.create_user(email='member@example.com')

        response = self.client.post(
            self.url,
            {'slack_id': 'ULINK12345', 'email': 'MEMBER@example.com'},
            format='json',
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.slack_id, 'ULINK12345')
        self.assertEqual(response.json()['user_id'], user.id)

    def test_repeated_link_is_idempotent(self):
        user = User.objects.create_user(
            email='linked@example.com',
            slack_id='ULINKED123',
        )

        response = self.client.post(
            self.url,
            {'slack_id': 'ULINKED123', 'email': 'linked@example.com'},
            format='json',
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['already_linked'])
        self.assertEqual(response.json()['user_id'], user.id)

    def test_existing_slack_link_cannot_be_moved_to_another_account(self):
        original = User.objects.create_user(
            email='original@example.com',
            slack_id='UORIGINAL1',
        )
        other = User.objects.create_user(email='other@example.com')

        response = self.client.post(
            self.url,
            {'slack_id': 'UORIGINAL1', 'email': 'other@example.com'},
            format='json',
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user_id'], original.id)
        other.refresh_from_db()
        self.assertIsNone(other.slack_id)

    def test_account_linked_to_another_slack_identity_returns_conflict(self):
        user = User.objects.create_user(
            email='claimed@example.com',
            slack_id='UCLAIMED12',
        )

        response = self.client.post(
            self.url,
            {'slack_id': 'UNEWLINK12', 'email': 'claimed@example.com'},
            format='json',
            **self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'slack_identity_conflict')
        user.refresh_from_db()
        self.assertEqual(user.slack_id, 'UCLAIMED12')

    def test_missing_account_does_not_create_one(self):
        response = self.client.post(
            self.url,
            {'slack_id': 'UNOMATCH12', 'email': 'missing@example.com'},
            format='json',
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['code'], 'slack_account_not_found')
        self.assertFalse(User.objects.filter(email='missing@example.com').exists())

    @override_settings(INTERNAL_API_KEY='general-internal-key')
    def test_general_internal_key_cannot_mutate_slack_links(self):
        user = User.objects.create_user(email='strict-roo@example.com')

        response = self.client.post(
            self.url,
            {'slack_id': 'USTRICT123', 'email': user.email},
            format='json',
            HTTP_X_API_KEY='general-internal-key',
        )

        self.assertEqual(response.status_code, 401)
        user.refresh_from_db()
        self.assertIsNone(user.slack_id)
