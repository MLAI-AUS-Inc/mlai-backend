"""Private CI proof credentials stay separate from model/source readers."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory

from content_factory.tests_website_connections import WebsiteDatabaseFixture, SHA
from content_factory.website_models import WebsiteConnectionOperation
from integrations.services import github_app
from integrations.tests_github_workflow_permissions import response
from workflow_runs.models import ContentFactoryRun

CI_PERMISSIONS = {'contents': 'read', 'pull_requests': 'read', 'checks': 'read', 'statuses': 'read'}
GRANTS = {**CI_PERMISSIONS, 'contents': 'write', 'pull_requests': 'write'}


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class CIEvidenceIssuerTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        patch.object(github_app, '_github_app_jwt', return_value='synthetic-jwt').start()
        self.get = patch.object(github_app.http_requests, 'get', return_value=response(200, {'permissions': GRANTS})).start()
        self.post = patch.object(github_app.http_requests, 'post', return_value=self.credential()).start()
        self.delete = patch.object(github_app.http_requests, 'delete', return_value=response(204, {})).start()
        self.addCleanup(patch.stopall)

    def credential(self, permissions=None):
        return response(201, {'token': 'synthetic-token', 'permissions': CI_PERMISSIONS if permissions is None else permissions})

    def mint(self, **changes):
        return github_app.create_installation_access_token(**{'installation_id': '45', 'repository': 'example/private',
            'repository_id': 123, 'permission_mode': 'read', 'permission_profile': 'ci_evidence', **changes})

    def test_ci_profile_reads_existing_write_grants_but_requests_only_exact_read_permissions(self):
        token = self.mint()
        self.assertEqual(token.permissions, CI_PERMISSIONS)
        self.assertEqual(token.permission_profile, 'ci_evidence')
        self.assertEqual(self.post.call_args.kwargs['json'], {'repository_ids': [123], 'permissions': CI_PERMISSIONS})
        self.assertNotIn('actions', token.permissions)
        self.assertNotIn('workflows', token.permissions)

    def test_model_reader_and_ci_evidence_caches_cannot_cross_profiles_or_repositories(self):
        normal_permissions = {'contents': 'read', 'pull_requests': 'read'}
        self.post.side_effect = [self.credential(normal_permissions), self.credential(), self.credential()]
        self.mint(permission_profile='repository')
        self.get.assert_not_called()
        self.mint()
        self.mint(permission_profile='repository')
        self.mint()
        self.assertEqual(self.post.call_count, 2)
        self.mint(repository_id=456)
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.post.call_args_list[0].kwargs['json']['permissions'], normal_permissions)

    def test_write_or_mutable_repository_ci_credentials_are_denied_before_provider(self):
        for changes in ({'permission_mode': 'write'}, {'permission_mode': ''}, {'repository_id': None}, {'repository_id': True}):
            with self.subTest(changes=changes), self.assertRaises(github_app.GitHubAppTokenError):
                self.mint(**changes)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_missing_app_or_installation_ci_grant_never_mints(self):
        for location in (0, 1):
            for key in ('checks', 'statuses'):
                missing = {**GRANTS, key: None}
                self.get.side_effect = [response(200, {'permissions': missing if index == location else GRANTS}) for index in range(2)]
                with self.subTest(location=location, key=key), self.assertRaises(github_app.GitHubCIEvidencePermissionRequired):
                    self.mint()
        self.post.assert_not_called()

    def test_cached_ci_token_does_not_borrow_revoked_or_suspended_grants(self):
        self.mint()
        self.post.reset_mock()
        self.get.return_value = response(200, {'permissions': {**GRANTS, 'checks': None}})
        with self.assertRaises(github_app.GitHubCIEvidencePermissionRequired):
            self.mint()
        self.get.return_value = response(200, {'permissions': GRANTS, 'suspended_at': 'synthetic'})
        with self.assertRaises(github_app.GitHubAppTokenError):
            self.mint()
        self.post.assert_not_called()

    def test_returned_write_actions_workflows_or_missing_ci_scope_is_revoked(self):
        for permissions in ({**CI_PERMISSIONS, 'checks': 'write'}, {**CI_PERMISSIONS, 'contents': 'write'},
                            {**CI_PERMISSIONS, 'actions': 'read'}, {**CI_PERMISSIONS, 'workflows': 'write'},
                            {**CI_PERMISSIONS, 'statuses': None}):
            with self.subTest(permissions=permissions), self.assertRaises(github_app.GitHubAppTokenError):
                self.post.return_value = self.credential(permissions)
                self.mint(use_cache=False)
        self.assertEqual(self.delete.call_count, 5)
        key = github_app._cache_key(installation_id='45', repository='example/private', permission_mode='read',
                                   permission_profile='ci_evidence') + ':repository-id:123'
        self.assertIsNone(cache.get(key))

    def test_access_and_transient_failures_are_not_missing_ci_grants(self):
        for status in (401, 403, 404, 422, 429, 503):
            self.post.return_value = response(status, {'message': 'private-provider-body'})
            with self.subTest(status=status), self.assertRaises(github_app.GitHubAppTokenError) as caught:
                self.mint(use_cache=False)
            self.assertNotIsInstance(caught.exception, github_app.GitHubCIEvidencePermissionRequired)
            self.assertNotIn('private-provider-body', str(caught.exception))
            if status in (429, 503):
                self.assertIsInstance(caught.exception, github_app.GitHubPermissionLookupUnavailable)

    def test_private_native_proof_uses_real_scoped_issuer_and_revokes_after_read(self):
        from content_factory.website_verification import read_ci_proof, CHECK_NAME
        from integrations.http_client import HTTPError
        proof = json.loads((Path(__file__).parents[1] / 'content_factory/fixtures/native_evidence_v2.json').read_text())
        check = {'name': CHECK_NAME, 'head_sha': proof['source_sha'], 'status': 'completed', 'conclusion': 'success',
                 'app': {'slug': 'github-actions'}, 'output': {'summary': 'MLAI_ARTICLES_ATTESTATION:' + base64.b64encode(json.dumps(proof).encode()).decode()}}
        def private_provider(url, **kwargs):
            if url.startswith('https://api.github.com/app'):
                return response(200, {'permissions': GRANTS})
            granted = self.post.call_args.kwargs['json']['permissions']
            if granted.get('checks') != 'read':
                raise HTTPError('Synthetic private endpoint denies missing Checks read')
            self.assertEqual(url, 'https://api.github.com/repos/' + proof['github_repo'] + '/commits/' + proof['source_sha'] + '/check-runs?per_page=100')
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'check_runs': [check]})
        self.get.side_effect = private_provider
        website = SimpleNamespace(installation_id='45', github_repo=proof['github_repo'], repository_id=proof['repository_id'])
        self.assertEqual(read_ci_proof(website, proof), proof)
        self.assertEqual(self.post.call_args.kwargs['json']['permissions'], CI_PERMISSIONS)
        self.delete.assert_called_once()

    def test_direct_provider_consumers_return_typed_safe_errors_without_minting(self):
        from content_factory.website_tokens import mint_ci_evidence_token
        from content_factory.website_contract import WebsiteAuthorityError
        for reply, code, status, retryable in (
                (response(200, {'permissions': {**GRANTS, 'checks': None}}), 'github_ci_evidence_permission_required', 409, False),
                (response(503, {}), 'github_temporarily_unavailable', 503, True),
                (response(401, {'message': 'private-provider-body'}), 'github_repository_unavailable', 409, False)):
            with self.subTest(code=code), self.assertRaises(WebsiteAuthorityError) as caught:
                self.get.return_value = reply
                mint_ci_evidence_token(installation_id='45', repository='example/private', repository_id=123)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn('private-provider-body', str(caught.exception))
        self.post.assert_not_called()
        self.delete.assert_not_called()


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key',
                   WEBSITE_CONNECTION_WRITE_MODE='enabled',
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class WebsiteCIEvidenceTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.run_id = str(uuid.uuid4())
        self.operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action='workflow', state='completed', idempotency_key=str(uuid.uuid4()),
            payload={'workflow': 'article_system_setup', 'run_id': self.run_id, 'attempt': 1})
        self.data = {**self.binding, 'operation_id': str(self.operation.pk), 'operation_attempt': 1, 'deletion_epoch': 0,
            'run_id': self.run_id, 'expected_source_sha': SHA, 'permission_profile': 'ci_evidence', 'permission_mode': 'read', 'action': 'read'}
        ContentFactoryRun.objects.create(run_id=self.run_id, organization=self.org, workflow='article_system_setup',
            status='completed', domain=self.org.domain, github_repo=self.website.github_repo, run_request=self.data)
        patch.object(github_app, '_github_app_jwt', return_value='synthetic-jwt').start()
        self.get = patch.object(github_app.http_requests, 'get', return_value=response(200, {'permissions': GRANTS})).start()
        self.post = patch.object(github_app.http_requests, 'post', return_value=response(201, {'token': 'synthetic-token', 'permissions': CI_PERMISSIONS})).start()
        self.delete = patch.object(github_app.http_requests, 'delete', return_value=response(204, {})).start()
        self.addCleanup(patch.stopall)

    def request(self, **changes):
        from content_factory.service_views import ContentFactoryTokenView
        return ContentFactoryTokenView.as_view()(APIRequestFactory().get('/token', {**self.data, **changes}, HTTP_X_API_KEY='synthetic-test-key'))

    def test_completed_owned_setup_can_read_ci_without_elevating_model_tokens(self):
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['permission_profile'], 'ci_evidence')
        self.assertEqual(result.data['github_permissions'], CI_PERMISSIONS)
        self.assertEqual(result.data['repository_id'], 123)
        self.assertEqual(result.data['website_connection_id'], str(self.website.pk))

    def test_unknown_write_action_and_missing_original_operation_are_denied_before_provider(self):
        for changes in ({'permission_mode': 'write'}, {'action': 'setup'}, {'action': 'publish'}, {'action': 'restoration'},
                        {'operation_id': ''}, {'operation_attempt': ''}, {'deletion_epoch': ''},
                        {'run_id': str(uuid.uuid4())}, {'operation_attempt': 2}):
            with self.subTest(changes=changes):
                self.assertIn(self.request(**changes).status_code, (400, 409))
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_missing_existing_ci_grant_has_typed_nonretryable_response(self):
        self.get.return_value = response(200, {'permissions': {**GRANTS, 'checks': None}})
        result = self.request()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['code'], 'github_ci_evidence_permission_required')
        self.assertFalse(result.data['retryable'])
        self.assertEqual(result.data['required_permissions'], ['checks:read', 'statuses:read'])
        self.assertNotIn('github_token', result.data)
        self.post.assert_not_called()

    def test_cancel_or_rebind_during_ci_mint_revokes_without_delivering(self):
        for mutation in ('cancel', 'rebind'):
            with self.subTest(mutation=mutation):
                def change_authority(*args, **kwargs):
                    if mutation == 'cancel':
                        self.operation.state = 'cancelled'
                        self.operation.save(update_fields=['state'])
                    else:
                        self.website.generation += 1
                        self.website.save(update_fields=['generation'])
                    return response(201, {'token': 'synthetic-token', 'permissions': CI_PERMISSIONS})
                self.operation.state = 'completed'
                self.operation.save(update_fields=['state'])
                self.post.side_effect = change_authority
                result = self.request()
                self.assertEqual(result.status_code, 409)
                self.assertNotIn('github_token', result.data)
        self.assertEqual(self.delete.call_count, 2)

    def test_awaiting_cleanup_private_ci_uses_exact_read_profile_and_final_fence(self):
        from content_factory.website_cleanup_verification import verify_cleanup_deployment
        operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action='cleanup', state='awaiting_deployment', idempotency_key=str(uuid.uuid4()),
            receipt={'merge_sha': SHA, 'verification_routes': [{'path': '/articles', 'expected_status': 404}]})
        def provider(url, **kwargs):
            if url.startswith('https://api.github.com/app'):
                return response(200, {'permissions': GRANTS})
            self.assertEqual(self.post.call_args.kwargs['json']['permissions'], CI_PERMISSIONS)
            value = {'check_runs': [{'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
                                    'app': {'slug': 'github-actions'}}]} if '/check-runs' in url else {'sha': SHA}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        self.get.side_effect = provider
        with patch('content_factory.website_live_fetch.fetch_live_route', return_value=(b'gone', {})):
            result = verify_cleanup_deployment(self.config, data={**self.binding, 'operation_id': str(operation.pk)})
        self.assertEqual(result.state, 'completed')
        self.assertTrue(result.receipt['cleanup_complete'])
        self.delete.assert_called_once()

    def test_disconnected_cleanup_read_can_be_cancelled_without_certifying_completion(self):
        from content_factory.website_cleanup_verification import verify_cleanup_deployment
        from content_factory.website_contract import WebsiteAuthorityError
        self.website.state = 'disconnected'
        self.website.save(update_fields=['state'])
        operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action='cleanup', state='awaiting_deployment', idempotency_key=str(uuid.uuid4()),
            receipt={'merge_sha': SHA, 'verification_routes': [{'path': '/articles', 'expected_status': 404}]})
        def provider(url, **kwargs):
            if url.startswith('https://api.github.com/app'):
                return response(200, {'permissions': GRANTS})
            value = {'check_runs': [{'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
                                    'app': {'slug': 'github-actions'}}]} if '/check-runs' in url else {'sha': SHA}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        def cancelled_route(*args, **kwargs):
            operation.state = 'cancelled'
            operation.save(update_fields=['state', 'updated_at'])
            return b'gone', {}
        self.get.side_effect = provider
        with patch('content_factory.website_live_fetch.fetch_live_route', side_effect=cancelled_route), \
             self.assertRaises(WebsiteAuthorityError) as caught:
            verify_cleanup_deployment(self.config, data={**self.binding, 'operation_id': str(operation.pk)})
        self.assertEqual(caught.exception.code, 'cleanup_review_changed')
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'cancelled')
        self.assertNotEqual(operation.receipt.get('cleanup_complete'), True)
        self.assertEqual(self.post.call_args.kwargs['json']['permissions'], CI_PERMISSIONS)
        self.delete.assert_called_once()
