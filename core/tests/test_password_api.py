from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.auth_cookies import ACCESS_COOKIE
from core.models import (
    PasswordResetChallenge,
    PasswordResetDeliveryStatus,
    PasswordResetEmailDelivery,
)
from core.refresh_sessions import issue_refresh_token


User = get_user_model()
REQUEST_URL = '/api/v1/auth/password/reset/request/'
CONFIRM_URL = '/api/v1/auth/password/reset/confirm/'
CHANGE_URL = '/api/v1/auth/password/change/'


def token_from_reset_link(link):
    fragment = urlparse(link).fragment
    query = fragment.split('?', 1)[1]
    return parse_qs(query)['token'][0]


def deliver_and_read_token(mock_send):
    mock_send.assert_not_called()
    call_command('run_password_reset_email_worker', '--once', stdout=StringIO())
    mock_send.assert_called_once()
    return token_from_reset_link(mock_send.call_args.args[1])


class PasswordResetApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    @patch('core.password_delivery.send_password_reset_email')
    def test_request_is_generic_for_existing_and_missing_accounts(self, mock_send):
        user = User.objects.create_user(email='member@example.com')

        existing = self.client.post(REQUEST_URL, {'email': user.email}, format='json')
        missing = self.client.post(REQUEST_URL, {'email': 'missing@example.com'}, format='json')

        self.assertEqual(existing.status_code, 202)
        self.assertEqual(missing.status_code, 202)
        self.assertEqual(existing.data, missing.data)
        self.assertEqual(PasswordResetChallenge.objects.count(), 1)
        self.assertEqual(PasswordResetEmailDelivery.objects.count(), 1)
        deliver_and_read_token(mock_send)
        delivery = PasswordResetEmailDelivery.objects.get()
        self.assertEqual(delivery.status, PasswordResetDeliveryStatus.SENT)
        self.assertEqual(delivery.encrypted_reset_link, '')

    @patch('core.password_delivery.send_password_reset_email')
    def test_valid_token_sets_password_verifies_email_and_is_one_use(self, mock_send):
        user = User.objects.create_user(email='setup@example.com')
        self.client.post(REQUEST_URL, {'email': user.email}, format='json')
        token = deliver_and_read_token(mock_send)

        response = self.client.post(
            CONFIRM_URL,
            {'token': token, 'new_password': 'A-secure-new-password-42!'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('A-secure-new-password-42!'))
        self.assertIsNotNone(user.password_set_at)
        self.assertIsNotNone(user.email_verified_at)
        self.assertEqual(user.auth_version, 2)
        replay = self.client.post(
            CONFIRM_URL,
            {'token': token, 'new_password': 'Another-secure-password-42!'},
            format='json',
        )
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.data['error'], 'invalid_or_expired_token')

    @patch('core.password_delivery.send_password_reset_email')
    def test_expired_token_is_rejected(self, mock_send):
        user = User.objects.create_user(email='expired@example.com')
        self.client.post(REQUEST_URL, {'email': user.email}, format='json')
        token = deliver_and_read_token(mock_send)
        PasswordResetChallenge.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        response = self.client.post(
            CONFIRM_URL,
            {'token': token, 'new_password': 'A-secure-new-password-42!'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'invalid_or_expired_token')

    @patch('core.password_delivery.send_password_reset_email')
    def test_suspended_and_placeholder_accounts_receive_no_challenge(self, mock_send):
        User.objects.create_user(email='placeholder@slack.placeholder.com')
        suspended = User.objects.create_user(email='suspended@example.com')
        suspended.is_active = False
        suspended.save(update_fields=('is_active',))

        for email in ('placeholder@slack.placeholder.com', 'suspended@example.com'):
            response = self.client.post(REQUEST_URL, {'email': email}, format='json')
            self.assertEqual(response.status_code, 202)

        self.assertFalse(PasswordResetChallenge.objects.exists())
        self.assertFalse(PasswordResetEmailDelivery.objects.exists())
        mock_send.assert_not_called()

    @patch('core.password_delivery.send_password_reset_email')
    def test_password_reset_revokes_pre_reset_access_tokens(self, mock_send):
        user = User.objects.create_user(
            email='revoke@example.com',
            password='Existing-secure-password-42!',
        )
        old_access = str(issue_refresh_token(user).access_token)
        self.client.post(REQUEST_URL, {'email': user.email}, format='json')
        token = deliver_and_read_token(mock_send)
        self.client.post(
            CONFIRM_URL,
            {'token': token, 'new_password': 'Replacement-secure-password-42!'},
            format='json',
        )

        authenticated = APIClient()
        authenticated.cookies[ACCESS_COOKIE] = old_access
        response = authenticated.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 401)

    @patch('core.password_delivery.send_password_reset_email')
    def test_reset_request_is_rate_limited_by_email(self, _mock_send):
        for _ in range(5):
            response = self.client.post(
                REQUEST_URL,
                {'email': 'rate-limited@example.com'},
                format='json',
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            REQUEST_URL,
            {'email': 'rate-limited@example.com'},
            format='json',
        )
        self.assertEqual(blocked.status_code, 429)

    @patch(
        'core.password_delivery.send_password_reset_email',
        side_effect=RuntimeError('provider unavailable secret=do-not-store'),
    )
    def test_delivery_failure_retries_without_persisting_sensitive_error(self, mock_send):
        user = User.objects.create_user(email='retry@example.com')
        self.client.post(REQUEST_URL, {'email': user.email}, format='json')

        call_command('run_password_reset_email_worker', '--once', stdout=StringIO())

        delivery = PasswordResetEmailDelivery.objects.get()
        self.assertEqual(delivery.status, PasswordResetDeliveryStatus.PENDING)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_error_code, 'RuntimeError')
        self.assertNotIn('do-not-store', delivery.last_error_code)
        self.assertTrue(delivery.encrypted_reset_link)
        mock_send.assert_called_once()


class PasswordChangeApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email='change@example.com',
            password='Existing-secure-password-42!',
        )
        refresh = issue_refresh_token(self.user)
        self.client = APIClient()
        self.client.cookies[ACCESS_COOKIE] = str(refresh.access_token)

    def test_cookie_authenticated_change_requires_exact_trusted_origin(self):
        payload = {
            'current_password': 'Existing-secure-password-42!',
            'new_password': 'Replacement-secure-password-42!',
        }

        missing = self.client.post(CHANGE_URL, payload, format='json')
        untrusted = self.client.post(
            CHANGE_URL,
            payload,
            format='json',
            HTTP_ORIGIN='https://attacker.example',
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(untrusted.status_code, 403)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Existing-secure-password-42!'))

    def test_change_requires_current_password_and_invalidates_session(self):
        wrong = self.client.post(
            CHANGE_URL,
            {
                'current_password': 'wrong-password',
                'new_password': 'Replacement-secure-password-42!',
            },
            format='json',
            HTTP_ORIGIN='https://chat.mlai.au',
        )
        self.assertEqual(wrong.status_code, 400)

        changed = self.client.post(
            CHANGE_URL,
            {
                'current_password': 'Existing-secure-password-42!',
                'new_password': 'Replacement-secure-password-42!',
            },
            format='json',
            HTTP_ORIGIN='https://chat.mlai.au',
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.data['reauthentication_required'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Replacement-secure-password-42!'))
        self.assertEqual(self.user.auth_version, 2)
        self.assertEqual(changed.cookies[ACCESS_COOKIE].value, '')


class PasswordSetupInviteCommandTests(TestCase):
    @patch('core.password_delivery.send_password_reset_email')
    def test_dry_run_does_not_create_tokens_and_send_mode_does(self, mock_send):
        User.objects.create_user(email='needs-password@example.com')
        User.objects.create_user(
            email='already-ready@example.com',
            password='Existing-secure-password-42!',
        )
        output = StringIO()

        call_command('send_password_setup_invites', '--dry-run', stdout=output)
        self.assertIn('1 eligible accounts', output.getvalue())
        self.assertFalse(PasswordResetChallenge.objects.exists())

        call_command('send_password_setup_invites', '--send', stdout=StringIO())
        self.assertEqual(PasswordResetChallenge.objects.count(), 1)
        self.assertEqual(PasswordResetEmailDelivery.objects.count(), 1)
        deliver_and_read_token(mock_send)
