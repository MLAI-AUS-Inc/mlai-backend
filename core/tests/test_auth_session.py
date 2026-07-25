"""Session-durability contract: a login must survive for weeks, not one day.

These tests pin the two halves of that guarantee — the configured token lifetimes,
and the refresh endpoint that actually converts a long-lived refresh cookie into a
fresh access cookie without bouncing the user to the magic-link screen.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.auth_cookies import ACCESS_COOKIE, REFRESH_COOKIE

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
        refresh = RefreshToken.for_user(self.user)
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


class LogoutClearsSessionTests(TestCase):
    def test_logout_deletes_both_auth_cookies(self):
        user = User.objects.create_user(email='logout@example.com')
        client = APIClient()
        refresh = RefreshToken.for_user(user)
        client.cookies[ACCESS_COOKIE] = str(refresh.access_token)
        client.cookies[REFRESH_COOKIE] = str(refresh)

        response = client.post('/api/v1/auth/logout/')

        self.assertEqual(response.status_code, 200)
        for key in (ACCESS_COOKIE, REFRESH_COOKIE):
            self.assertEqual(response.cookies[key].value, '')
