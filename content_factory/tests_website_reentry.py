"""Real HTTP worker -> backend reentry under PostgreSQL row locks."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
import requests
from rest_framework.test import APIRequestFactory

from workflow_runs.models import ContentFactoryRun
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import authority_guard, owner_operation_scope, transition_connection, require_unlocked_remote_call
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation
from .website_views import WebsiteConnectionAuthorizeView, WebsiteMutationView, guarded_owner_operation


@override_settings(ROO_API_KEY='synthetic-test-key', INTERNAL_API_KEY='synthetic-test-key')
class WebsiteWorkerReentryTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('Actual row-lock reentry requires PostgreSQL')
        self.context = SimpleNamespace(organization=self.org, profile=SimpleNamespace(user=object()))
        self.errors = []

    @contextmanager
    def worker(self, *, disconnect=False, purge=False):
        """Worker calls real backend API views through a second loopback HTTP request."""
        testcase = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, value, status=200):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                close_old_connections()
                try:
                    request = APIRequestFactory().get('/authorize', dict(parse_qsl(urlsplit(self.path).query)), HTTP_X_API_KEY='synthetic-test-key')
                    response = WebsiteConnectionAuthorizeView.as_view()(request)
                    self.send_json(response.data, response.status_code)
                except Exception as exc:
                    testcase.errors.append(exc)
                    self.send_json({'error': str(exc)}, 500)
                finally:
                    connections.close_all()

            def do_POST(self):
                close_old_connections()
                try:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path == '/ledger':
                        response = WebsiteMutationView.as_view()(APIRequestFactory().post('/ledger', payload, format='json', HTTP_X_API_KEY='synthetic-test-key'))
                        self.send_json(response.data, response.status_code)
                        return
                    if self.path == '/mirror':
                        from .service_views import ContentFactoryRunView
                        response = ContentFactoryRunView.as_view()(APIRequestFactory().put('/mirror', payload, format='json', HTTP_X_API_KEY='synthetic-test-key'), run_id='cleanup-loopback')
                        self.send_json(response.data, response.status_code)
                        return
                    if purge:
                        # Reproduce actual lock inversion: offboarding holds org
                        # then needs this operation, while cancellation mirrors
                        # its status through a callback that also needs the org.
                        from .website_connections import offboard_website_connections
                        from organizations.models import Organization
                        org_locked = threading.Event()
                        def offboard():
                            close_old_connections()
                            try:
                                with transaction.atomic():
                                    Organization.objects.select_for_update().get(pk=testcase.org.pk)
                                    org_locked.set()
                                    offboard_website_connections(testcase.org, purge=True)
                            except Exception as exc:
                                testcase.errors.append(exc)
                            finally:
                                connections.close_all()
                        deleting = threading.Thread(target=offboard, daemon=True)
                        deleting.start()
                        if not org_locked.wait(2):
                            raise RuntimeError('Offboarding did not acquire its company lock')
                        base = f'http://127.0.0.1:{self.server.server_port}'
                        mirrored = requests.post(base + '/mirror', json={**testcase.binding,
                            'workflow': 'article_generation', 'status': 'cancelled'}, timeout=3)
                        if mirrored.status_code != 409:
                            raise RuntimeError('Revoked cleanup callback unexpectedly admitted')
                        deleting.join(3)
                        if deleting.is_alive():
                            raise RuntimeError('Offboarding remained blocked by cleanup HTTP')
                        self.send_json({'cleanup_success': True, 'cleanup_pending': False})
                        return
                    # This second HTTP request takes the same org + connection
                    # locks as production middleware/token/config callbacks.
                    base = f'http://127.0.0.1:{self.server.server_port}'
                    auth = requests.get(base + '/authorize?' + urlencode({**payload, 'action': 'read'}), timeout=2)
                    auth.raise_for_status()
                    ledger = requests.post(base + '/ledger', json={**payload, 'operation_id': 'loopback-intent', 'base_sha': 'a' * 40, 'files': []}, timeout=2)
                    ledger.raise_for_status()
                    if disconnect:
                        transition_connection(testcase.config, action='disconnect', expected=testcase.binding)
                    self.send_json({'run_id': 'loopback-run', 'status': 'queued'})
                except Exception as exc:
                    testcase.errors.append(exc)
                    self.send_json({'error': str(exc)}, 500)
                finally:
                    connections.close_all()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        remote = {'enabled': True, 'base_url': f'http://127.0.0.1:{server.server_port}', 'api_key_configured': True, 'is_local_env': False}
        try:
            with (
                patch('content_factory.vibe_marketing_views._content_factory_remote_config', return_value=remote),
                patch('content_factory.vibe_marketing_views._content_factory_headers', return_value={'X-Api-Key': 'synthetic-test-key'}),
                patch('content_factory.vibe_marketing_views.founder_actor_id_for_user', return_value='synthetic'),
            ):
                yield
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertEqual(self.errors, [])

    def dispatch(self):
        from .vibe_marketing_views import _queue_content_factory_run
        testcase = self
        class Owner:
            @guarded_owner_operation('scan')
            def post(self, request):
                return _queue_content_factory_run(endpoint='scan', workflow='repo_scan', context=testcase.context,
                    config=testcase.config, payload={'domain': testcase.org.domain, 'github_repo': testcase.website.github_repo})
        with patch('content_factory.website_views._context', return_value=(self.context, self.config, None)):
            return Owner().post(SimpleNamespace(data=self.binding))

    def test_explicit_database_transaction_rejects_remote_call_before_http(self):
        from .vibe_marketing_views import _call_content_factory_run_action
        with transaction.atomic(), patch('content_factory.vibe_marketing_views.http_client.post') as remote:
            with self.assertRaisesMessage(RuntimeError, 'outside database transactions'):
                _call_content_factory_run_action(run_id='not-yet-dispatched', action='resume', payload={})
            remote.assert_not_called()

    def test_settings_commit_before_full_bootstrap_reentry(self):
        from .vibe_marketing_views import VibeMarketingSettingsView
        testcase = self
        class Settings(VibeMarketingSettingsView):
            @transaction.atomic
            def _save_settings(self, request):
                # Exercise the exact row held by real settings writes and worker
                # consent. The inherited put must serialize only after commit.
                testcase.org.name = 'Changed company name'
                testcase.org.save(update_fields=['name'])
                return testcase.context
        def bootstrap(context, **kwargs):
            require_unlocked_remote_call()
            return {'run_id': self.dispatch().run_id}
        with self.worker(), patch('content_factory.vibe_marketing_views._serialize_bootstrap', side_effect=bootstrap):
            response = Settings().put(SimpleNamespace(data={}, user=object()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['run_id'], 'loopback-run')

    def test_owner_dispatch_reentrant_authorize_and_ledger_do_not_deadlock(self):
        with self.worker():
            run = self.dispatch()
        self.assertEqual(run.run_id, 'loopback-run')
        self.assertEqual(run.status, 'queued')
        self.assertEqual(run.run_request['website_connection_id'], str(self.website.pk))
        self.assertEqual(self.website.repository_mutations.count(), 1)

    def test_disconnect_during_dispatch_keeps_late_response_cancelled(self):
        with self.worker(disconnect=True):
            run = self.dispatch()
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        operation = WebsiteConnectionOperation.objects.get(idempotency_key=f'{self.website.pk}:late-dispatch:loopback-run')
        self.assertEqual(operation.payload['cancel_run_ids'], ['loopback-run'])
        self.config.refresh_from_db()
        self.assertEqual(self.config.publish_targets, [])

    def test_resumed_owner_control_reauthorizes_without_outer_transaction(self):
        from .vibe_marketing_views import VibeMarketingRunControlView
        run = ContentFactoryRun.objects.create(run_id='loopback-run', workflow='article_system_setup',
            domain=self.org.domain, github_repo=self.website.github_repo, status='failed', run_request=self.binding)
        with (
            self.worker(),
            patch('content_factory.website_views._context', return_value=(self.context, self.config, None)),
            patch('content_factory.vibe_marketing_views._resolve_context_or_response', return_value=(self.context, None)),
            patch('content_factory.vibe_marketing_views._run_belongs_to_context', return_value=True),
            patch('content_factory.vibe_marketing_views._serialize_run', side_effect=lambda run, **kwargs: {'status': run.status}),
        ):
            response = VibeMarketingRunControlView().post(SimpleNamespace(data=self.binding, user=SimpleNamespace(pk=1)), run.run_id, 'resume')
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')

    def test_native_target_rejects_nondefault_branch_even_with_same_source_sha(self):
        self.website.branch = 'staging'
        self.website.save(update_fields=['branch'])
        with self.assertRaisesMessage(WebsiteAuthorityError, 'selected branch'):
            with authority_guard(self.binding, action='setup'):
                self.fail('Unsupported branch was authorized')

    def test_native_target_rejects_subroot_without_provider_access(self):
        self.website.app_root = 'apps/site'
        self.website.save(update_fields=['app_root'])
        with patch('content_factory.website_connections.read_repository_native_target') as provider:
            with self.assertRaisesMessage(WebsiteAuthorityError, 'application root'):
                with authority_guard(self.binding, action='setup'):
                    self.fail('Unsupported application root was authorized')
            provider.assert_not_called()

    def test_cleanup_http_reentry_and_offboarding_do_not_deadlock_or_replace_erasure_receipt(self):
        from .website_reconciliation import process_website_connection_operations
        ContentFactoryRun.objects.create(run_id='cleanup-loopback', workflow='article_generation',
            domain=self.org.domain, github_repo=self.website.github_repo, status='running', run_request=self.binding)
        operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            idempotency_key='cleanup-loopback-operation', action='disconnect',
            payload={'previous_generation': self.website.generation, 'cancel_run_ids': ['cleanup-loopback'], 'stop_preview_run_ids': []})
        with self.worker(purge=True):
            result = process_website_connection_operations(limit=1)
        self.assertEqual(result['pending'], 1)  # Our stale pre-erasure snapshot lost the CAS.
        operation.refresh_from_db()
        self.assertTrue(operation.payload['purge_after_reconciliation'])
        self.assertTrue(operation.receipt['website_database_evidence_erased'])
        self.assertEqual(operation.receipt['artifact_retention']['status'], 'retained')

    def test_callback_followup_is_durable_and_reauthorizes_outside_claim_lock(self):
        from .website_connections import queue_website_followup
        from .website_reconciliation import process_website_connection_operations
        from integrations import http_client
        operation = queue_website_followup('trigger_article_generation', data={**self.binding, 'job_id': 'callback-source'},
            arguments={'slack_user_id': 'synthetic', 'article_request': {'domain': self.org.domain, 'topic': 'Test'}})
        def dispatch(slack_user_id, article_request):
            from .vibe_marketing_views import _content_factory_remote_config
            return http_client.post(_content_factory_remote_config()['base_url'] + '/api/runs/article', json=article_request, timeout=(1, 3)).json()
        with self.worker(), patch('integrations.services.article_generation.trigger_article_generation', side_effect=dispatch):
            result = process_website_connection_operations()
        operation.refresh_from_db()
        self.assertEqual(result['completed'], 1)
        self.assertEqual(operation.state, 'completed')
        self.assertEqual(operation.receipt['run_id'], 'loopback-run')

    def test_disconnect_cancels_callback_followup_before_dispatch(self):
        from .website_connections import queue_website_followup
        from .website_reconciliation import process_website_connection_operations
        operation = queue_website_followup('trigger_article_generation', data={**self.binding, 'job_id': 'callback-source'},
            arguments={'slack_user_id': 'synthetic', 'article_request': {'domain': self.org.domain, 'topic': 'Test'}})
        transition_connection(self.config, action='disconnect', expected=self.binding)
        with patch('integrations.services.article_generation.trigger_article_generation') as dispatch:
            process_website_connection_operations(limit=10)
            dispatch.assert_not_called()
        operation.refresh_from_db()
        self.assertEqual(operation.state, 'cancelled')
