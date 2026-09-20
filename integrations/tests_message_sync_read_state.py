"""Account fairness, lease fencing and cold-client unread cache regressions."""
from contextlib import contextmanager
from unittest.mock import patch, Mock

from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from django.contrib.auth import get_user_model

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm, slack_chat_read_state as reads
from integrations.services.message_sync import read_state as sync
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost
from integrations.services.message_sync import receipts


@override_settings(MESSAGE_SYNC_ENABLED=True)
class BackgroundReadStateTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        reads.cache.clear()

    def make_grant(self, index, workspace):
        user = get_user_model().objects.create_user(email=f'read-sync-{index}@example.invalid')
        connection = ExternalServiceConnection.objects.create(user=user, provider='slack', status='connected', access_token='synthetic')
        return SlackDmMirrorGrant.objects.create(user=user, connection=connection,
            slack_workspace_id=workspace, slack_user_id=f'U{index}', consented_at=timezone.now())

    def release_due(self):
        for c in ExternalServiceConnection.objects.all():
            if sync.KEY in (c.sync_cursor or {}):
                c.sync_cursor[sync.KEY]['due'] = 0
                c.save(update_fields=['sync_cursor'])

    def test_workspace_and_owner_rotation_with_no_client_sessions(self):
        busy = [self.make_grant(i, 'TIOAUTH') for i in range(3)]
        quiet = self.make_grant(99, 'TQUIET')
        seen = []
        for _ in range(9):
            lease = sync.claim_read_state()
            self.assertIsNotNone(lease)
            seen.append(lease.grant_id)
            sync.finish_read_state(lease, after='Dchecked')
            self.release_due()
        self.assertEqual(seen[:4], [self.grant.pk, quiet.pk, busy[0].pk, quiet.pk])
        self.assertEqual(set(seen), {self.grant.pk, quiet.pk, *(g.pk for g in busy)})

    def test_read_state_lease_starts_after_owner_lock(self):
        from datetime import timedelta
        started = timezone.now()
        claimed_at = started + timedelta(seconds=45)
        lock = sync._lock
        with patch.object(sync.timezone, 'now', return_value=started) as clock:
            def delayed_lock(*args, **kwargs):
                result = lock(*args, **kwargs)
                clock.return_value = claimed_at
                return result
            with patch.object(sync, '_lock', side_effect=delayed_lock):
                lease = sync.claim_read_state()
        self.assertIsNotNone(lease)
        self.connection.refresh_from_db()
        value = self.connection.sync_cursor[sync.KEY]
        self.assertEqual(value['served'], claimed_at.timestamp())
        self.assertEqual(value['expires'], claimed_at.timestamp() + 120)

    def test_expired_or_revoked_worker_cannot_make_source_call(self):
        lease = sync.claim_read_state()
        self.assertIsNone(sync.claim_read_state())
        self.connection.refresh_from_db()
        self.connection.sync_cursor[sync.KEY]['expires'] = 0
        self.connection.save(update_fields=['sync_cursor'])
        current = sync.claim_read_state()
        authority = dm._capture_slack_grant_api_authority(self.grant)
        with sync.read_state_context(lease), patch.object(dm, 'WebClient') as client:
            with self.assertRaises(LeaseLost):
                dm._call_slack_with_grant_authority(authority, 'conversations_info', required_scopes={'im:read'}, channel='D1')
            client.assert_not_called()
        with self.assertRaises(LeaseLost):
            sync.finish_read_state(lease, after='Dwrong')
        self.grant.status = 'revoked'
        self.grant.save(update_fields=['status'])
        with sync.read_state_context(current), patch.object(dm, 'WebClient') as client:
            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                dm._call_slack_with_grant_authority(authority, 'conversations_info', required_scopes={'im:read'}, channel='D1')
            client.assert_not_called()

    @contextmanager
    def source(self, callback):
        targets = [reads.ReadTarget('room-b', 'D2', 'im'), reads.ReadTarget('room-a', 'D1', 'im')]
        with patch.object(reads, '_targets_for_keys', return_value=targets), patch.object(
            sync.CommunityChatDevice.objects, 'filter'
        ) as devices, patch.object(sync.cache, 'get_many', return_value={}), patch.object(
            reads, 'refresh_target', side_effect=callback
        ):
            devices.return_value.values_list.return_value = ['a'*64]
            yield

    def test_source_id_cursor_survives_restart_and_directory_reordering(self):
        seen = []
        with self.source(lambda grant, authority, target: seen.append(target.slack_id)):
            self.assertEqual(sync.refresh_read_state_once(), 1)
            self.release_due()
            self.assertEqual(sync.refresh_read_state_once(), 1)
        self.assertEqual(seen, ['D1', 'D2'])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[sync.KEY]['after'], 'D2')

    def test_budget_deferral_keeps_exact_target_and_other_owners_get_a_turn(self):
        other = self.make_grant(100, 'TIOAUTH')
        with self.source(Mock(side_effect=BudgetDeferred(120, before_request_method='conversations.info'))):
            self.assertEqual(sync.refresh_read_state_once(), 0)
        self.connection.refresh_from_db()
        value = self.connection.sync_cursor[sync.KEY]
        self.assertEqual(value['after'], '')
        self.assertNotIn('served', value)
        self.assertGreater(value['due'], timezone.now().timestamp()+110)
        self.assertEqual(sync.claim_read_state().grant_id, other.pk)

    def test_failing_target_does_not_block_the_rest_or_erase_existing_cursor(self):
        with self.source(Mock(side_effect=TimeoutError)):
            self.assertEqual(sync.refresh_read_state_once(), 0)
        self.release_due()
        seen=[]
        with self.source(lambda grant, authority, target: seen.append(target.slack_id)):
            self.assertEqual(sync.refresh_read_state_once(), 1)
        self.assertEqual(seen, ['D2'])

    def test_no_devices_do_not_create_public_read_targets(self):
        self.assertEqual(reads._targets_for_keys(self.grant, set()), [])

    def test_scope_failure_does_not_return_or_store_cached_state(self):
        lease = sync.claim_read_state()
        self.grant.status = 'paused'
        self.grant.save(update_fields=['status'])
        authority = dm._capture_slack_grant_api_authority(self.grant)
        with sync.read_state_context(lease), patch.object(reads.cache, 'get') as cached:
            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                reads.refresh_target(self.grant, authority, reads.ReadTarget('room', 'D1', 'im'))
            cached.assert_not_called()

    def test_worker_populates_the_same_badge_for_cold_clients_without_source_reads(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', return_value={'channel': {
                'id': 'D1', 'last_read': '100.000001', 'latest': {'ts': '101.000001'},
                'unread_count_display': 3,
            }}
        ) as source:
            self.assertEqual(sync.refresh_read_state_once(), 1)
            web = reads.read_state_page(self.user, public_key=self.owner_key)
            from community_chat.models import CommunityChatDevice
            CommunityChatDevice.objects.create(user=self.user, public_key='3'*64, status='verified', verified_at=timezone.now())
            mobile = reads.read_state_page(self.user, public_key='3'*64)
        self.assertEqual(source.call_count, 1)
        self.assertEqual(web['channels'], mobile['channels'])
        self.assertEqual(web['authorized_channel_ids'], mobile['authorized_channel_ids'])
        self.assertEqual(web['channels']['room']['unread_count'], 3)

    def test_source_response_after_lease_expiry_is_not_published(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        def expire(*args, **kwargs):
            c=ExternalServiceConnection.objects.get(pk=self.connection.pk)
            c.sync_cursor[sync.KEY]['expires'] = 0
            c.save(update_fields=['sync_cursor'])
            return {'channel': {'id': 'D1', 'last_read': '100.000001', 'unread_count_display': 3}}
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', side_effect=expire
        ), patch.object(reads.cache, 'set') as stored:
            self.assertEqual(sync.refresh_read_state_once(), 0)
        stored.assert_not_called()

    def test_rate_limited_read_is_durable_and_worker_confirms_for_every_device(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', side_effect=BudgetDeferred(12)
        ):
            pending = reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
        self.assertFalse(pending['synced'])
        self.assertTrue(pending['pending'])
        self.connection.refresh_from_db()
        self.assertEqual(next(iter(self.connection.sync_cursor[receipts.KEY].values()))['source_ts'], '102.000001')
        # A fresh worker runs with no client, verifies the device again and
        # publishes its source-confirmed result to the shared account cache.
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', return_value={'channel': {
                'id': 'D1', 'last_read': '100.000001', 'latest': {'ts': '102.000001'},
                'unread_count_display': 2,
            }}
        ) as source:
            self.assertEqual(sync.refresh_read_state_once(), 1)
            web = reads.read_state_page(self.user, public_key=self.owner_key)
            ios = reads.read_state_page(self.user, public_key=self.owner_key)
        self.assertEqual([c.args[1] for c in source.call_args_list], ['conversations_info', 'conversations_mark'])
        self.assertEqual(web['channels'], ios['channels'])
        self.assertEqual(web['authorized_channel_ids'], ios['authorized_channel_ids'])
        self.assertFalse(web['channels']['room']['is_unread'])
        self.assertEqual(web['channels']['room']['last_read'], '102.000001')
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[receipts.KEY], {})

    def test_partial_confirmed_read_does_not_invent_remaining_count(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', return_value={'channel': {
                'id': 'D1', 'last_read': '100.000001', 'latest': {'ts': '104.000001', 'user': 'UOTHER'},
                'unread_count_display': 4,
            }}
        ):
            result = reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
        self.assertTrue(result['synced'])
        self.assertTrue(result['channels']['room']['is_unread'])
        self.assertIsNone(result['channels']['room']['unread_count'])

    def test_own_post_or_thread_reply_is_not_proof_of_unread_after_confirmation(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        for latest in ({'ts': '104.000001', 'user': 'UOWNER'},
                       {'ts': '104.000001', 'user': 'UOTHER', 'thread_ts': '99.000001'}):
            with self.subTest(latest=latest), patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
                reads, '_call_slack_with_grant_authority', return_value={'channel': {
                    'id': 'D1', 'last_read': '100.000001', 'latest': latest, 'unread_count_display': 1,
                }}
            ):
                result = reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
            snapshot = result['channels']['room']
            self.assertFalse(snapshot['available'])
            self.assertFalse(snapshot['is_unread'])
            self.assertIsNone(snapshot['unread_count'])
            self.assertTrue(snapshot['refresh_required'])

    def test_newer_intent_survives_older_confirmation_and_queue_preserves_other_cursors(self):
        authority = dm._capture_slack_grant_api_authority(self.grant)
        target = reads.ReadTarget('room', 'D1', 'im')
        self.connection.sync_cursor = {'discovery': {'cursor': 'keep'}}
        self.connection.save(update_fields=['sync_cursor'])
        for ts in ('102.000001', '105.000001', '101.000001'):
            receipts.enqueue_read(authority, target, public_key=self.owner_key, source_ts=ts)
        receipts.complete_read(authority, target, source_ts='102.000001')
        self.connection.refresh_from_db()
        self.assertEqual(next(iter(self.connection.sync_cursor[receipts.KEY].values()))['source_ts'], '105.000001')
        self.assertEqual(self.connection.sync_cursor['discovery'], {'cursor': 'keep'})

    def test_lower_device_frontier_survives_revocation_of_higher_device_intent(self):
        from community_chat.models import CommunityChatDevice
        authority = dm._capture_slack_grant_api_authority(self.grant)
        target = reads.ReadTarget('room', 'D1', 'im')
        old = CommunityChatDevice.objects.create(user=self.user, public_key='2'*64,
            status='verified', verified_at=timezone.now())
        receipts.enqueue_read(authority, target, public_key=old.public_key, source_ts='105.000001')
        receipts.enqueue_read(authority, target, public_key=self.owner_key, source_ts='102.000001')
        old.status = 'revoked'
        old.revoked_at = timezone.now()
        old.save(update_fields=['status', 'revoked_at'])
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, 'apply_read', return_value={'synced': True, 'last_read': '102.000001'}
        ) as apply:
            self.assertEqual(receipts.flush_read_once(self.grant, authority, {self.owner_key}), 0)
            apply.assert_not_called()
            self.assertEqual(receipts.flush_read_once(self.grant, authority, {self.owner_key}), 1)
        self.assertEqual(apply.call_args.kwargs['source_ts'], '102.000001')

    def test_reenrolled_same_key_cannot_replay_previous_device_generation(self):
        from community_chat.models import CommunityChatDevice
        authority = dm._capture_slack_grant_api_authority(self.grant)
        target = reads.ReadTarget('room', 'D1', 'im')
        receipts.enqueue_read(authority, target, public_key=self.owner_key, source_ts='102.000001')
        CommunityChatDevice.objects.filter(user=self.user).update(status='revoked', revoked_at=timezone.now())
        CommunityChatDevice.objects.create(user=self.user, public_key=self.owner_key,
            status='verified', verified_at=timezone.now())
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(reads, 'apply_read') as apply:
            self.assertEqual(receipts.flush_read_once(self.grant, authority, {self.owner_key}), 0)
            apply.assert_not_called()

    def test_broken_receipt_cannot_starve_other_unread_conversations(self):
        authority = dm._capture_slack_grant_api_authority(self.grant)
        target = reads.ReadTarget('room', 'D1', 'im')
        other = reads.ReadTarget('other', 'D2', 'im')
        receipts.enqueue_read(authority, target, public_key=self.owner_key, source_ts='102.000001')
        with patch.object(reads, '_targets_for_keys', return_value=[target, other]), patch.object(
            reads, 'apply_read', side_effect=RuntimeError('permanent source failure')
        ), patch.object(reads, 'refresh_target') as refresh:
            self.assertEqual(sync.refresh_read_state_once(), 1)
            self.release_due()
            self.assertEqual(sync.refresh_read_state_once(), 1)
        self.assertEqual([c.args[2].slack_id for c in refresh.call_args_list], ['D1', 'D2'])

    def test_restart_after_source_confirmation_does_not_repeat_or_regress_mark(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        source_cursor = ['100.000001']
        calls = []
        def source(authority, method, **kwargs):
            calls.append(method)
            if method == 'conversations_mark':
                source_cursor[0] = kwargs['ts']
                return {'ok': True}
            return {'channel': {'id': 'D1', 'last_read': source_cursor[0],
                                'latest': {'ts': '102.000001'}, 'unread_count_display': 0}}
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', side_effect=source
        ):
            with patch.object(receipts, 'complete_read', side_effect=RuntimeError('restart')):
                with self.assertRaises(RuntimeError):
                    reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
            self.assertEqual(sync.refresh_read_state_once(), 1)
        self.assertEqual(calls, ['conversations_info', 'conversations_mark', 'conversations_info'])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[receipts.KEY], {})

    def test_foreground_read_revalidates_device_under_consent_lock(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            dm, '_locked_active_verified_device', return_value=None
        ), patch.object(reads, '_call_slack_with_grant_authority') as source:
            with self.assertRaises(dm.SlackDmMirrorError):
                reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
            source.assert_not_called()

    def test_confirmed_read_ignores_newer_hidden_control_frontier(self):
        target = reads.ReadTarget('room', 'D1', 'im')
        authority = dm._capture_slack_grant_api_authority(self.grant)
        reads.cache.set(reads._cache_key(authority, target), {'latest_ts': '102.000001'})
        with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(
            reads, '_call_slack_with_grant_authority', return_value={'channel': {
                'id': 'D1', 'last_read': '100.000001',
                'latest': {'ts': '105.000001', 'subtype': 'channel_join'},
            }}
        ):
            result = reads.mark_read(self.user, public_key=self.owner_key, channel_id='room', source_ts='102.000001')
        self.assertFalse(result['channels']['room']['is_unread'])

    def test_revoked_device_or_old_consent_cannot_replay_queued_read(self):
        authority = dm._capture_slack_grant_api_authority(self.grant)
        target = reads.ReadTarget('room', 'D1', 'im')
        for device_keys in (set(), {self.owner_key}):
            receipts.enqueue_read(authority, target, public_key=self.owner_key, source_ts='102.000001')
            if device_keys:
                self.connection.refresh_from_db()
                next(iter(self.connection.sync_cursor[receipts.KEY].values()))['authority'] = 'old-generation'
                self.connection.save(update_fields=['sync_cursor'])
            with patch.object(reads, '_targets_for_keys', return_value=[target]), patch.object(reads, 'apply_read') as source:
                self.assertEqual(receipts.flush_read_once(self.grant, authority, device_keys), 0)
                source.assert_not_called()
            self.connection.refresh_from_db()
            self.assertEqual(self.connection.sync_cursor[receipts.KEY], {})

    def test_history_scheduler_rotates_accounts_before_conversation_volume(self):
        from integrations.models import SlackDmMirrorConversation
        from integrations.services.message_sync.history import ensure_state
        from integrations.services.message_sync.scheduler import claim_job, finish_job
        small = self.make_grant(200, 'TIOAUTH')
        owners = {}
        for index, grant in enumerate([self.grant] * 6 + [small]):
            conversation = SlackDmMirrorConversation.objects.create(
                grant=grant, slack_workspace_id='TIOAUTH', slack_conversation_id=f'DFAIR{index}',
                participant_hash=f'{index:064x}', status='live',
            )
            state = ensure_state(conversation)
            owners[state.pk] = grant.pk
        observed = []
        for _ in range(4):
            lease = claim_job(kinds=['head'])
            observed.append(owners[lease.state_id])
            finish_job(lease, checkpoint={}, delay_seconds=0, complete=True)
        self.assertEqual(observed, [self.grant.pk, small.pk, self.grant.pk, small.pk])

    def test_history_claim_records_service_time_after_arbitration(self):
        from datetime import timedelta
        from integrations.models import SlackDmMirrorConversation, BridgeSyncJob
        from integrations.services.message_sync.history import ensure_state
        from integrations.services.message_sync.scheduler import claim_job
        conversation = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id='TIOAUTH', slack_conversation_id='DCLAIMCLOCK',
            participant_hash='c' * 64, status='live',
        )
        ensure_state(conversation)
        started = timezone.now()
        claimed_at = started + timedelta(seconds=45)
        with patch('integrations.services.message_sync.scheduler.timezone.now', side_effect=[started, claimed_at]):
            lease = claim_job(kinds=['head'], lease_seconds=120)
        job = BridgeSyncJob.objects.get(pk=lease.job_id)
        self.assertEqual(job.last_served_at, claimed_at)
        self.assertEqual(job.lease_expires_at, claimed_at + timedelta(seconds=120))

    @skipUnlessDBFeature('has_select_for_update')
    def test_parallel_history_claims_preserve_owner_turns(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        from integrations.models import SlackDmMirrorConversation, BridgeSyncJob
        from integrations.services.message_sync.history import ensure_state
        from integrations.services.message_sync.scheduler import claim_job
        small = self.make_grant(300, 'TIOAUTH')
        owners = {}
        for index, grant in enumerate([self.grant] * 5 + [small]):
            conversation = SlackDmMirrorConversation.objects.create(
                grant=grant, slack_workspace_id='TIOAUTH', slack_conversation_id=f'DPARALLEL{index}',
                participant_hash=f'{index:064x}', status='live')
            owners[ensure_state(conversation).pk] = grant.pk
        barrier = Barrier(4)
        def claim(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return claim_job(kinds=['head'])
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = [lease for lease in pool.map(claim, range(4)) if lease is not None]
        while len(claimed) < 4:
            claimed.append(claim_job(kinds=['head']))
        jobs = BridgeSyncJob.objects.filter(pk__in=[lease.job_id for lease in claimed]).order_by('last_served_at')
        observed = [owners[job.state_id] for job in jobs]
        self.assertEqual(observed[:2], [self.grant.pk, small.pk])
