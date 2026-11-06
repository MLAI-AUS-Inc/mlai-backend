"""Native startup returns are bounded navigation hints, never OAuth credentials."""
from uuid import uuid4
from django.test import SimpleTestCase, override_settings
from integrations.services.chat_oauth_return import (safe_chat_startup_return_path,
    native_chat_startup_return_url, oauth_return_redirect)
from integrations.services.external_connectors import normalize_connector_next
from integrations.views import _normalize_google_next
from community_chat.device_auth_v2 import probe_payload


class StartupReturnTests(SimpleTestCase):
    def test_native_and_browser_paths_share_the_exact_bounded_grammar(self):
        company, plan = str(uuid4()), str(uuid4())
        paths = [f'/my-startup/articles?company_id={company}&articlePlan={plan}',
            f'/my-startup/settings?company_id={company}&googleBaseline=refresh',
            f'/my-startup/create?company_id={company}&googleBaseline=refresh&step=websiteCheck']
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(safe_chat_startup_return_path(path, company_id=company), path)
                for scheme in ('mlaichat', 'mlaichat-dev'):
                    uri = scheme+':/'+path
                    self.assertEqual(native_chat_startup_return_url(uri), uri)
                    self.assertEqual(_normalize_google_next(uri), uri)
                    self.assertEqual(normalize_connector_next(uri), uri)
                    response = oauth_return_redirect(uri)
                    self.assertEqual(response['Location'], uri)
                    self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_foreign_company_duplicates_credentials_and_escape_routes_are_refused(self):
        company, other = str(uuid4()), str(uuid4())
        path = f'/my-startup/settings?company_id={company}&googleBaseline=refresh'
        self.assertIsNone(safe_chat_startup_return_path(path, company_id=other))
        for value in [path+'&token=secret', path+'&company_id='+company, path+'#fragment',
                path.replace('refresh','no'), path+'&articlePlan='+other, path+'&step=website',
                path.replace('/settings','/../settings'), path.replace('/settings','/%73ettings'),
                '//evil.test'+path, 'https://chat.mlai.au'+path, path.replace('settings','sett\nings')]:
            with self.subTest(value=value):
                self.assertIsNone(safe_chat_startup_return_path(value))
        for uri in ['mlaichat://evil/settings?company_id='+company, 'mlaichat://user@my-startup/settings?company_id='+company,
                    'mlaichat://my-startup:8080/settings?company_id='+company]:
            self.assertIsNone(native_chat_startup_return_url(uri))

    def test_password_capability_is_public_configuration_and_absent_when_off(self):
        with override_settings(COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=False, COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED=False):
            self.assertEqual(probe_payload(), {'flow': 1})
        with override_settings(COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=True, COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED=False):
            self.assertEqual(probe_payload(), {'flow': 1, 'passwordAuthEnabled': True})
