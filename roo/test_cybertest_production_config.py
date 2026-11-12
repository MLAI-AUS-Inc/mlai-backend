"""Production authority contracts; no Django, database, credentials or network."""
import unittest

from roo.cybertest_production_config import production_entry_settings


class ProductionEntryConfigurationTests(unittest.TestCase):
    def configured(self, **changes):
        return {
            'APP_ENV': 'production', 'DEBUG': 'false',
            'CYBERTEST_ENTRY_PRODUCTION_ENABLED': 'true',
            'CYBERTEST_APP_ORIGIN': 'https://chat.mlai.au',
            'CYBERTEST_BASE_PATH': '/cybertest',
            'CYBERTEST_BILLING_API_KEY': 'synthetic-production-key-' + 'p' * 40,
            'CYBERTEST_PRODUCTION_COMPANY_IDS': '10000000-0000-4000-8000-000000000001',
            'CYBERTEST_BILLING_MODE': 'disabled',
            **changes,
        }

    def test_default_does_not_activate_production_or_override_local_settings(self):
        self.assertEqual(production_entry_settings({}), {
            'CYBERTEST_ENTRY_PRODUCTION_ENABLED': False,
            'CYBERTEST_PRODUCTION_COMPANY_IDS': [],
        })

    def test_production_entry_can_be_deployed_before_purchases_are_enabled(self):
        result = production_entry_settings(self.configured())
        self.assertTrue(result['CYBERTEST_ENTRY_PRODUCTION_ENABLED'])
        self.assertTrue(result['CYBERTEST_PERSISTENT_BINDINGS_ENABLED'])
        self.assertFalse(result['CYBERTEST_LIVE_BILLING_ENABLED'])
        self.assertFalse(result['CYBERTEST_ENTRY_LOCAL_ENABLED'])
        self.assertFalse(result['CYBERTEST_ENTRY_STAGING_ENABLED'])
        self.assertEqual(result['CYBERTEST_BILLING_MODE'], 'disabled')
        self.assertEqual(result['CYBERTEST_PRODUCTION_COMPANY_IDS'],
                         ['10000000-0000-4000-8000-000000000001'])

    def test_live_billing_requires_explicit_live_and_phase_flags(self):
        config = self.configured(CYBERTEST_BILLING_MODE='live',
                                 CYBERTEST_LIVE_BILLING_ENABLED='true',
                                 CYBERTEST_PHASED_SCANS_ENABLED='true')
        result = production_entry_settings(config)
        self.assertEqual(result['CYBERTEST_BILLING_MODE'], 'live')
        self.assertTrue(result['CYBERTEST_LIVE_BILLING_ENABLED'])
        self.assertTrue(result['CYBERTEST_PHASED_SCANS_ENABLED'])
        for name in ['CYBERTEST_LIVE_BILLING_ENABLED', 'CYBERTEST_PHASED_SCANS_ENABLED']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                production_entry_settings({**config, name: 'false'})

    def test_incompatible_authority_and_synthetic_billing_are_rejected(self):
        for changes in [
            {'APP_ENV': 'chat-staging'}, {'APP_ENV': 'test'}, {'DEBUG': 'true'},
            {'CYBERTEST_ENTRY_STAGING_ENABLED': 'true'},
            {'CYBERTEST_ENTRY_LOCAL_ENABLED': 'true'},
            {'CYBERTEST_BILLING_MODE': 'test'}, {'CYBERTEST_BILLING_MODE': 'live'},
            {'CYBERTEST_LIVE_BILLING_ENABLED': 'true'},
            {'CYBERTEST_ENTRY_PRODUCTION_ENABLED': 'True'},
            {'CYBERTEST_PHASED_SCANS_ENABLED': '1'},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                production_entry_settings(self.configured(**changes))

    def test_only_exact_origin_and_reserved_prefix_are_accepted(self):
        for origin in ['https://chat-staging.mlai.au', 'http://chat.mlai.au',
                       'https://api.mlai.au', 'https://chat.mlai.au.evil.example',
                       'https://user@chat.mlai.au', 'https://chat.mlai.au:444',
                       'https://chat.mlai.au/', 'https://chat.mlai.au?x=1',
                       'https://chat.mlai.au#x', 'https://chat.mlai.au\n']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                production_entry_settings(self.configured(CYBERTEST_APP_ORIGIN=origin))
        for path in ['', '/', '/cybertest/', '/cybertest/..', '/other']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                production_entry_settings(self.configured(CYBERTEST_BASE_PATH=path))

    def test_company_allowlist_is_explicit_and_bounded(self):
        for value in ['', '*', 'not-a-uuid', ','.join(['10000000-0000-4000-8000-000000000001'] * 101)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                production_entry_settings(self.configured(CYBERTEST_PRODUCTION_COMPANY_IDS=value))

    def test_bad_or_reused_service_keys_are_rejected_without_echo(self):
        key = self.configured()['CYBERTEST_BILLING_API_KEY']
        for changes in [{'CYBERTEST_BILLING_API_KEY': ''}, {'SECRET_KEY': key},
                        {'COMMUNITY_CHAT_ADAPTER_TOKEN': key},
                        {'CYBERTEST_BILLING_API_KEY': key + '\n'}]:
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError) as caught:
                production_entry_settings(self.configured(**changes))
            self.assertNotIn(key, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
