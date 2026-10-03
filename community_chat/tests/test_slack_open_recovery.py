"""Exact dry-run recovery cannot bypass the established owner-open authority."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.db.backends.postgresql.base import DatabaseWrapper

from integrations.models import SlackDmMirrorConversation
from integrations.services import slack_open_recovery as recovery
from integrations.services.message_sync import device_recovery, private_coverage
from integrations.services.slack_owner_inventory_api import InventoryError


class SlackOpenRecoveryTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk=2, scopes={'im:read', 'im:history'}, save=MagicMock())
        self.grant = SimpleNamespace(
            pk=7, user_id=3, user=object(), connection_id=2, connection=self.connection,
            owner_conversation_inventory=MagicMock(), save=MagicMock(),
        )
        self.device = SimpleNamespace(pk=4, public_key='a' * 64, verified_at=datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.authority = object()
        self.row = SimpleNamespace(
            slack_conversation_id='DUNFINISHED', kind='im', eligibility='eligible',
            source_archived=False, source_is_open=None,
        )
        self.conversation = SimpleNamespace(pk=9, status='provisioning', mlai_channel_id=None, participant_hash='old-boundary')
        self.grant.owner_conversation_inventory.filter.return_value.first.return_value = self.row
        patches = {
            'transaction.atomic': {'side_effect': lambda: nullcontext()},
            'SlackDmMirrorGrant.objects': {},
            'SlackDmMirrorConversation.objects': {},
            'CommunityChatDevice.objects': {},
            '_authorized': {'return_value': (self.grant, self.authority, self.device, {})},
            'dm._lock_slack_grant_api_authority': {'return_value': (self.grant, self.connection)},
            'dm._grant_history_days': {'return_value': 30},
            'dm._history_required_scopes': {'return_value': {'im:read', 'im:history'}},
            'has_metadata_consent': {'return_value': True},
            'state_for': {'return_value': {}},
            'device_epoch': {'return_value': 'current-device-consent-epoch'},
            'enqueue_open_locked': {'return_value': {'state': 'importing', 'retry_after_seconds': 2}},
        }
        self.mocks = {}
        for name, options in patches.items():
            patcher = patch(f'integrations.services.slack_open_recovery.{name}', **options)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.mocks['SlackDmMirrorGrant.objects'].select_related.return_value.filter.return_value.first.return_value = self.grant
        self.devices = self.mocks['CommunityChatDevice.objects']
        self.devices.filter.return_value.first.return_value = self.device
        self.devices.select_for_update.return_value.filter.return_value.first.return_value = self.device
        self.mirrors = self.mocks['SlackDmMirrorConversation.objects']
        self.mirrors.select_for_update.return_value.filter.return_value.first.return_value = self.conversation

    def run_recovery(self, **kwargs):
        return recovery.recover_existing_conversation(grant_id=7, device_id=4, source_id='DUNFINISHED', **kwargs)

    def test_default_is_content_free_dry_run_without_writes_or_queue(self):
        result = self.run_recovery()
        self.assertFalse(result['apply'])
        self.assertFalse(result['enqueued'])
        self.assertEqual((result['grant_id'], result['device_id'], result['conversation_id']), (7, 4, 9))
        self.assertEqual(len(result['plan']), 64)
        self.assertNotIn(self.device.public_key, str(result))
        self.assertNotIn('old-boundary', str(result))
        self.assertNotIn('current-device-consent-epoch', str(result))
        self.grant.save.assert_not_called()
        self.connection.save.assert_not_called()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_apply_uses_same_existing_owner_open_queue(self):
        plan = self.run_recovery()['plan']
        result = self.run_recovery(apply=True, expected_plan=plan)
        self.assertTrue(result['enqueued'])
        self.mocks['enqueue_open_locked'].assert_called_once_with(
            self.grant, self.connection, self.authority, self.device, self.row,
        )
        self.assertEqual(self.mirrors.select_for_update.return_value.filter.call_args.kwargs,
                         {'grant_id': 7, 'slack_conversation_id': 'DUNFINISHED'})

    def test_apply_requires_matching_reviewed_plan(self):
        for plan in [None, 'old-plan']:
            with self.subTest(plan=plan), self.assertRaisesRegex(InventoryError, 'recovery_plan_changed'):
                self.run_recovery(apply=True, expected_plan=plan)
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_authority_or_mirror_changes_invalidate_review(self):
        plan = self.run_recovery()['plan']
        self.mocks['device_epoch'].return_value = 'new-consent'
        with self.assertRaisesRegex(InventoryError, 'recovery_plan_changed'):
            self.run_recovery(apply=True, expected_plan=plan)
        self.mocks['device_epoch'].return_value = 'current-device-consent-epoch'
        self.conversation.pk = 11
        with self.assertRaisesRegex(InventoryError, 'recovery_plan_changed'):
            self.run_recovery(apply=True, expected_plan=plan)
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_other_grant_cannot_receive_the_requested_recovery(self):
        self.mocks['_authorized'].return_value = (SimpleNamespace(pk=8), self.authority, self.device, {})
        with self.assertRaisesRegex(InventoryError, 'slack_authority_changed'):
            self.run_recovery()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_device_revoke_between_lookup_and_lock_blocks_enqueue(self):
        self.devices.select_for_update.return_value.filter.return_value.first.return_value = None
        with self.assertRaisesRegex(InventoryError, 'device_unverified'):
            self.run_recovery()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_already_provisioned_room_cannot_get_another_provisioning_intent(self):
        self.conversation.mlai_channel_id = 'existing-room'
        with self.assertRaisesRegex(InventoryError, 'conversation_not_unprovisioned'):
            self.run_recovery()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_paused_mirror_or_ineligible_source_is_not_reactivated(self):
        for status, eligibility in [('paused', 'eligible'), ('provisioning', 'shared')]:
            with self.subTest(status=status, eligibility=eligibility):
                self.conversation.status, self.row.eligibility = status, eligibility
                with self.assertRaises(InventoryError):
                    self.run_recovery()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_missing_history_scope_is_not_granted_by_recovery(self):
        self.connection.scopes = {'im:read'}
        with self.assertRaisesRegex(InventoryError, 'slack_authority_changed'):
            self.run_recovery()
        self.mocks['enqueue_open_locked'].assert_not_called()

    def test_command_requires_review_before_applying_and_defaults_to_dry_run(self):
        output = StringIO()
        call_command('recover_slack_conversation_open', grant_id=7, device_id=4, source_id='DUNFINISHED', stdout=output)
        self.assertIn('"apply": false', output.getvalue())
        with self.assertRaisesRegex(CommandError, '--expected-plan'):
            call_command('recover_slack_conversation_open', grant_id=7, device_id=4, source_id='DUNFINISHED', apply=True)
        self.mocks['enqueue_open_locked'].assert_not_called()


@override_settings(MESSAGE_SYNC_ENABLED=True)
class SourceActivityRecoveryWakeTests(SimpleTestCase):
    def setUp(self):
        self.cursor = {'message_sync_discovery': {'token': 'lease', 'served': 5, 'due': 99, 'expires': 80}}
        self.connection = SimpleNamespace(pk=2, sync_cursor=deepcopy(self.cursor), save=MagicMock())
        self.grant = SimpleNamespace(pk=7, connection_id=2, status='active', revoked_at=None,
                                     last_discovery_at=object(), save=MagicMock())

    def test_new_source_activity_wakes_only_existing_unfinished_mirror_and_keeps_backoff(self):
        with patch.object(device_recovery.SlackDmMirrorConversation, 'objects') as rows:
            rows.filter.return_value.exists.return_value = True
            self.assertTrue(device_recovery.schedule_source_recovery_locked(self.grant, self.connection, 'DUNFINISHED'))
        self.assertEqual(rows.filter.call_args.kwargs, {
            'grant_id': 7, 'slack_conversation_id': 'DUNFINISHED',
            'status__in': ['provisioning', 'error'], 'mlai_channel_id__isnull': True,
        })
        self.assertIsNone(self.grant.last_discovery_at)
        self.assertEqual(self.connection.sync_cursor, self.cursor)
        self.connection.save.assert_not_called()

    def test_coalesces_wake_while_discovery_is_already_pending(self):
        self.grant.last_discovery_at = None
        with patch.object(device_recovery.SlackDmMirrorConversation, 'objects') as rows:
            self.assertFalse(device_recovery.schedule_source_recovery_locked(self.grant, self.connection, 'DUNFINISHED'))
        rows.filter.assert_not_called()
        self.grant.save.assert_not_called()

    def test_inventory_hint_is_correlated_to_exact_owner_source_and_is_recovery_only(self):
        # Compile without opening a connection. The database/network-free test
        # runner rejects execution; this verifies the actual query boundary.
        database = DatabaseWrapper({'NAME': 'never-opened'}, alias='source-recovery-compile')
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        with patch.object(private_coverage, 'connections', {'default': database}):
            ordinary = private_coverage.recent_conversations(SlackDmMirrorConversation.objects.all(), now=now)
            recovery_query = private_coverage.recent_conversations(
                SlackDmMirrorConversation.objects.all(), now=now, include_owner_inventory=True,
            )
            ordinary_sql, _ = ordinary.query.get_compiler(connection=database).as_sql()
            sql, params = recovery_query.query.get_compiler(connection=database).as_sql()
        self.assertNotIn('slack_owner_conversation_inventory', ordinary_sql)
        self.assertIn('slack_owner_conversation_inventory', sql)
        self.assertIn('U0."grant_id" = ("slack_dm_mirror_conversation"."grant_id")', sql)
        self.assertIn('U0."slack_conversation_id" = ("slack_dm_mirror_conversation"."slack_conversation_id")', sql)
        self.assertIn('eligible', params)
        self.assertIn(int(now.timestamp()) + 300, params)
        self.assertIn(r'^\d{10}(\.\d{1,6})?$', params)
        self.assertIsNone(database.connection)

    def test_recovery_hint_cannot_bypass_current_slack_source_validation(self):
        candidate = SimpleNamespace(
            grant=SimpleNamespace(connection=SimpleNamespace(provider_metadata={}), consent_version='current'),
            slack_conversation_id='DUNFINISHED', coverage_activity=1790917036,
        )
        with (
            patch.object(recovery.dm, '_call_slack_with_grant_authority',
                         return_value={'channel': {'id': 'DOTHER', 'is_im': True}}),
            patch.object(recovery.dm, '_discover_conversation') as discover,
            self.assertRaises(recovery.dm.SlackDmMirrorUpstreamError),
        ):
            device_recovery._recover(candidate, object(), {}, datetime(2026, 10, 3, tzinfo=timezone.utc))
        discover.assert_not_called()

    def test_source_inventory_alone_cannot_create_a_new_mirror(self):
        with patch.object(device_recovery.SlackDmMirrorConversation, 'objects') as rows:
            rows.filter.return_value.exists.return_value = False
            self.assertFalse(device_recovery.schedule_source_recovery_locked(self.grant, self.connection, 'DUNSEEN'))
        self.grant.save.assert_not_called()

    def test_changed_authority_and_disabled_sync_do_not_schedule(self):
        for field, value in [('status', 'paused'), ('revoked_at', object()), ('connection_id', 10)]:
            previous = getattr(self.grant, field)
            setattr(self.grant, field, value)
            with patch.object(device_recovery.SlackDmMirrorConversation, 'objects') as rows:
                self.assertFalse(device_recovery.schedule_source_recovery_locked(self.grant, self.connection, 'DUNFINISHED'))
            rows.filter.assert_not_called()
            setattr(self.grant, field, previous)
        with override_settings(MESSAGE_SYNC_ENABLED=False):
            self.assertFalse(device_recovery.schedule_source_recovery_locked(self.grant, self.connection, 'DUNFINISHED'))
        self.grant.save.assert_not_called()
