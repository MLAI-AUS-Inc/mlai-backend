from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from community_chat import account_cookies, views
from community_chat.authentication import CommunityChatAccountAuthentication


@override_settings(DEBUG=False, COMMUNITY_CHAT_HOST_ONLY_COOKIES=True)
class AccountCookieIsolationTests(SimpleTestCase):
    def credentials(self):
        return SimpleNamespace(access_token='synthetic-access', refresh_token='synthetic-refresh')

    def test_production_credentials_are_host_only_and_legacy_cookies_expire(self):
        response = account_cookies.set_account_session_cookies(HttpResponse(), self.credentials())
        for name in ('__Host-mlai_chat_access', '__Host-mlai_chat_refresh'):
            self.assertIn(name, response.cookies)
            cookie = response.cookies[name]
            self.assertEqual(cookie['domain'], '')
            self.assertEqual(cookie['path'], '/')
            self.assertTrue(cookie['secure'])
            self.assertTrue(cookie['httponly'])
        for name in ('mlai_chat_access', 'mlai_chat_refresh'):
            self.assertEqual(response.cookies[name]['domain'], '.mlai.au')
            self.assertEqual(response.cookies[name]['max-age'], 0)

    @override_settings(COMMUNITY_CHAT_ACCESS_COOKIE_NAME='__Host-mlai_staging_access',
                       COMMUNITY_CHAT_REFRESH_COOKIE_NAME='__Host-mlai_staging_refresh')
    def test_staging_uses_distinct_names_for_issue_refresh_and_logout(self):
        response = account_cookies.set_account_session_cookies(HttpResponse(), self.credentials())
        self.assertIn('__Host-mlai_staging_access', response.cookies)
        self.assertNotIn('__Host-mlai_chat_access', response.cookies)
        with patch.object(views, 'rotate_account_session', return_value=SimpleNamespace(
            **vars(self.credentials()), session=SimpleNamespace(id='test', access_expires_at=None,
            expires_at=None, installation_id='test', client_id='mlai-chat-web'))):
            request = APIRequestFactory().post('/', {}, format='json',
                HTTP_ORIGIN='https://chat-staging.mlai.au',
                HTTP_COOKIE='__Host-mlai_staging_refresh=synthetic-refresh')
            response = views.AccountSessionRefreshView.as_view()(request)
            self.assertEqual(response.status_code, 200)
            self.assertIn('__Host-mlai_staging_refresh', response.cookies)
        response = account_cookies.clear_account_session_cookies(HttpResponse())
        self.assertEqual(response.cookies['__Host-mlai_staging_refresh']['max-age'], 0)
        self.assertEqual(response.cookies['__Host-mlai_staging_refresh']['domain'], '')

    def test_old_domain_cookie_cannot_authenticate_after_cutover(self):
        request = APIRequestFactory().get('/', HTTP_COOKIE='mlai_chat_access=synthetic-access')
        with patch('community_chat.authentication.authenticate_access_token', side_effect=AssertionError('legacy credential was read')) as lookup:
            self.assertIsNone(CommunityChatAccountAuthentication().authenticate(request))
            lookup.assert_not_called()

    def test_new_access_cookie_is_the_reader_used_by_authentication(self):
        request = APIRequestFactory().get('/', HTTP_COOKIE='__Host-mlai_chat_access=synthetic-access')
        session = SimpleNamespace(user=SimpleNamespace(), public_key='key', installation_id='id',
                                  client_id='mlai-chat-web', origin='https://chat.mlai.au', platform='web', name='Browser')
        with patch('community_chat.authentication.authenticate_access_token', return_value=session), \
             patch('community_chat.onboarding.require_community_access'):
            self.assertEqual(CommunityChatAccountAuthentication().authenticate(request), (session.user, session))

    @override_settings(COMMUNITY_CHAT_HOST_ONLY_COOKIES=False)
    def test_disabled_cutover_preserves_existing_cookie_contract(self):
        response = account_cookies.set_account_session_cookies(HttpResponse(), self.credentials())
        self.assertEqual(response.cookies['mlai_chat_access']['domain'], '.mlai.au')
        self.assertNotIn('__Host-mlai_chat_access', response.cookies)

    def test_isolated_cookie_names_cannot_disable_host_prefix_or_collide(self):
        for access,refresh in [('mlai_chat_access','__Host-refresh'),('__Host-access','__Host-access')]:
            with self.subTest(access=access),override_settings(COMMUNITY_CHAT_ACCESS_COOKIE_NAME=access,COMMUNITY_CHAT_REFRESH_COOKIE_NAME=refresh):
                with self.assertRaises(ImproperlyConfigured):
                    account_cookies.set_account_session_cookies(HttpResponse(),self.credentials())
