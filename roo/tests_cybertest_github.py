"""The scanner reuses the founder's existing GitHub connection (no database)."""
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.services.github_app import GitHubAppTokenError
from roo import cybertest_entry_views as views
from roo import cybertest_github as github
from roo.cybertest_entry import EntryError

COMPANY = '10000000-0000-4000-8000-000000000001'
WORKSPACE = '20000000-0000-4000-8000-000000000001'
IDENTITY = dict(user_id='7', company_id=COMPANY, workspace_id=WORKSPACE, company_name='Acme')
REPOS = [
    {'id': 11, 'fullName': 'acme/site', 'private': True, 'installationId': '501'},
    {'id': 12, 'fullName': 'Acme/API', 'private': False, 'installationId': '502'},
    {'id': None, 'fullName': 'acme/broken', 'installationId': '501'},
]


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class FounderGitHubConnectionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.user = SimpleNamespace(pk=7)
        self.enterContext(patch.object(github, 'founder', return_value=self.user))
        self.repos = self.enterContext(patch.object(github, 'list_user_repos', return_value=REPOS))

    def test_founder_without_a_connection_keeps_the_scanner_connect_button(self):
        with patch.object(github, '_installation', return_value=None):
            self.assertEqual(github.connection(IDENTITY), {'connected': False})
        self.repos.assert_not_called()

    def test_unknown_github_identity_is_not_connected(self):
        with patch.object(github, '_installation', return_value=SimpleNamespace(github_user_name='sam')), \
                patch.object(github, '_github_user_id', return_value=None):
            self.assertEqual(github.connection(IDENTITY), {'connected': False})

    def test_existing_connection_lists_repositories_without_installation_details(self):
        with patch.object(github, '_installation', return_value=SimpleNamespace(github_user_name=' sam ')), \
                patch.object(github, '_github_user_id', return_value='99'):
            result = github.connection(IDENTITY)
        self.assertEqual(result, {
            'connected': True, 'login': 'sam', 'github_user_id': '99',
            'repositories': [
                {'repository_id': '12', 'full_name': 'Acme/API', 'private': False},
                {'repository_id': '11', 'full_name': 'acme/site', 'private': True},
            ],
        })

    def test_read_token_is_read_only_and_scoped_to_one_listed_repository(self):
        token = SimpleNamespace(token='ghs_read', expires_at=datetime(2026, 11, 21, 1, 0, tzinfo=dt_timezone.utc))
        with patch.object(github.github_app, 'create_installation_access_token', return_value=token) as mint:
            result = github.read_token(IDENTITY, '11', 'ACME/site')
        self.assertEqual(result, {'token': 'ghs_read', 'expires_at': '2026-11-21T01:00:00+00:00'})
        mint.assert_called_once_with(installation_id='501', repository='acme/site', repository_id=11, permission_mode='read')

    def test_repository_outside_the_connection_or_mismatched_is_refused(self):
        with patch.object(github.github_app, 'create_installation_access_token') as mint:
            for repository_id, name in [('13', 'acme/other'), ('11', 'acme/API'), ('x', 'acme/site'), (11, 'acme/site')]:
                with self.assertRaises(EntryError):
                    github.read_token(IDENTITY, repository_id, name)
        mint.assert_not_called()

    def test_github_failure_is_a_retryable_error(self):
        with patch.object(github.github_app, 'create_installation_access_token', side_effect=GitHubAppTokenError('down')):
            with self.assertRaises(EntryError) as raised:
                github.read_token(IDENTITY, '11', 'acme/site')
        self.assertEqual(raised.exception.status, 503)

    def test_github_user_id_is_looked_up_once_and_cached(self):
        installation = SimpleNamespace(github_user_name='sam', installation_id='501')
        response = SimpleNamespace(status_code=200, json=lambda: {'id': 99})
        with patch.object(github.github_app, '_request_installation_access_token', return_value={'token': 't'}), \
                patch.object(github.requests, 'get', return_value=response) as get:
            self.assertEqual(github._github_user_id(installation), '99')
            self.assertEqual(github._github_user_id(installation), '99')
        get.assert_called_once()
        self.assertIsNone(github._github_user_id(SimpleNamespace(github_user_name='bad/login', installation_id='501')))


@override_settings(CYBERTEST_BILLING_API_KEY='k' * 32,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class GitHubViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()

    def post(self, data, key='k' * 32):
        return views.GitHubView.as_view()(self.factory.post('/github/', data, format='json', HTTP_X_CYBERTEST_KEY=key))

    def test_requires_the_service_key_before_checking_the_grant(self):
        with patch.object(views, 'authority') as authority:
            self.assertEqual(self.post({'grant': 'g' * 43}, key='wrong').status_code, 403)
        authority.assert_not_called()

    def test_lists_or_mints_for_the_signed_in_founder_only(self):
        with patch.object(views, 'authority') as authority, \
                patch.object(github, 'connection', return_value={'connected': False}) as connection, \
                patch.object(github, 'read_token', return_value={'token': 't', 'expires_at': None}) as read:
            authority.return_value.inspect.return_value = IDENTITY
            self.assertEqual(self.post({'grant': 'g' * 43}).data, {'connected': False})
            response = self.post({'grant': 'g' * 43, 'repository_id': '11', 'repository': 'acme/site'})
        self.assertEqual(response.data, {'token': 't', 'expires_at': None})
        connection.assert_called_once_with(IDENTITY)
        read.assert_called_once_with(IDENTITY, '11', 'acme/site')

    def test_unexpected_fields_are_refused(self):
        with patch.object(views, 'authority') as authority:
            self.assertEqual(self.post({'grant': 'g' * 43, 'repository_id': '11'}).status_code, 400)
            self.assertEqual(self.post({'grant': 'g' * 43, 'extra': 1}).status_code, 400)
        authority.assert_not_called()
