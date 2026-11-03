"""Actual scoped HTTP switches keep their original cancellation and free attempt."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import requests
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from workflow_runs.models import ContentFactoryRun
from . import content_first_article as cards
from . import first_article_commands as commands
from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .tests_content_first_article import CardFixture, free_rows
from . import tests_content_first_article as fixtures


class SwitchHTTPFixture(CardFixture):
    owned = False

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(CONTENT_FACTORY_URL='https://switch.fixture.invalid',
            CONTENT_FACTORY_API_KEY='synthetic-switch-key'))
        self.posted, self.stops = [], []

        def worker(url, *, json, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            if self.owned and url.endswith('/cancel'):
                return self.cancel_response(json)
            self.posted.append(deepcopy(json))
            response = requests.Response()
            response.status_code = 202
            response._content = ('{"run_id":"switch-worker-%s","status":"queued"}' % len(self.posted)).encode()
            response._content_consumed = True
            return response

        self.enterContext(patch('integrations.http_client.post', side_effect=worker))
        if not self.owned:
            self.enterContext(patch.object(views, '_call_content_factory_run_action', side_effect=self.remote_stop))
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        first = self.client.post(f'/api/v1/my-startup/first-article/start/?company_id={self.company.pk}',
            {'customTopic': 'Clinic booking reminders', 'expectedCostPoints': 0}, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.original = ContentFactoryRun.objects.get(run_id=first.data['runId'])
        ContentFactoryRun.objects.filter(pk=self.original.pk).update(
            created_at=self.original.created_at - timedelta(minutes=40))
        if self.owned:
            from .tests_run_event_authority import batch
            from .run_event_authority import receive_authoritative_run_events
            response = receive_authoritative_run_events(batch(self.original, [{'status': 'running'}]))
            self.assertEqual(response.status_code, 200, response.data)
        else:
            ContentFactoryRun.objects.filter(pk=self.original.pk).update(status='running')
        self.original.refresh_from_db()
        self.url = f'/api/v1/my-startup/first-article/switch-topic/?company_id={self.company.pk}'
        self.body = {'runId': self.original.run_id, 'customTopic': 'A different angle on clinic bookings'}

    def remote_stop(self, **kwargs):
        kwargs.pop('control_user', None)
        kwargs.pop('control_command_key', None)
        return fixtures.SwitchTopicTests.remote_stop(self, **kwargs)

    def switch(self, *, key='original-switch', body=None, client=None):
        return (client or self.client).post(self.url, self.body if body is None else body,
            format='json', HTTP_IDEMPOTENCY_KEY=key)


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False)
class FirstArticleSwitchCommandTests(SwitchHTTPFixture, TransactionTestCase):
    def test_completed_replay_returns_only_its_original_child_without_effects(self):
        first = self.switch()
        self.assertEqual(first.status_code, 201, first.data)
        child = ContentFactoryRun.objects.get(run_id=first.data['runId'])
        ContentFactoryRun.objects.filter(pk=child.pk).update(status='completed')
        ContentFactoryRun.objects.create(run_id='unrelated-latest-run', organization=self.org,
            domain=self.org.domain, workflow='article_generation', status='running')
        before = list(ContentFactoryDispatchOutbox.objects.values())
        grants = list(free_rows(self.org).values())
        replay = self.switch()
        self.assertEqual((replay.status_code, replay.data['runId'], replay.data['status']),
            (200, child.run_id, 'completed'))
        self.assertEqual(list(ContentFactoryDispatchOutbox.objects.values()), before)
        self.assertEqual(list(free_rows(self.org).values()), grants)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))
        self.assertEqual(self.debits, [])

    def test_unknown_cancel_keeps_original_attempt_and_never_starts_replacement(self):
        before = list(free_rows(self.org).values())
        with patch.object(views, '_call_content_factory_run_action', return_value={'status': 'cancelled'}):
            response = self.switch()
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(list(free_rows(self.org).values()), before)
        self.original.refresh_from_db()
        self.assertEqual(self.original.status, 'running')
        self.assertEqual(len(self.posted), 1)

    def test_crash_after_cancel_resumes_exact_prepared_replacement(self):
        with patch.object(cards, '_dispatch', side_effect=ConnectionError('Synthetic crash before replacement')):
            with self.assertRaises(ConnectionError):
                self.switch()
        self.original.refresh_from_db()
        self.assertEqual(self.original.status, 'cancelled')
        response = self.switch()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))
        self.assertEqual(self.posted[-1]['topic'], self.body['customTopic'])
        self.assertEqual(self.debits, [])

    def test_lost_response_after_child_dispatch_replays_original_outbox(self):
        with patch.object(commands, 'finish', side_effect=ConnectionError('Synthetic lost HTTP acknowledgement')):
            with self.assertRaises(ConnectionError):
                self.switch()
        child = ContentFactoryRun.objects.exclude(pk=self.original.pk).get()
        response = self.switch()
        self.assertEqual((response.status_code, response.data['runId']), (200, child.run_id))
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))

    def test_changed_body_or_target_cannot_reuse_the_original_switch_key(self):
        first = self.switch()
        self.assertEqual(first.status_code, 201, first.data)
        for changed in ({**self.body, 'customTopic': 'Different intent'}, {**self.body, 'runId': first.data['runId']}):
            response = self.switch(body=changed)
            self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))

    def test_current_inactive_owner_cannot_recover_or_repeat_effects(self):
        self.assertEqual(self.switch().status_code, 201)
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        response = self.switch()
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))

    def test_client_uuid_and_full_catalogue_body_replay_after_process_memory_loss(self):
        body = {'runId': self.original.run_id, 'topicId': self.card()['alternatives'][0]['id'], 'customTopic': None}
        key = str(uuid4())
        first = self.switch(key=key, body=body)
        self.assertEqual(first.status_code, 201, first.data)
        from django.core.cache import cache
        cache.clear()
        another = APIClient(); another.force_authenticate(self.owner)
        replay = self.switch(key=key, body=deepcopy(body), client=another)
        self.assertEqual((replay.status_code, replay.data['runId']), (200, first.data['runId']))
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))

    def test_concurrent_same_key_is_durable_during_cache_outage(self):
        entered, release = Event(), Event()
        def stop(**kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return self.remote_stop(**kwargs)
        def first():
            close_old_connections()
            try:
                client = APIClient(); client.force_authenticate(self.owner)
                return self.switch(client=client)
            finally:
                connections.close_all()
        with patch.object(views, '_call_content_factory_run_action', side_effect=stop), \
                patch('django.core.cache.cache.add', side_effect=ConnectionError('Synthetic unavailable cache')), \
                ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first)
            self.assertTrue(entered.wait(10))
            try:
                concurrent = self.switch()
                self.assertEqual((concurrent.status_code, concurrent.data['reason']), (409, 'in_progress'))
            finally:
                release.set()
            result = pending.result(timeout=20)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.switch().data['runId'], result.data['runId'])
        self.assertEqual((ContentFactoryDispatchOutbox.objects.count(), len(self.stops), len(self.posted)), (2, 1, 2))

    def test_revoked_owner_after_cancel_prevents_release_and_replacement(self):
        before = list(free_rows(self.org).values())
        def stop(**kwargs):
            receipt = self.remote_stop(**kwargs)
            type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
            return receipt
        with patch.object(views, '_call_content_factory_run_action', side_effect=stop):
            response = self.switch()
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(list(free_rows(self.org).values()), before)
        self.assertEqual(len(self.posted), 1)

    def test_revoked_owner_between_preflight_and_actual_debit_prevents_replacement(self):
        def revoke(*args, **kwargs):
            type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        with patch.object(views, '_validate_article_start_admission', side_effect=revoke):
            response = self.switch()
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual((len(self.posted), ContentFactoryDispatchOutbox.objects.count()), (1, 1))
        self.assertEqual(self.debits, [])

    def test_changed_source_binding_after_cancellation_cannot_dispatch(self):
        def stop(**kwargs):
            receipt = self.remote_stop(**kwargs)
            ContentFactoryRun.objects.filter(pk=self.original.pk).update(github_repo='different/repository')
            return receipt
        with patch.object(views, '_call_content_factory_run_action', side_effect=stop):
            response = self.switch()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(len(self.posted), 1)

    def test_invalid_remote_status_stays_pending_without_effects(self):
        def stop(**kwargs):
            return {**self.remote_stop(**kwargs), 'content_factory_status_code': 'untrusted'}
        with patch.object(views, '_call_content_factory_run_action', side_effect=stop):
            response = self.switch()
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(len(self.posted), 1)

    def test_changed_cap_after_cancellation_never_falls_back_to_a_paid_attempt(self):
        def stop(**kwargs):
            receipt = self.remote_stop(**kwargs)
            self.enterContext(override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=1))
            return receipt
        with patch.object(views, '_call_content_factory_run_action', side_effect=stop):
            response = self.switch()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual((len(self.posted), ContentFactoryDispatchOutbox.objects.count()), (1, 1))
        self.assertEqual(self.debits, [])


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class OwnedFirstArticleSwitchCommandTests(SwitchHTTPFixture, TransactionTestCase):
    owned = True

    def setUp(self):
        self.stopped = False
        self.close_available = True
        self.close_cursor = 2
        super().setUp()
        self.enterContext(patch('requests.get', side_effect=self.history))
        self.enterContext(patch('requests.post', side_effect=self.close))

    def response(self, body, status=200):
        response = requests.Response(); response.status_code = status
        response._content = json.dumps(body).encode(); response._content_consumed = True
        return response

    def cancel_response(self, payload):
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, owner_receipt, wire_identity, current_execution_ticket
        self.assertEqual(payload[WIRE_REQUEST_KEY], wire_identity(owner_receipt(self.original)))
        self.assertEqual(payload[WIRE_EXECUTION_KEY], current_execution_ticket(self.original))
        self.stops.append(deepcopy(payload))
        self.stopped = True
        return self.response({'status': 'cancelled'})

    def history(self, url, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        if not url.endswith('/events'):
            return self.response({}, 404)
        from .tests_run_event_authority import batch
        after = kwargs['params']['after_seq']
        raw = batch(self.original, [{'status': 'running'}, {'status': 'cancelled'}] if self.stopped else [{'status': 'running'}])
        raw['events'] = [event for event in raw['events'] if event['seq'] > after]
        cursor = raw['events'][-1]['seq'] if raw['events'] else after
        return self.response({'schema_version': 1, 'run_id': self.original.run_id,
            'batches': [raw] if raw['events'] else [], 'next_cursor': cursor, 'has_more': False})

    def close(self, url, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        self.assertTrue(url.endswith('/' + self.original.run_id + '/cancel'))
        if not self.close_available:
            return self.response({'status': 'cancelled'})
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, owner_receipt, wire_identity, current_execution_ticket
        from .run_terminal_settlement import receipt_digest
        receipt = {'schema_version': 1, 'run_id': self.original.run_id, 'status': 'closed_no_effects',
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.original)),
            WIRE_EXECUTION_KEY: current_execution_ticket(self.original),
            'event_cursor': self.close_cursor, 'closed_at': timezone.now().isoformat()}
        receipt['receipt_sha256'] = receipt_digest(receipt)
        return self.response({'terminal_settlement_v1': receipt})

    def test_current_owned_close_and_drained_journal_release_once_and_replay_exact_child(self):
        response = self.switch()
        self.assertEqual(response.status_code, 201, response.data)
        from .run_dispatch_settlement import _free_obligation_state
        from .run_terminal_settlement import terminal_closed_without_effects
        self.original.refresh_from_db()
        self.assertTrue(terminal_closed_without_effects(self.original))
        self.assertEqual(_free_obligation_state(self.original), 'released')
        grants = list(free_rows(self.org).values())
        replay = self.switch()
        self.assertEqual((replay.status_code, replay.data['runId']), (200, response.data['runId']))
        self.assertEqual(list(free_rows(self.org).values()), grants)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))
        self.assertEqual(self.debits, [])

    def test_terminal_status_without_typed_closure_never_releases_or_dispatches(self):
        before = list(free_rows(self.org).values())
        self.close_available = False
        response = self.switch()
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(list(free_rows(self.org).values()), before)
        self.assertEqual(len(self.posted), 1)

    def test_pending_owned_closure_can_resume_same_key_without_another_cancel(self):
        self.close_available = False
        pending = self.switch()
        self.assertEqual(pending.status_code, 503, pending.data)
        self.close_available = True
        response = self.switch()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((len(self.stops), len(self.posted)), (1, 2))

    def test_closed_receipt_ahead_of_drained_history_cannot_release_or_dispatch(self):
        before = list(free_rows(self.org).values())
        self.close_cursor = 3
        response = self.switch()
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(list(free_rows(self.org).values()), before)
        self.assertEqual(len(self.posted), 1)
