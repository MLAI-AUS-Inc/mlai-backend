"""Synthetic concurrency, shared-quota telemetry and initial-import fairness."""
import asyncio
import json
import threading
from datetime import timedelta
from io import StringIO
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import (
    BridgeApiBudget, BridgeSyncJob, CommunityBridgeChannel, ExternalServiceConnection,
    SlackDmMirrorConversation, SlackDmMirrorGrant,
)
from integrations.services.message_sync import execution, telemetry
from integrations.services.message_sync.history import ensure_state
from integrations.services.message_sync.scheduler import BudgetDeferred, claim_job, finish_job
from integrations.services.message_sync.slack_client import budgeted_client


class IndependentSlotTests(IsolatedAsyncioTestCase):
    async def test_fast_slot_repeats_while_sibling_is_blocked_and_cancellation_stops_claims(self):
        release = threading.Event()
        started = threading.Event()
        fast_progress = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        counts = [0, 0]

        def operation(slot):
            counts[slot] += 1
            if slot == 0:
                started.set()
                if not release.wait(timeout=5):
                    raise AssertionError("blocked slot was not released")
            elif counts[1] >= 3:
                event_loop.call_soon_threadsafe(fast_progress.set)
            return 1

        with patch.object(execution, "close_old_connections"):
            task = asyncio.create_task(execution.run_slots(operation, slots=2, idle_seconds=.05))
            try:
                await asyncio.wait_for(fast_progress.wait(), timeout=3)
                self.assertTrue(started.is_set())
                self.assertEqual(counts[0], 1)
                self.assertGreaterEqual(counts[1], 3)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
            stopped = list(counts)
            await asyncio.sleep(.1)
            self.assertEqual(counts, stopped)

    async def test_read_pool_remains_available_when_all_history_slots_are_blocked(self):
        release = threading.Event()
        blocked = threading.Barrier(5)
        read_ran = asyncio.Event()
        loop = asyncio.get_running_loop()

        def history(_):
            blocked.wait(timeout=5)
            release.wait(timeout=5)
            return 1

        def read(_):
            loop.call_soon_threadsafe(read_ran.set)
            return 1

        with patch.object(execution, "close_old_connections"):
            task = asyncio.create_task(execution.run_slots(history, slots=4))
            read_task = None
            try:
                await asyncio.to_thread(blocked.wait, timeout=3)
                read_task = asyncio.create_task(execution.run_slots(read, slots=2))
                await asyncio.wait_for(read_ran.wait(), timeout=2)
                self.assertFalse(release.is_set())
            finally:
                for pending in (task, read_task):
                    if pending:
                        pending.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await pending
                release.set()

    async def test_both_worker_entrypoints_use_shared_bounded_lanes(self):
        from integrations.services.community_bridge import worker
        with patch.object(worker, "run_lane") as lane, patch.object(worker, "process_history_once") as page:
            await worker._run_history_workers()
            kwargs = lane.call_args.kwargs
            self.assertEqual(kwargs['slots'], 4)
            for slot in range(4):
                lane.call_args.args[0](slot)
            self.assertEqual([c.kwargs['prefer_import'] for c in page.call_args_list], [True, False, True, False])
            await worker._run_read_state_workers()
            self.assertEqual(lane.call_args.kwargs['slots'], 2)
            self.assertEqual(lane.call_args.kwargs['lane'], 'read_state')


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_SLACK_APP_ID='ATEST')
class ThroughputTelemetryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.scope = telemetry.scope_key('ATEST', 'TTEST', 'conversations.history')

    def snapshot(self):
        return telemetry.snapshot([self.scope], minutes=1, now=120)['scopes'][self.scope]

    def test_success_records_only_completed_minute_counters(self):
        client = MagicMock()
        client.api_call.return_value = {'ok': True, 'messages': [{'text': 'secret content'}]}
        with patch('integrations.services.message_sync.slack_client.admit_request'), patch.object(telemetry, 'time', return_value=61):
            wrapped = budgeted_client(client, workspace_id='TTEST')
            self.assertTrue(wrapped.api_call('conversations.history')['ok'])
            self.assertIsNone(telemetry.snapshot([self.scope], minutes=1, now=61)['scopes'][self.scope])
        value = self.snapshot()
        self.assertEqual(value['admitted'], 1)
        self.assertEqual(value['finished'], 1)
        self.assertNotIn('secret content', json.dumps(value))

    def test_local_deferral_does_not_count_as_provider_request(self):
        client = MagicMock()
        upstream = client.api_call
        with patch('integrations.services.message_sync.slack_client.admit_request', side_effect=BudgetDeferred(2)), patch.object(telemetry, 'time', return_value=61):
            wrapped = budgeted_client(client, workspace_id='TTEST')
            with self.assertRaises(BudgetDeferred):
                wrapped.api_call('conversations.history')
        upstream.assert_not_called()
        self.assertEqual(self.snapshot()['deferred'], 1)
        self.assertEqual(self.snapshot()['admitted'], 0)

    def test_provider_429_records_shared_cooldown_and_preserves_retry_after(self):
        response = MagicMock(status_code=429, headers={'Retry-After': '17'})
        client = MagicMock()
        client.api_call.side_effect = SlackApiError('synthetic', response)
        with patch('integrations.services.message_sync.slack_client.admit_request'), patch(
            'integrations.services.message_sync.slack_client.record_cooldown'
        ) as cooldown, patch.object(telemetry, 'time', return_value=61):
            with self.assertRaises(BudgetDeferred) as deferred:
                budgeted_client(client, workspace_id='TTEST').api_call('conversations.history')
        self.assertEqual(deferred.exception.retry_after, 17)
        self.assertEqual(cooldown.call_args.kwargs['retry_after'], 17)
        self.assertEqual(self.snapshot()['rate_limited'], 1)
        self.assertEqual(self.snapshot()['admitted'], 1)

    def test_cache_failure_cannot_discard_provider_response(self):
        client = MagicMock()
        client.api_call.return_value = {'ok': True}
        with patch('integrations.services.message_sync.slack_client.admit_request'), patch.object(telemetry.cache, 'add', side_effect=RuntimeError):
            self.assertEqual(budgeted_client(client, workspace_id='TTEST').api_call('conversations.history'), {'ok': True})

    def test_different_app_and_workspace_scopes_never_share_counters(self):
        scopes = [telemetry.scope_key(a, w, 'conversations.history') for a, w in [('A1','T1'), ('A2','T1'), ('A1','T2')]]
        with patch.object(telemetry, 'time', return_value=61):
            for i, scope in enumerate(scopes):
                telemetry.record(scope, 'admitted', i+1)
        rows = telemetry.snapshot(scopes, minutes=1, now=120)['scopes']
        self.assertEqual([rows[s]['admitted'] for s in scopes], [1, 2, 3])


class InitialImportPriorityTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.old = ensure_state(self.conversation)
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.save(update_fields=['history_backfilled_at'])
        self.recent = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id=self.grant.slack_workspace_id,
            slack_conversation_id='DRECENT', status='live', latest_synced_ts=f'{int(timezone.now().timestamp())}.000001',
        )
        self.pending = ensure_state(self.recent)
        BridgeSyncJob.objects.filter(state=self.pending).update(last_served_at=timezone.now())

    def test_import_slot_prioritizes_unfinished_recent_archive_but_normal_slot_preserves_fairness(self):
        lease = claim_job(prefer_import=True)
        self.assertEqual((lease.state_id, lease.kind), (self.pending.pk, 'archive'))
        finish_job(lease, checkpoint={'cursor': 'saved-page'})
        normal = claim_job()
        self.assertEqual(normal.state_id, self.old.pk)
        finish_job(normal, checkpoint={})
        resumed = claim_job(prefer_import=True)
        self.assertEqual(resumed.checkpoint, {'cursor': 'saved-page'})

    def test_priority_does_not_promote_outside_window_or_completed_import(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                self.recent.latest_synced_ts = f'{int((timezone.now()-timedelta(days=31)).timestamp())}.000001'
                self.recent.history_backfilled_at = timezone.now() if complete else None
                self.recent.save()
                lease = claim_job(prefer_import=True)
                self.assertEqual(lease.state_id, self.old.pk)
                finish_job(lease, checkpoint={})
                BridgeSyncJob.objects.filter(state=self.old).update(last_served_at=None)

    def test_inflight_import_still_excludes_all_other_jobs_for_same_conversation(self):
        lease = claim_job(prefer_import=True)
        other = claim_job(prefer_import=True)
        self.assertEqual(lease.state_id, self.pending.pk)
        self.assertEqual(other.state_id, self.old.pk)

    def test_initial_import_priority_never_overrides_another_owners_turn(self):
        public = ensure_state(CommunityBridgeChannel.objects.create(
            slack_workspace_id=self.grant.slack_workspace_id, slack_channel_id='COTHER',
            destination_platform='buzz', destination_workspace_id='test.invalid',
            destination_channel_id='public-room',
        ))
        # This owner has never had a turn; a large private import cannot jump it.
        first = claim_job(prefer_import=True)
        self.assertEqual((first.state_id, first.kind), (public.pk, 'archive'))
        finish_job(first, checkpoint={}, delay_seconds=60, complete=True)
        second = claim_job(prefer_import=True)
        self.assertEqual(second.state_id, self.pending.pk)

    def test_seven_day_consent_does_not_prioritize_activity_eight_days_old(self):
        self.grant.history_days = 7
        self.grant.save(update_fields=['history_days'])
        self.recent.latest_synced_ts = f'{int((timezone.now()-timedelta(days=8)).timestamp())}.000001'
        self.recent.save(update_fields=['latest_synced_ts'])
        self.assertEqual(claim_job(prefer_import=True).state_id, self.old.pk)

    def test_large_owner_cannot_refund_its_turn_by_finishing_one_of_many_rooms(self):
        for index in range(30):
            room = SlackDmMirrorConversation.objects.create(
                grant=self.grant, slack_workspace_id=self.grant.slack_workspace_id,
                slack_conversation_id=f'DLARGE{index}', status='live',
                latest_synced_ts=f'{int(timezone.now().timestamp())}.000001',
            )
            ensure_state(room)
        user = get_user_model().objects.create_user(email='small-import@example.invalid')
        connection = ExternalServiceConnection.objects.create(user=user, provider='slack', access_token='synthetic')
        grant = SlackDmMirrorGrant.objects.create(
            user=user, connection=connection, slack_workspace_id=self.grant.slack_workspace_id,
            slack_user_id='USMALL', consented_at=timezone.now(),
        )
        small = ensure_state(SlackDmMirrorConversation.objects.create(
            grant=grant, slack_workspace_id=grant.slack_workspace_id, slack_conversation_id='DSMALL', status='live',
        ))
        BridgeSyncJob.objects.update(last_served_at=None)
        first = claim_job(prefer_import=True)
        self.assertNotEqual(first.state_id, small.pk)
        # Only a future-due job now records the large owner's latest turn.
        finish_job(first, checkpoint={}, delay_seconds=3600, complete=True)
        second = claim_job(prefer_import=True)
        self.assertEqual(second.state_id, small.pk)
        finish_job(second, checkpoint={}, delay_seconds=3600, complete=True)
        third = claim_job(prefer_import=True)
        self.assertNotEqual(third.state_id, small.pk)

    def test_command_reports_observed_scope_counters_without_source_identifiers(self):
        BridgeApiBudget.objects.create(app_id='ATEST', workspace_id='TSECRET', method='conversations.history', next_admitted_at=timezone.now())
        scope = telemetry.scope_key('ATEST', 'TSECRET', 'conversations.history')
        with patch.object(telemetry, 'time', return_value=61):
            telemetry.record(scope, 'admitted', 20)
        output = StringIO()
        with patch.object(telemetry, 'time', return_value=120):
            call_command('message_sync_status', window_minutes=1, stdout=output)
        payload = output.getvalue()
        self.assertNotIn('TSECRET', payload)
        row = json.loads(payload)['provider_throughput']['measured_scopes'][0]
        self.assertEqual(row['requests_per_minute'], 20)
