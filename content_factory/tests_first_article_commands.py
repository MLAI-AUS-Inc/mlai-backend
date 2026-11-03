"""Authenticated first-start retries retain the original debit, grant and dispatch."""
from django.db import connections
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from integrations.services.article_generation import charge_content_factory_request_for_user
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import first_article_commands as commands
from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .tests_content_first_article import CardFixture
from .tests_run_event_authority import FLAGS, batch
from .run_event_authority import receive_authoritative_run_events


@override_settings(**FLAGS, ARTICLES_RUN_CONTROL_MODEL_USD='5', ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)
class FirstArticleCommandTests(CardFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(CONTENT_FACTORY_URL='https://first-start.fixture.invalid',
            CONTENT_FACTORY_API_KEY='synthetic-first-start-key'))
        self.posted = []
        def worker(url, *, json, **kwargs):
            import requests
            self.posted.append(deepcopy(json))
            self.assertIn('backend_run_admission_v1', json)
            response = requests.Response(); response.status_code = 202
            response._content = b'{"run_id":"first-start-worker","status":"queued"}'
            response._content_consumed = True
            return response
        self.enterContext(patch('integrations.http_client.post', side_effect=worker))
        self.client = APIClient(); self.client.force_authenticate(self.owner)
        self.body = {'customTopic': 'Booking reminders for busy clinics', 'expectedCostPoints': 0}
        self.url = f'/api/v1/my-startup/first-article/start/?company_id={self.company.pk}'

    def post(self, key='retained-start-key', body=None, client=None):
        return (client or self.client).post(self.url, body or self.body, format='json', HTTP_IDEMPOTENCY_KEY=key)

    def complete(self, run, status='completed'):
        response = receive_authoritative_run_events(batch(run, [{'status': status}]))
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()

    def test_free_retry_after_terminal_replays_original_grant_and_run_without_dispatch(self):
        first = self.post()
        self.assertEqual(first.status_code, 201, first.data)
        run = ContentFactoryRun.objects.get(run_id=first.data['runId'])
        self.complete(run, 'failed')
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(views, 'start_article_generation', side_effect=AssertionError('No second dispatch')):
            replay = self.post()
        self.assertEqual((replay.status_code, replay.data['runId'], replay.data['status']), (200, run.run_id, 'failed'))
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(Ledger.objects.filter(kind='ADJUST').count(), 1)
        self.assertFalse(Ledger.objects.filter(kind__in=['SPEND', 'REFUND']).exists())
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_actual_admitted_dispatch_alias_route_echoes_requested_selector_and_bound_worker_without_effects(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        first = self.post()
        self.assertEqual(first.status_code, 201, first.data)
        run = ContentFactoryRun.objects.get(run_id=first.data['runId'])
        logical_key = run.run_request['client_request_id']
        self.assertNotEqual(logical_key, run.run_id)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        outbox = list(ContentFactoryDispatchOutbox.objects.values())
        with patch.object(views, '_call_content_factory_run_status', side_effect=AssertionError('Alias read cannot poll')):
            with CaptureQueriesContext(connection) as queries:
                value = self.client.get(f'/api/v1/vibe-marketing/runs/{logical_key}/',
                    {'company_id': str(self.company.pk), 'view': 'route'})
        self.assertEqual(value.status_code, 200, value.data)
        self.assertEqual(value.data, {'contractVersion': 2, 'companyId': str(self.company.pk),
            'requestedRunId': logical_key, 'runId': run.run_id, 'kind': 'article'})
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        self.assertEqual(list(ContentFactoryDispatchOutbox.objects.values()), outbox)
        self.assertEqual(len(self.posted), 1)

    def test_paid_completed_retry_and_lost_ack_keep_one_actual_spend_and_original_key(self):
        from integrations.services import article_generation as generation
        self.balance = 100
        PointsService.credit_purchased_topup(user=self.owner, delta=100,
            description='Synthetic first-start funding', idempotency_key='first-start-funding')
        self.body['expectedCostPoints'] = 12
        def lose_ack(*args, **kwargs):
            raise ConnectionError('Synthetic lost HTTP acknowledgement')
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False), \
                patch.object(generation, 'get_content_factory_article_cost_points', return_value=12), \
                patch.object(views, 'charge_content_factory_request_for_user', side_effect=charge_content_factory_request_for_user), \
                patch.object(commands, 'finish', side_effect=lose_ack):
            with self.assertRaises(ConnectionError):
                self.post()
        run = ContentFactoryRun.objects.get()
        self.complete(run)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(views, 'start_article_generation', side_effect=AssertionError('No second paid dispatch')):
            replay = self.post()
        self.assertEqual((replay.status_code, replay.data['runId']), (200, run.run_id))
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        spend = Ledger.objects.get(kind='SPEND')
        PointsService.refund(user=self.owner, delta=12, source=spend.source,
            description='Synthetic actual original refund', idempotency_key='first-start-original-refund',
            created_by_slack_id=f'mlai_user:{self.owner.pk}',
            original_spend_key=spend.idempotency_key, reference_type=spend.reference_type, reference_id=spend.reference_id)
        with patch.object(views, 'start_article_generation', side_effect=AssertionError('No new paid command')):
            self.assertEqual(self.post().data['runId'], run.run_id)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)

    def test_conflicting_body_and_frozen_current_owner_refuse_before_effects(self):
        first = self.post()
        self.assertEqual(first.status_code, 201, first.data)
        conflicting = self.post(body={**self.body, 'customTopic': 'Different paid article'})
        self.assertEqual((conflicting.status_code, conflicting.data['reason']), (409, 'operation_conflict'))
        self.store(startup_team={'version': 1, 'owner_user_id': self.owner.pk, 'frozen': True,
            'members': [], 'requests': [], 'invite': None, 'history': []})
        with patch.object(views, 'start_article_generation') as start:
            denied = self.post()
        self.assertEqual(denied.status_code, 403)
        start.assert_not_called()
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)

    def test_concurrent_same_key_claim_is_durable_when_cache_is_unavailable(self):
        entered, release = Event(), Event()
        original = views.start_article_generation
        def delayed(**kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(**kwargs)
        def first():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.owner)
                return self.post(client=client)
            finally:
                connections.close_all()
        with patch.object(views, 'start_article_generation', side_effect=delayed), \
                patch('django.core.cache.cache.add', side_effect=ConnectionError('Synthetic unavailable cache')), \
                ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first)
            self.assertTrue(entered.wait(10))
            try:
                concurrent = self.post()
                self.assertEqual((concurrent.status_code, concurrent.data['reason']), (409, 'in_progress'))
            finally:
                release.set()
            result = pending.result(timeout=20)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(self.post().data['runId'], result.data['runId'])
        self.assertEqual(len(self.posted), 1)

    def test_owner_frozen_between_preflight_and_debit_refuses_under_the_money_lock(self):
        def freeze(*args, **kwargs):
            self.store(startup_team={'version': 1, 'owner_user_id': self.owner.pk, 'frozen': True,
                'members': [], 'requests': [], 'invite': None, 'history': []})
        with patch.object(views, '_validate_article_start_admission', side_effect=freeze):
            result = self.post()
        self.assertEqual(result.status_code, 403, result.data)
        self.assertFalse(ContentFactoryRun.objects.exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertFalse(Ledger.objects.filter(kind__in=['SPEND', 'ADJUST']).exists())
        self.assertEqual(self.posted, [])
