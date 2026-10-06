"""Workflow capability issuance without widening ordinary repository tokens."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from content_factory.tests_website_connections import WebsiteDatabaseFixture, SHA
from content_factory.website_models import WebsiteConnectionOperation
from content_factory.website_tokens import mint_website_token
from integrations.services import github_app
from workflow_runs.models import ContentFactoryRun


REPOSITORY_PERMISSIONS = {'contents': 'write', 'pull_requests': 'write'}
WORKFLOW_PERMISSIONS = {**REPOSITORY_PERMISSIONS, 'workflows': 'write'}


def response(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class WorkflowPermissionIssuerTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.jwt = patch.object(github_app, '_github_app_jwt', return_value='synthetic-jwt').start()
        self.get = patch.object(github_app.http_requests, 'get', return_value=response(200, {'permissions': WORKFLOW_PERMISSIONS})).start()
        self.post = patch.object(github_app.http_requests, 'post', return_value=self.credential(WORKFLOW_PERMISSIONS)).start()
        self.delete = patch.object(github_app.http_requests, 'delete', return_value=response(204, {})).start()
        self.addCleanup(patch.stopall)

    def credential(self, permissions):
        return response(201, {'token': 'synthetic-token', 'permissions': permissions,
                             'expires_at': (timezone.now() + timedelta(hours=1)).isoformat()})

    def mint(self, **kwargs):
        return github_app.create_installation_access_token(installation_id='45', repository='example/site',
            repository_id=kwargs.pop('repository_id', 123), permission_mode=kwargs.pop('permission_mode', 'write'), **kwargs)

    def test_repository_and_workflow_profiles_never_share_cached_credentials(self):
        self.post.side_effect = [self.credential(REPOSITORY_PERMISSIONS), self.credential(WORKFLOW_PERMISSIONS), self.credential(WORKFLOW_PERMISSIONS)]
        normal = self.mint()
        workflow = self.mint(permission_profile='workflow_files')
        self.assertNotIn('workflows', normal.permissions)
        self.assertEqual(workflow.permissions['workflows'], 'write')
        self.assertEqual(workflow.as_content_factory_payload()['permission_profile'], 'workflow_files')
        self.mint()
        self.mint(permission_profile='workflow_files')
        self.assertEqual(self.post.call_count, 2)
        self.mint(permission_profile='workflow_files', repository_id=456)
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.post.call_args_list[0].kwargs['json'], {'repository_ids': [123], 'permissions': REPOSITORY_PERMISSIONS})
        self.assertEqual(self.post.call_args_list[1].kwargs['json'], {'repository_ids': [123], 'permissions': WORKFLOW_PERMISSIONS})

    def test_missing_app_or_installation_grant_never_mints(self):
        for absent in (0, 1):
            grants = [response(200, {'permissions': WORKFLOW_PERMISSIONS}) for _ in range(2)]
            grants[absent] = response(200, {'permissions': REPOSITORY_PERMISSIONS})
            with self.subTest(absent=absent):
                self.get.side_effect = grants
                with self.assertRaises(github_app.GitHubWorkflowPermissionRequired):
                    self.mint(permission_profile='workflow_files')
        self.post.assert_not_called()
        self.delete.assert_not_called()

    def test_permission_preflight_reads_only_and_uses_actual_installation_grant(self):
        permissions = github_app.require_installation_workflow_permissions('45')
        self.assertEqual(permissions, WORKFLOW_PERMISSIONS)
        self.assertEqual([call.args[0] for call in self.get.call_args_list],
                         ['https://api.github.com/app', 'https://api.github.com/app/installations/45'])
        self.post.assert_not_called()
        self.delete.assert_not_called()

    def test_suspended_installation_cannot_use_cached_workflow_token(self):
        self.mint(permission_profile='workflow_files')
        self.post.reset_mock()
        self.get.side_effect = [response(200, {'permissions': WORKFLOW_PERMISSIONS}),
                                response(200, {'permissions': WORKFLOW_PERMISSIONS, 'suspended_at': 'synthetic'})]
        with self.assertRaises(github_app.GitHubAppTokenError):
            self.mint(permission_profile='workflow_files')
        self.post.assert_not_called()

    def test_unknown_profile_read_mode_or_mutable_repository_denied_before_provider(self):
        for kwargs in ({'permission_profile': 'all'}, {'permission_profile': 'workflow_files', 'permission_mode': 'read'},
                       {'permission_profile': 'workflow_files', 'repository_id': None},
                       {'permission_profile': 'workflow_files', 'repository_id': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(github_app.GitHubAppTokenError):
                self.mint(**kwargs)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_transient_or_malformed_grant_evidence_is_not_permission_approval(self):
        for reply in (response(429, {}), response(503, {}), response(200, [])):
            with self.subTest(status=reply.status_code):
                self.get.return_value = reply
                with self.assertRaises(github_app.GitHubPermissionLookupUnavailable):
                    self.mint(permission_profile='workflow_files')
        self.post.assert_not_called()

    def test_narrowed_mint_response_is_revoked_and_never_cached(self):
        self.post.return_value = self.credential(REPOSITORY_PERMISSIONS)
        with self.assertRaises(github_app.GitHubWorkflowPermissionRequired):
            self.mint(permission_profile='workflow_files')
        self.delete.assert_called_once()
        self.assertIsNone(cache.get(github_app._cache_key(installation_id='45', repository='example/site',
            permission_mode='write', permission_profile='workflow_files') + ':repository-id:123'))

    def test_grant_change_during_mint_has_sanitized_permission_error(self):
        self.post.return_value = response(403, {'message': 'synthetic-private-provider-body'})
        self.get.side_effect = [response(200, {'permissions': WORKFLOW_PERMISSIONS}),
                                response(200, {'permissions': WORKFLOW_PERMISSIONS}),
                                response(200, {'permissions': REPOSITORY_PERMISSIONS})]
        with self.assertRaises(github_app.GitHubWorkflowPermissionRequired) as caught:
            self.mint(permission_profile='workflow_files')
        self.assertNotIn('synthetic-private-provider-body', str(caught.exception))

    def test_repository_write_grants_are_required_even_when_workflows_is_approved(self):
        for location in (0, 1):
            for missing in ('contents', 'pull_requests'):
                for granted in (None, 'read'):
                    permissions = dict(WORKFLOW_PERMISSIONS)
                    permissions[missing] = granted
                    self.get.side_effect = [response(200, {'permissions': permissions if index == location else WORKFLOW_PERMISSIONS})
                                            for index in range(2)]
                    with self.subTest(location=location, missing=missing, granted=granted):
                        with self.assertRaises(github_app.GitHubAppTokenError) as caught:
                            self.mint(permission_profile='workflow_files')
                        self.assertNotIsInstance(caught.exception, github_app.GitHubWorkflowPermissionRequired)
        self.post.assert_not_called()

    def test_credential_and_repository_failures_are_not_misreported_as_workflow_approval(self):
        for status in (401, 403, 404, 422):
            for phase in ('grant', 'mint'):
                with self.subTest(status=status, phase=phase):
                    self.get.return_value = response(status if phase == 'grant' else 200,
                        {'permissions': WORKFLOW_PERMISSIONS, 'message': 'private-provider-body'})
                    self.post.return_value = response(status, {'message': 'private-provider-body'})
                    with self.assertRaises(github_app.GitHubAppTokenError) as caught:
                        self.mint(permission_profile='workflow_files')
                    self.assertNotIsInstance(caught.exception, github_app.GitHubWorkflowPermissionRequired)
                    self.assertNotIn('private-provider-body', str(caught.exception))

    def test_rate_limited_403_is_transient_and_not_a_missing_grant(self):
        for phase in ('grant', 'mint'):
            self.get.return_value = response(200, {'permissions': WORKFLOW_PERMISSIONS})
            limited = response(403, {})
            limited.headers = {'X-RateLimit-Remaining': '0'}
            if phase == 'grant':
                self.get.return_value = limited
            else:
                self.post.return_value = limited
            with self.subTest(phase=phase), self.assertRaises(github_app.GitHubPermissionLookupUnavailable):
                self.mint(permission_profile='workflow_files')

    def test_cached_repository_profile_cannot_return_elevated_or_forged_metadata(self):
        key = github_app._cache_key(installation_id='45', repository='example/site', permission_mode='write') + ':repository-id:123'
        base = {'github_token': 'old-token', 'github_permissions': REPOSITORY_PERMISSIONS,
                'permission_profile': 'repository', 'installation_id': '45', 'github_repo': 'example/site'}
        for changes in ({'permission_profile': 'workflow_files'}, {'permission_profile': []},
                        {'github_permissions': WORKFLOW_PERMISSIONS}, {'github_permissions': {}},
                        {'github_permissions': {'contents': 'write'}}, {'installation_id': '99'},
                        {'github_repo': 'other/site'}, {'token_source': 'oauth'},
                        {'github_permissions': {**REPOSITORY_PERMISSIONS, 'administration': 'write'}}):
            with self.subTest(changes=changes):
                cache.set(key, {**base, **changes})
                self.post.return_value = self.credential(REPOSITORY_PERMISSIONS)
                self.assertEqual(self.mint().token, 'synthetic-token')
        self.get.assert_not_called()
        self.assertEqual(self.post.call_count, 9)

    def test_malformed_workflow_cache_or_narrowed_repository_grant_never_escapes(self):
        self.mint(permission_profile='workflow_files')
        key = github_app._cache_key(installation_id='45', repository='example/site', permission_mode='write',
                                   permission_profile='workflow_files') + ':repository-id:123'
        cached = cache.get(key)
        cache.set(key, {**cached, 'permission_profile': 'repository'})
        self.mint(permission_profile='workflow_files')
        self.assertEqual(self.post.call_count, 2)
        for missing in ('contents', 'pull_requests'):
            permissions = dict(WORKFLOW_PERMISSIONS)
            permissions[missing] = 'read'
            self.post.return_value = self.credential(permissions)
            with self.subTest(missing=missing), self.assertRaises(github_app.GitHubAppTokenError) as caught:
                self.mint(permission_profile='workflow_files', use_cache=False)
            self.assertNotIsInstance(caught.exception, github_app.GitHubWorkflowPermissionRequired)
        self.assertEqual(self.delete.call_count, 2)


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key',
                   WEBSITE_CONNECTION_WRITE_MODE='enabled',
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class WebsiteWorkflowPermissionTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.run_id = str(uuid.uuid4())
        self.operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action='workflow', state='running', idempotency_key=str(uuid.uuid4()),
            payload={'workflow': 'article_system_setup', 'attempt': 1, 'run_id': self.run_id})
        self.data = {**self.binding, 'operation_id': str(self.operation.pk), 'operation_attempt': 1,
                     'deletion_epoch': 0, 'run_id': self.run_id, 'expected_source_sha': SHA,
                     'permission_profile': 'workflow_files', 'permission_mode': 'write', 'action': 'setup'}
        self.run = ContentFactoryRun.objects.create(run_id=self.run_id, organization=self.org, workflow='article_system_setup',
            domain=self.org.domain, github_repo=self.website.github_repo, status='running', run_request=self.data)
        self.jwt = patch.object(github_app, '_github_app_jwt', return_value='synthetic-jwt').start()
        self.get = patch.object(github_app.http_requests, 'get', return_value=response(200, {'permissions': WORKFLOW_PERMISSIONS})).start()
        self.post = patch.object(github_app.http_requests, 'post', return_value=response(201,
            {'token': 'synthetic-token', 'permissions': WORKFLOW_PERMISSIONS})).start()
        self.delete = patch.object(github_app.http_requests, 'delete', return_value=response(204, {})).start()
        self.addCleanup(patch.stopall)

    def request(self, **changes):
        from content_factory.service_views import ContentFactoryTokenView
        return ContentFactoryTokenView.as_view()(APIRequestFactory().get('/token', {**self.data, **changes},
            HTTP_X_API_KEY='synthetic-test-key'))

    def test_real_guarded_profile_wire_echoes_actual_permissions(self):
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['permission_profile'], 'workflow_files')
        self.assertEqual(result.data['github_permissions'], WORKFLOW_PERMISSIONS)
        self.assertEqual(result.data['token_source'], 'github_app_installation')
        self.assertEqual(self.post.call_args.kwargs['json'], {'repository_ids': [123], 'permissions': WORKFLOW_PERMISSIONS})

    def test_preflight_returns_no_credential_and_creates_no_token_reference(self):
        result = self.request(preflight='1')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['permissions_source'], 'installation_grant')
        self.assertEqual(result.data['permission_profile'], 'workflow_files')
        self.assertEqual(result.data['github_permissions']['workflows'], 'write')
        self.assertNotIn('github_token', result.data)
        self.assertNotIn('credential_reference', result.data)
        self.post.assert_not_called()
        self.assertIsNone(cache.get(f'website-token-refs:{self.website.pk}:{self.website.generation}'))

    def test_explicit_target_echo_is_validated_for_preflight_and_token(self):
        from content_factory.website_models import WebsiteConnectionTarget
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=self.website.generation,
            target_key='stories', adapter='static', source_sha=SHA)
        for preflight in ('0', '1'):
            with self.subTest(preflight=preflight):
                result = self.request(connection_target_id='stories', preflight=preflight)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.data['connection_target_id'], 'stories')
                self.assertEqual(result.data['website_connection_id'], str(self.website.pk))
                self.assertEqual(result.data['connection_generation'], self.website.generation)
                self.assertEqual(result.data['repository_id'], self.website.repository_id)
        self.get.reset_mock()
        self.post.reset_mock()
        self.assertEqual(self.request(connection_target_id='other').status_code, 409)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_missing_grant_is_actionable_nonretryable_409_without_mint(self):
        self.get.return_value = response(200, {'permissions': REPOSITORY_PERMISSIONS})
        result = self.request(preflight='1')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['code'], 'github_workflow_permission_required')
        self.assertFalse(result.data['retryable'])
        self.assertEqual(result.data['required_permission'], 'workflows:write')
        self.assertNotIn('github_token', result.data)
        self.post.assert_not_called()

    def test_unknown_read_publish_merge_and_legacy_operation_profiles_never_call_provider(self):
        for changes in ({'permission_profile': 'all'}, {'permission_mode': 'read'}, {'action': 'publish'},
                        {'action': 'merge'}, {'action': 'read'}, {'operation_id': ''}, {'operation_attempt': ''},
                        {'deletion_epoch': ''}, {'operation_id': str(uuid.uuid4())}, {'run_id': str(uuid.uuid4())}):
            with self.subTest(changes=changes):
                result = self.request(**changes)
                self.assertIn(result.status_code, (400, 409))
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_unrelated_workflow_cannot_borrow_setup_profile(self):
        self.operation.payload = {**self.operation.payload, 'workflow': 'article_generation'}
        self.operation.save(update_fields=['payload'])
        self.assertEqual(self.request().status_code, 409)
        self.get.assert_not_called()

    def test_cancel_during_read_fences_preflight_without_mutation(self):
        def cancel(*args, **kwargs):
            self.operation.state = 'cancelled'
            self.operation.save(update_fields=['state'])
            return response(200, {'permissions': WORKFLOW_PERMISSIONS})
        self.get.side_effect = cancel
        self.assertEqual(self.request(preflight='1').status_code, 409)
        self.post.assert_not_called()
        self.delete.assert_not_called()

    def test_rebind_during_mint_revokes_credential_before_delivery(self):
        def rebind(*args, **kwargs):
            self.website.generation += 1
            self.website.save(update_fields=['generation'])
            return response(201, {'token': 'synthetic-token', 'permissions': WORKFLOW_PERMISSIONS})
        self.post.side_effect = rebind
        result = self.request()
        self.assertEqual(result.status_code, 409)
        self.assertNotIn('github_token', result.data)
        self.delete.assert_called_once()

    def test_transient_grant_read_is_retryable_503_not_false_permission_denial(self):
        self.get.return_value = response(503, {})
        result = self.request(preflight='1')
        self.assertEqual(result.status_code, 503)
        self.assertTrue(result.data['retryable'])
        self.post.assert_not_called()

    def test_ordinary_profile_never_requests_extra_permission_or_preflight(self):
        self.post.return_value = response(201, {'token': 'synthetic-token', 'permissions': REPOSITORY_PERMISSIONS})
        result = self.request(permission_profile='repository')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('workflows', result.data['github_permissions'])
        self.get.assert_not_called()
        self.assertEqual(self.post.call_args.kwargs['json']['permissions'], REPOSITORY_PERMISSIONS)

    def test_workflow_grant_reads_run_outside_website_authority_lock(self):
        from content_factory.website_connections import require_unlocked_remote_call
        def verify_unlocked(*args, **kwargs):
            require_unlocked_remote_call()
            return response(200, {'permissions': WORKFLOW_PERMISSIONS})
        self.get.side_effect = verify_unlocked
        self.assertEqual(self.request(preflight='1').status_code, 200)

    def test_provider_access_failures_keep_the_repository_error_not_workflow_approval(self):
        for status in (401, 403, 404, 422):
            with self.subTest(status=status):
                self.post.return_value = response(status, {'message': 'private-provider-body'})
                result = self.request()
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.data['code'], 'github_repository_unavailable')
                self.assertFalse(result.data['retryable'])
                self.assertNotIn('private-provider-body', str(result.data))

    def test_missing_repository_write_grant_is_not_a_workflow_approval_request(self):
        self.get.return_value = response(200, {'permissions': {'contents': 'read', 'pull_requests': 'write', 'workflows': 'write'}})
        result = self.request(preflight='1')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['code'], 'github_repository_unavailable')
        self.post.assert_not_called()

    def test_approved_cleanup_requires_the_exact_reviewed_workflow_inverse(self):
        workflow_path = '.github/workflows/mlai-articles-verification.yml'
        self.operation.action, self.operation.state = 'cleanup', 'applying'
        approved = {'source_sha': SHA, 'proposal_digest': 'reviewed-digest',
                    'deletions': [workflow_path], 'approved_by_user_id': '17'}
        self.operation.payload = {**self.operation.payload, 'approved_cleanup': approved}
        self.operation.save(update_fields=['action', 'state', 'payload'])
        changes = {'action': 'cleanup', 'source_sha': SHA, 'proposal_digest': 'reviewed-digest', 'preflight': '1'}
        self.assertEqual(self.request(**changes).status_code, 200)
        self.get.reset_mock()
        for denied in ({'source_sha': 'b' * 40}, {'expected_source_sha': 'b' * 40}, {'proposal_digest': 'different'},
                       {'source_sha': '', 'expected_source_sha': ''}, {'proposal_digest': ''}):
            with self.subTest(denied=denied):
                self.assertEqual(self.request(**{**changes, **denied}).status_code, 409)
        for invalid in ({}, {**approved, 'deletions': ['integration/support.md']},
                        {**approved, 'approved_by_user_id': ''}, []):
            with self.subTest(invalid=invalid):
                self.operation.payload = {**self.operation.payload, 'approved_cleanup': invalid}
                self.operation.save(update_fields=['payload'])
                self.assertEqual(self.request(**changes).status_code, 409)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_restoration_requires_original_setup_and_exact_owner_approved_plan(self):
        self.operation.action, self.operation.state = 'cleanup', 'applying'
        approved = {'expected_base_sha': SHA, 'plan_digest': 'reviewed-plan', 'approved_by_user_id': '17',
                    'workflow_paths': ['.github/workflows/mlai-articles-verification.yml']}
        self.operation.payload = {**self.operation.payload, 'setup_run_ids': [self.run_id], 'approved_restoration': approved}
        self.operation.save(update_fields=['action', 'state', 'payload'])
        changes = {'action': 'restoration', 'setup_run_id': self.run_id, 'expected_base_sha': SHA,
                   'plan_digest': 'reviewed-plan', 'preflight': '1'}
        self.assertEqual(self.request(**changes).status_code, 200)
        self.get.reset_mock()
        for denied in ({'setup_run_id': str(uuid.uuid4())}, {'expected_base_sha': 'b' * 40}, {'plan_digest': 'different'}):
            with self.subTest(denied=denied):
                self.assertEqual(self.request(**{**changes, **denied}).status_code, 409)
        for invalid in ({}, {**approved, 'approved_by_user_id': ''}, {**approved, 'workflow_paths': []},
                        {**approved, 'workflow_paths': ['app/articles/page.tsx']}, ['malformed']):
            with self.subTest(invalid=invalid):
                self.operation.payload = {**self.operation.payload, 'approved_restoration': invalid}
                self.operation.save(update_fields=['payload'])
                self.assertEqual(self.request(**changes).status_code, 409)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_owner_restoration_approval_records_only_reviewed_workflow_inverse_paths(self):
        from content_factory.website_restoration import approve_worker_restoration
        workflow_path = '.github/workflows/mlai-articles-verification.yml'
        for changes, expected in (([{'path': workflow_path, 'operation': 'restore'},
                                    {'path': 'app/articles/page.tsx', 'operation': 'delete'}], [workflow_path]),
                                  ([{'path': workflow_path, 'operation': 'create'}], []),
                                  ([{'path': 'app/articles/page.tsx', 'operation': 'delete'}], [])):
            with self.subTest(changes=changes):
                self.operation.action, self.operation.state = 'cleanup', 'review_required'
                self.operation.next_attempt_at = None
                self.operation.payload = {**self.operation.payload, 'setup_run_ids': [self.run_id]}
                self.operation.receipt = {'setup_run_id': self.run_id, 'source_sha': SHA,
                    'proposal_digest': 'reviewed-plan', 'changes': changes}
                self.operation.save()
                with patch('content_factory.website_connections.verify_repository_access', return_value={'repository_id': 123}), \
                     patch('content_factory.website_restoration.worker_restoration', return_value={'status': 'no_op'}):
                    result = approve_worker_restoration(self.config, user=SimpleNamespace(pk=17),
                        data={**self.binding, 'operation_id': str(self.operation.pk), 'source_sha': SHA, 'proposal_digest': 'reviewed-plan'})
                self.assertEqual(result.payload['approved_restoration'], {'plan_digest': 'reviewed-plan',
                    'expected_base_sha': SHA, 'approved_by_user_id': '17', 'workflow_paths': expected})
        self.post.assert_not_called()

    def test_cancelled_and_stale_operation_attempts_never_reach_grant_lookup(self):
        self.assertEqual(self.request(operation_attempt=2).status_code, 409)
        self.operation.state = 'cancelled'
        self.operation.save(update_fields=['state'])
        self.assertEqual(self.request().status_code, 409)
        self.get.assert_not_called()
        self.post.assert_not_called()
