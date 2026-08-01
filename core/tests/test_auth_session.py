"""Session-durability contract: a login must survive for weeks, not one day.

These tests pin the two halves of that guarantee — the configured token lifetimes,
and the refresh endpoint that actually converts a long-lived refresh cookie into a
fresh access cookie without bouncing the user to the magic-link screen.
"""

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.auth_cookies import ACCESS_COOKIE, REFRESH_COOKIE
from core.refresh_sessions import REFRESH_SESSION_CLAIM, issue_refresh_token

User = get_user_model()

REFRESH_URL = '/api/v1/auth/token/refresh/'
ME_URL = '/api/v1/auth/me/'


class TokenLifetimeSettingsTests(TestCase):
    def test_refresh_token_outlives_a_single_day(self):
        """Regression guard: this was 1 day, which forced a daily re-login."""
        self.assertGreaterEqual(
            settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
            timedelta(days=30),
        )

    def test_refresh_outlives_access_so_it_can_actually_refresh(self):
        self.assertGreater(
            settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
            settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'],
        )

    def test_rotation_is_enabled_so_the_window_slides(self):
        self.assertTrue(settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS'])
        # Blacklisting would log out the loser of a concurrent-refresh race.
        self.assertFalse(settings.SIMPLE_JWT['BLACKLIST_AFTER_ROTATION'])


class CookieTokenRefreshTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email='founder@example.com')

    def _login_cookies(self):
        refresh = issue_refresh_token(self.user)
        self.client.cookies[ACCESS_COOKIE] = str(refresh.access_token)
        self.client.cookies[REFRESH_COOKIE] = str(refresh)
        return refresh

    def test_refresh_sets_both_cookies_with_explicit_lifetimes(self):
        original = self._login_cookies()

        response = self.client.post(REFRESH_URL)

        self.assertEqual(response.status_code, 200)

        access_cookie = response.cookies[ACCESS_COOKIE]
        self.assertTrue(access_cookie.value)
        self.assertEqual(
            int(access_cookie['max-age']),
            int(settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds()),
        )

        # Rotation must hand back a *new* refresh cookie, and it must carry the full
        # refresh lifetime — otherwise it degrades to a session cookie and the login
        # dies when the browser closes.
        refresh_cookie = response.cookies[REFRESH_COOKIE]
        self.assertTrue(refresh_cookie.value)
        self.assertNotEqual(refresh_cookie.value, str(original))
        self.assertEqual(
            int(refresh_cookie['max-age']),
            int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds()),
        )

    def test_refreshed_access_cookie_authenticates(self):
        self._login_cookies()

        refresh_response = self.client.post(REFRESH_URL)
        new_access = refresh_response.cookies[ACCESS_COOKIE].value

        client = APIClient()
        client.cookies[ACCESS_COOKIE] = new_access
        response = client.get(ME_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['email'], self.user.email)

    def test_missing_refresh_cookie_returns_401(self):
        response = self.client.post(REFRESH_URL)
        self.assertEqual(response.status_code, 401)

    def test_invalid_refresh_cookie_returns_401_and_clears_cookies(self):
        self.client.cookies[ACCESS_COOKIE] = 'stale.access.token'
        self.client.cookies[REFRESH_COOKIE] = 'not-a-real-token'

        response = self.client.post(REFRESH_URL)

        self.assertEqual(response.status_code, 401)
        # A dead session is cleared so the browser stops retrying on every page load.
        for key in (ACCESS_COOKIE, REFRESH_COOKIE):
            self.assertEqual(response.cookies[key].value, '')
            self.assertEqual(int(response.cookies[key]['max-age']), 0)

    @patch('core.refresh_sessions.cache.get', side_effect=ConnectionError('cache unavailable'))
    def test_refresh_fails_closed_when_revocation_store_is_unavailable(self, mock_cache_get):
        self._login_cookies()

        response = self.client.post(REFRESH_URL)

        self.assertEqual(response.status_code, 401)
        for key in (ACCESS_COOKIE, REFRESH_COOKIE):
            self.assertEqual(response.cookies[key].value, '')


class LogoutClearsSessionTests(TestCase):
    def _client_with_refresh(self, user):
        client = APIClient()
        refresh = issue_refresh_token(user)
        client.cookies[ACCESS_COOKIE] = str(refresh.access_token)
        client.cookies[REFRESH_COOKIE] = str(refresh)
        return client, refresh

    def test_logout_revokes_rotating_refresh_family_and_returns_contract(self):
        user = User.objects.create_user(email='logout@example.com')
        client, refresh = self._client_with_refresh(user)
        original_refresh = str(refresh)

        rotate_response = client.post(REFRESH_URL)
        self.assertEqual(rotate_response.status_code, 200)
        rotated_refresh = rotate_response.cookies[REFRESH_COOKIE].value
        self.assertNotEqual(rotated_refresh, original_refresh)
        self.assertEqual(
            RefreshToken(rotated_refresh)[REFRESH_SESSION_CLAIM],
            refresh[REFRESH_SESSION_CLAIM],
        )

        response = client.post('/api/v1/auth/logout/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {
                'message': 'Logged out successfully',
                'refresh_revoked': True,
            },
        )
        for key in (ACCESS_COOKIE, REFRESH_COOKIE):
            self.assertEqual(response.cookies[key].value, '')

        # Both the current refresh and an older rotated copy share the family
        # identifier, so neither can mint another access token after logout.
        for revoked_refresh in (original_refresh, rotated_refresh):
            with self.subTest(refresh=revoked_refresh[:12]):
                replay_client = APIClient()
                replay_client.cookies[REFRESH_COOKIE] = revoked_refresh
                replay_response = replay_client.post(REFRESH_URL)
                self.assertEqual(replay_response.status_code, 401)

    def test_logout_revokes_every_rotation_of_legacy_pre_family_tokens(self):
        user = User.objects.create_user(email='legacy-refresh-logout@example.com')
        client = APIClient()
        legacy_refresh = RefreshToken.for_user(user)
        original_refresh = str(legacy_refresh)
        self.assertNotIn(REFRESH_SESSION_CLAIM, legacy_refresh)
        client.cookies[ACCESS_COOKIE] = str(legacy_refresh.access_token)
        client.cookies[REFRESH_COOKIE] = original_refresh

        rotate_response = client.post(REFRESH_URL)
        self.assertEqual(rotate_response.status_code, 200)
        rotated_refresh = rotate_response.cookies[REFRESH_COOKIE].value
        self.assertNotIn(REFRESH_SESSION_CLAIM, RefreshToken(rotated_refresh))

        logout_response = client.post('/api/v1/auth/logout/')
        self.assertEqual(logout_response.status_code, 200)

        for revoked_refresh in (original_refresh, rotated_refresh):
            with self.subTest(refresh=revoked_refresh[:12]):
                replay_client = APIClient()
                replay_client.cookies[REFRESH_COOKIE] = revoked_refresh
                replay_response = replay_client.post(REFRESH_URL)
                self.assertEqual(replay_response.status_code, 401)

    def test_logout_succeeds_when_access_is_expired_but_refresh_is_valid(self):
        user = User.objects.create_user(email='expired-access-logout@example.com')
        client, refresh = self._client_with_refresh(user)
        expired_access = refresh.access_token
        expired_access.set_exp(
            from_time=timezone.now() - timedelta(days=2),
            lifetime=timedelta(seconds=1),
        )
        client.cookies[ACCESS_COOKIE] = str(expired_access)

        response = client.post('/api/v1/auth/logout/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['refresh_revoked'])

    def test_logout_requires_valid_refresh_but_always_clears_browser_state(self):
        for refresh_value in (None, 'not-a-valid-refresh-token'):
            with self.subTest(refresh_value=refresh_value):
                client = APIClient()
                client.cookies[ACCESS_COOKIE] = 'expired-or-invalid-access-token'
                if refresh_value:
                    client.cookies[REFRESH_COOKIE] = refresh_value

                response = client.post('/api/v1/auth/logout/')

                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.data,
                    {'error': 'Valid refresh credential required.'},
                )
                for key in (ACCESS_COOKIE, REFRESH_COOKIE, 'sessionid', 'csrftoken'):
                    self.assertIn(key, response.cookies)
                    self.assertEqual(response.cookies[key].value, '')

    @patch('core.refresh_sessions.cache.set', side_effect=ConnectionError('cache unavailable'))
    def test_logout_preserves_refresh_for_retry_when_revocation_store_is_unavailable(self, mock_cache_set):
        user = User.objects.create_user(email='cache-failure-logout@example.com')
        client, refresh = self._client_with_refresh(user)

        response = client.post('/api/v1/auth/logout/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.data,
            {'error': 'Logout revocation is temporarily unavailable.'},
        )
        for key in (ACCESS_COOKIE, 'sessionid', 'csrftoken'):
            self.assertEqual(response.cookies[key].value, '')
        self.assertNotIn(REFRESH_COOKIE, response.cookies)
        self.assertEqual(client.cookies[REFRESH_COOKIE].value, str(refresh))

    @override_settings(
        DEBUG=False,
        SESSION_COOKIE_DOMAIN='.mlai.au',
        SESSION_COOKIE_PATH='/',
        SESSION_COOKIE_SAMESITE='Lax',
        # Production CSRF remains host-only to api.mlai.au. It must never be
        # broadened to the parent domain where Plane could receive it.
        CSRF_COOKIE_DOMAIN=None,
        CSRF_COOKIE_PATH='/',
        CSRF_COOKIE_SAMESITE='Lax',
    )
    def test_logout_deletes_each_cookie_at_its_actual_production_scope(self):
        user = User.objects.create_user(email='production-logout@example.com')
        client, _refresh = self._client_with_refresh(user)
        client.force_login(user)
        session_key = client.session.session_key
        self.assertTrue(Session.objects.filter(session_key=session_key).exists())

        response = client.post('/api/v1/auth/logout/')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Session.objects.filter(session_key=session_key).exists())

        for key in (ACCESS_COOKIE, REFRESH_COOKIE, 'sessionid'):
            self.assertIn(key, response.cookies)
            self.assertEqual(response.cookies[key].value, '')
            self.assertEqual(int(response.cookies[key]['max-age']), 0)
            self.assertEqual(response.cookies[key]['domain'], '.mlai.au')
            self.assertEqual(response.cookies[key]['path'], '/')

        csrf_cookie = response.cookies['csrftoken']
        self.assertEqual(csrf_cookie.value, '')
        self.assertEqual(int(csrf_cookie['max-age']), 0)
        self.assertEqual(csrf_cookie['domain'], '')
        self.assertEqual(csrf_cookie['path'], '/')
