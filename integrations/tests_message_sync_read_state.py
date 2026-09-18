"""Account fairness, lease fencing and cold-client unread cache regressions."""
from contextlib import contextmanager
from unittest.mock import patch, Mock

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.contrib.auth import get_user_model

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import ExternalServiceConnection, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm, slack_chat_read_state as reads
from integrations.services.message_sync import read_state as sync
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost


@override_settings(MESSAGE_SYNC_ENABLED=True)
class BackgroundReadStateTests(SlackDmIoAuthorityFixture, TransactionTestCase):
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
            mobile = reads.read_state_page(self.user, public_key=self.owner_key)
        self.assertEqual(source.call_count, 1)
        self.assertEqual(web, mobile)
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
