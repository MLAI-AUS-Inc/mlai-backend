"""Test-only hosted configuration: no Django setup, database or network."""
import unittest

from roo.cybertest_staging_config import staging_entry_settings


class StagingEntryConfigurationTests(unittest.TestCase):
    def test_chat_origin_requires_explicit_reserved_path(self):
        values=self.configured(CYBERTEST_APP_ORIGIN='https://chat-staging.mlai.au', CYBERTEST_BASE_PATH='/cybertest')
        result=staging_entry_settings(values)
        self.assertEqual(result['CYBERTEST_APP_ORIGIN'], 'https://chat-staging.mlai.au')
        self.assertEqual(result['CYBERTEST_BASE_PATH'], '/cybertest')
        for path in ['', '/', '/auth', '//evil.example', '/cybertest/', '/cybertest/..']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                staging_entry_settings({**values, 'CYBERTEST_BASE_PATH':path})
        with self.assertRaises(ValueError):
            staging_entry_settings({**values, 'CYBERTEST_APP_ORIGIN':'https://chat.mlai.au'})

    def configured(self, **changes):
        values = {
            'APP_ENV': 'chat-staging', 'DEBUG': 'false',
            'CYBERTEST_ENTRY_STAGING_ENABLED': 'true',
            'CYBERTEST_BILLING_API_KEY': 'test-only-service-key-' + 'a' * 40,
            'CYBERTEST_APP_ORIGIN': 'https://audit.staging.example',
            'CYBERTEST_STAGING_COMPANY_IDS': '07edcd7f-9ee3-5fd9-8b42-d8b01680c088',
            'CYBERTEST_BILLING_MODE': 'test',
        }
        return {**values, **changes}

    def test_disabled_configuration_drops_inherited_credential_and_live_flags(self):
        result = staging_entry_settings(self.configured(
            CYBERTEST_ENTRY_STAGING_ENABLED='false', CYBERTEST_BILLING_MODE='live'))
        self.assertFalse(result['CYBERTEST_ENTRY_STAGING_ENABLED'])
        self.assertEqual(result['CYBERTEST_BILLING_API_KEY'], '')
        self.assertEqual(result['CYBERTEST_BILLING_MODE'], 'disabled')
        self.assertFalse(result['CYBERTEST_LIVE_BILLING_ENABLED'])

    def test_explicit_test_configuration_preserves_exact_audit_origin(self):
        result = staging_entry_settings(self.configured())
        self.assertEqual(result['CYBERTEST_APP_ORIGIN'], 'https://audit.staging.example')
        self.assertEqual(result['CYBERTEST_STAGING_COMPANY_IDS'],
                         ['07edcd7f-9ee3-5fd9-8b42-d8b01680c088'])
        self.assertTrue(result['CYBERTEST_PERSISTENT_BINDINGS_ENABLED'])
        self.assertEqual(result['CYBERTEST_BILLING_MODE'], 'test')
        self.assertFalse(result['CYBERTEST_ENTRY_LOCAL_ENABLED'])
        self.assertEqual(result['CYBERTEST_BILLING_API_KEY'], self.configured()['CYBERTEST_BILLING_API_KEY'])

    def test_enabled_configuration_rejects_production_and_incomplete_settings(self):
        for changes in [
            {'APP_ENV': 'production'}, {'DEBUG': 'true'},
            {'CYBERTEST_BILLING_MODE': 'live'}, {'CYBERTEST_LIVE_BILLING_ENABLED': 'true'},
            {'CYBERTEST_ENTRY_LOCAL_ENABLED': 'true'},
            {'CYBERTEST_BILLING_API_KEY': ''}, {'CYBERTEST_STAGING_COMPANY_IDS': ''},
            {'CYBERTEST_STAGING_COMPANY_IDS': 'not-a-company'},
            {'CYBERTEST_ENTRY_STAGING_ENABLED': 'tru'},
        ]:
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError):
                staging_entry_settings(self.configured(**changes))

    def test_audit_origin_cannot_be_a_production_or_account_service(self):
        for origin in ['http://audit.staging.example', 'https://chat.mlai.au',
                       'https://api.mlai.au', 'https://chat-staging.mlai.au',
                       'https://api-staging.mlai.au', 'https://user:secret@audit.staging.example',
                       'https://audit.staging.example/path', 'https://audit.staging.example?token=x']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                staging_entry_settings(self.configured(CYBERTEST_APP_ORIGIN=origin))

    def test_shared_secrets_and_multiline_credentials_are_rejected_without_echoing_them(self):
        key = self.configured()['CYBERTEST_BILLING_API_KEY']
        for changes in [dict(COMMUNITY_CHAT_ADAPTER_TOKEN=key), dict(SECRET_KEY=key),
                        dict(CYBERTEST_BILLING_API_KEY=key + '\nINJECTED=true')]:
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError) as caught:
                staging_entry_settings(self.configured(**changes))
            self.assertNotIn(key, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
