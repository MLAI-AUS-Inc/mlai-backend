"""A stale token refresh is bounded and never becomes a guessed revocation."""
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from integrations.services.github_connections import read_bound_repository


class ConnectionRefreshTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(github_repo='example/site', installation_id='45', repository_id=123,
                                          organization=object())
        self.url = 'https://api.github.com/repos/example/site/commits/main'

    def read(self, statuses, *, cohort=True, flag=True):
        responses = [SimpleNamespace(status_code=status) for status in statuses]
        with override_settings(ARTICLES_CONNECTION_REFUSAL_CODES_ENABLED=flag), \
             patch('content_factory.website_connections.require_unlocked_remote_call') as unlocked, \
             patch('content_factory.p2_foundation.p2_applies', return_value=cohort), \
             patch('integrations.services.github_app.create_installation_access_token',
                   side_effect=[SimpleNamespace(token='cached'), SimpleNamespace(token='fresh')]) as mint, \
             patch('integrations.http_client.get', side_effect=responses) as get:
            result = read_bound_repository(self.connection, self.url)
            if cohort and flag:
                unlocked.assert_called_once()
            else:
                unlocked.assert_not_called()
        return result, mint, get

    def test_rotated_token_minted_once_for_same_repository(self):
        result, mint, get = self.read([401, 200])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(mint.call_count, 2)
        self.assertTrue(mint.call_args.kwargs['use_cache'] is False)
        self.assertEqual(mint.call_args.kwargs['repository_id'], 123)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer fresh')

    def test_refresh_is_bounded(self):
        result, mint, get = self.read([401, 401])
        self.assertEqual(result.status_code, 401)
        self.assertEqual(get.call_count, 2)

    def test_other_statuses_are_not_relabelled_or_retried(self):
        for status in [200, 403, 404, 410, 429, 500]:
            with self.subTest(status=status):
                result, mint, get = self.read([status])
                self.assertEqual(result.status_code, status)
                self.assertEqual(mint.call_count, 1)
                self.assertEqual(get.call_count, 1)

    def test_legacy_and_flag_off_keep_original_single_request(self):
        for options in [dict(cohort=False), dict(flag=False)]:
            with self.subTest(options=options):
                result, mint, get = self.read([401], **options)
                self.assertEqual(mint.call_count, 1)
                self.assertEqual(get.call_count, 1)

    def test_foreign_repository_url_never_mints_a_token(self):
        with patch('content_factory.website_connections.require_unlocked_remote_call'), \
             patch('integrations.services.github_app.create_installation_access_token') as mint:
            with self.assertRaises(ValueError):
                read_bound_repository(self.connection, 'https://api.github.com/repos/example/site-other')
            mint.assert_not_called()
