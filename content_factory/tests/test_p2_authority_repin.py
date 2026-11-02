"""Source re-pin preserves tenant, authorization, money and original run identity."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from content_factory import source_repin as repin
from content_factory.self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_named_legacy_org
from content_factory.website_contract import WebsiteAuthorityError
from content_factory.website_models import WebsiteConnection
from workflow_runs.models import ContentFactoryRun

A, B, C, D = (char * 40 for char in 'abcd')


@override_settings(**ALL_FLAGS_ON)
class SourceRepinTests(TransactionTestCase):
    """The real run CAS executes under the same local lock boundary as the guard."""

    def setUp(self):
        self.fixture = make_p2_org()
        self.org = self.fixture.organization
        self.connection = WebsiteConnection.objects.create(organization=self.org, github_repo='example/site',
            repository_id=123, installation_id='45', branch='main', site_url=self.org.domain)
        self.request = {'expected_source_sha': A, 'website_connection_id': str(self.connection.pk),
                        'connection_generation': self.connection.generation, 'repository_id': 123,
                        'operation_id': 'operation', 'operation_attempt': 1, 'article_text': 'Keep the written article'}
        self.run = ContentFactoryRun.objects.create(run_id='repin-test', organization=self.org,
            domain=self.org.domain, github_repo='example/site', workflow='article_system_setup', status='blocked',
            run_request=self.request, result={'preserved': True})
        self.payload = {**self.request, 'from_sha': A, 'to_sha': B,
                        'reason': 'source_changed_mid_run', 'repin_key': 'repin-test:1'}
        self.provider_calls = []
        @contextmanager
        def guarded(payload, *, action, require_current_head):
            # Provider proof must run before database locks, then local persistence is atomic.
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertTrue(require_current_head)
            self.provider_calls.append((payload, action))
            with transaction.atomic():
                yield self.connection
        self.guard = patch.object(repin, 'authority_guard', side_effect=guarded)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def apply(self, **values):
        return repin.repin_source(self.run.run_id, {**self.payload, **values})

    def test_success_and_replay_leave_timestamp_content_and_ledger_unchanged(self):
        from roo.models import Ledger
        rows, timestamp = Ledger.objects.count(), self.run.updated_at
        self.assertEqual(self.apply()['status'], 'repinned')
        self.run.refresh_from_db()
        request, result = self.run.run_request, self.run.result
        self.assertEqual(request, {**self.request, 'expected_source_sha': B})
        self.assertTrue(result['preserved'])
        self.assertEqual(self.run.updated_at, timestamp)
        self.assertEqual(self.apply()['status'], 'already_repinned')
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, result)
        self.assertEqual(Ledger.objects.count(), rows)

    def test_third_repin_refused_and_alert_deduped(self):
        self.apply()
        self.apply(from_sha=B, to_sha=C, repin_key='repin-test:2')
        for _ in range(2):
            with self.assertRaises(WebsiteAuthorityError) as error:
                self.apply(from_sha=C, to_sha=D, repin_key='repin-test:3')
            self.assertEqual(error.exception.authority_code, 'source_repin_limit_reached')
        self.fixture.config.refresh_from_db()
        alerts = self.fixture.config.pillar_strategy.get('ops_alerts', {})
        # The alert store is the shared dedupe authority, not a new outbox.
        self.assertEqual(sum(row.get('alert_reason') == 'source_change_repeated'
                             for row in alerts.values() if isinstance(row, dict)), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request['expected_source_sha'], C)
        self.assertEqual(self.run.result['re_pin_v1']['repins'], 2)

    def test_flag_off_never_calls_provider(self):
        with override_settings(ARTICLES_SOURCE_REPIN_ENABLED=False), self.assertRaises(WebsiteAuthorityError):
            self.apply()
        self.assertFalse(self.provider_calls)

    def test_legacy_never_calls_provider(self):
        legacy = make_named_legacy_org('la_vie').organization
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(organization=legacy)
        with self.assertRaises(WebsiteAuthorityError):
            self.apply()
        self.assertFalse(self.provider_calls)

    def test_terminal_and_settled_runs_refused(self):
        for status in ['completed', 'cancelled', 'denied']:
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(WebsiteAuthorityError):
                self.apply()
        for state in ['released', 'refunded']:
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status='blocked', result={'points_v1': {'state': state}})
            with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError):
                self.apply()
        self.assertFalse(self.provider_calls)

    def test_changed_source_refused_without_receipt(self):
        with self.assertRaises(WebsiteAuthorityError):
            self.apply(from_sha=C)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request['expected_source_sha'], A)
        self.assertNotIn('re_pin_v1', self.run.result)

    def test_scope_substitution_refused_before_provider(self):
        for field, value in [('repository_id', 456), ('connection_generation', 99), ('operation_attempt', 2),
                             ('operation_id', 'other'), ('website_connection_id', 'other')]:
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                self.apply(**{field: value})
        self.assertFalse(self.provider_calls)

    def test_wrong_org_returned_by_guard_refused(self):
        self.connection.organization_id += 100
        with self.assertRaises(WebsiteAuthorityError):
            self.apply()

    def test_provider_moved_head_refusal_propagates(self):
        with patch.object(repin, 'authority_guard', side_effect=WebsiteAuthorityError('website_source_changed', 'Changed.')):
            with self.assertRaises(WebsiteAuthorityError) as error:
                self.apply()
        self.assertEqual(error.exception.authority_code, 'website_source_changed')
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, self.request)

    def test_key_reuse_with_different_target_refused(self):
        self.apply()
        with self.assertRaises(WebsiteAuthorityError):
            self.apply(to_sha=C)

    def test_missing_run_is_not_created(self):
        with self.assertRaises(ContentFactoryRun.DoesNotExist):
            repin.repin_source('absent', {**self.payload, 'repin_key': 'absent:1'})

    def test_malformed_history_fails_closed(self):
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={'re_pin_v1': {'version': 1}})
        with self.assertRaises(WebsiteAuthorityError):
            self.apply()

    @override_settings(ROO_API_KEY='synthetic-repin-test-key', INTERNAL_API_KEY='synthetic-repin-test-key')
    def test_endpoint_requires_service_key(self):
        client = APIClient()
        response = client.post('/api/content-factory/runs/repin-test/repin', self.payload, format='json')
        self.assertIn(response.status_code, [401, 403])
        self.assertFalse(self.provider_calls)
        response = client.post('/api/content-factory/runs/repin-test/repin', self.payload,
                               format='json', HTTP_X_API_KEY='synthetic-repin-test-key')
        self.assertEqual(response.status_code, 200, response.data)

    def test_real_guard_verifies_head_without_observation_before_locks(self):
        self.guard.stop()
        self.fixture.config.website_connection = self.connection
        self.fixture.config.github_repo = self.connection.github_repo
        self.fixture.config.save(update_fields=['website_connection', 'github_repo'])
        request = {key: value for key, value in self.request.items() if key not in {'operation_id', 'operation_attempt'}}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(run_request=request)
        payload = {key: value for key, value in self.payload.items() if key not in {'operation_id', 'operation_attempt'}}
        calls = []
        def verify(connection, sha):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            calls.append(sha)
            return sha
        with patch('content_factory.website_connections.verify_repository_head', side_effect=verify), \
             patch('content_factory.website_connections.verify_repository_native_target'):
            self.assertEqual(repin.repin_source(self.run.run_id, payload)['status'], 'repinned')
        self.assertEqual(calls, [B])

    def test_reselect_disconnected_same_repository_gets_new_generation(self):
        from content_factory.website_connections import bind_website
        self.connection.state = 'disconnected'
        self.connection.save(update_fields=['state'])
        config = self.fixture.config
        config.website_connection = self.connection
        config.github_repo = self.connection.github_repo
        config.save(update_fields=['website_connection', 'github_repo'])
        metadata = dict(repository_id=123, github_repo='example/site', installation_id='45', branch='main')
        with patch('content_factory.website_connections.verify_repository_access', return_value=metadata):
            connection = bind_website(config, user=self.fixture.owner, repo='example/site')
        self.assertEqual(connection.pk, self.connection.pk)
        self.assertEqual(connection.generation, self.connection.generation + 1)
        self.assertEqual(connection.state, 'connected')

    def test_malformed_request_is_a_refusal(self):
        for change in [dict(reason=[]), dict(reason={}), dict(to_sha=False), dict(repin_key=1), dict(from_sha=None)]:
            with self.subTest(change=change), self.assertRaises(WebsiteAuthorityError):
                self.apply(**change)
        self.assertFalse(self.provider_calls)

    def test_concurrent_real_guard_never_repeats_source_transition(self):
        from django.test import skipUnlessDBFeature
        from django.db import connection, close_old_connections
        if not connection.features.has_select_for_update:
            self.skipTest('Requires PostgreSQL row locks')
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.guard.stop()
        self.fixture.config.website_connection = self.connection
        self.fixture.config.github_repo = self.connection.github_repo
        self.fixture.config.save(update_fields=['website_connection', 'github_repo'])
        request = {key: value for key, value in self.request.items() if key not in {'operation_id', 'operation_attempt'}}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(run_request=request)
        payload = {key: value for key, value in self.payload.items() if key not in {'operation_id', 'operation_attempt'}}
        barrier = Barrier(2)
        def verify(connection, sha):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            barrier.wait(timeout=15)
            return sha
        def apply():
            close_old_connections()
            try:
                return repin.repin_source('repin-test', payload)['status']
            except WebsiteAuthorityError as error:
                return error.authority_code
            finally:
                close_old_connections()
        with patch('content_factory.website_connections.verify_repository_head', side_effect=verify), \
             patch('content_factory.website_connections.verify_repository_native_target'), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(apply) for _ in range(2)]
            statuses = [future.result(timeout=30) for future in futures]
        self.assertEqual(statuses.count('repinned'), 1, statuses)
        self.assertTrue(set(statuses) <= {'repinned', 'already_repinned', 'website_connection_changed', 'website_authority_raced'}, statuses)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result['re_pin_v1']['repins'], 1)

    def test_optional_null_binding_fields_do_not_reject_a_valid_request(self):
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(run_request={**self.request, 'connection_target_id': None})
        self.assertEqual(self.apply()['status'], 'repinned')

    def test_nonrepository_workflow_is_refused(self):
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(workflow='island_refresh')
        with self.assertRaises(WebsiteAuthorityError):
            self.apply()
        self.assertFalse(self.provider_calls)
