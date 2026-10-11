"""Database-free mapped-room count cutover and provider-budget regressions."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from integrations.services import slack_chat_read_state as reads
from integrations.services.message_sync import inbox_observations, relay_read_counts

ROOM = '00000000-0000-4000-8000-000000000001'


@override_settings(MESSAGE_SYNC_RELAY_READ_COUNTS=True)
class RelayReadCountsTests(SimpleTestCase):
    def target(self, *, public=False):
        return reads.ReadTarget(ROOM, 'Csynthetic', 'public_channel' if public else 'mpim',
                               bridge=SimpleNamespace(destination_channel_id=ROOM) if public else None,
                               conversation=None if public else SimpleNamespace(mlai_channel_id=ROOM),
                               source_activity_ts='102.000001')

    def context(self):
        return {'channel_id': ROOM, 'source_id': 'Csynthetic'}

    def refresh(self, target, *, context=True, fail_history=False):
        grant = SimpleNamespace(slack_user_id='UOWNER')
        connection = SimpleNamespace(sync_cursor={}, save=Mock())
        authority = SimpleNamespace()
        details = {'id': target.slack_id, 'is_member': True, 'last_read': '100.000001',
                   'latest': {'ts': '102.000001', 'text': 'private body'}, 'unread_count_display': 23}
        with patch.object(reads.transaction, 'atomic', side_effect=nullcontext), patch.object(
            reads, '_cache_key', return_value='synthetic-scoped-cache'), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads.cache, 'get', return_value=None), patch.object(reads.cache, 'delete'), patch.object(
            inbox_observations, 'capture', return_value=self.context() if context else None), patch.object(
            inbox_observations, 'observe_locked') as observe, patch.object(
            reads, '_call_slack_with_grant_authority', return_value={'channel': details}) as info, patch.object(
            reads, '_unread_messages', side_effect=reads.BudgetDeferred(40) if fail_history else None,
            return_value=([{'ts': '102.000001', 'user': 'UOTHER'}], 'slack_history', False)) as history, patch(
            'integrations.services.message_sync.read_snapshots.publish_snapshot', side_effect=lambda _, __, snapshot: snapshot):
            result = reads.refresh_target(grant, authority, target)
        return result, info, history, observe

    def test_private_and_public_mapped_rooms_need_only_one_metadata_request(self):
        for public in (False, True):
            with self.subTest(public=public):
                result, info, history, observe = self.refresh(self.target(public=public), fail_history=True)
                info.assert_called_once()
                self.assertEqual(info.call_args.args[1], 'conversations_info')
                history.assert_not_called()
                observe.assert_called_once()
                self.assertTrue(result['available'])
                self.assertTrue(result['is_unread'])
                self.assertEqual(result['last_read'], '100.000001')
                self.assertEqual(result['count_source'], 'relay')
                self.assertIsNone(result['unread_count'])
                self.assertIsNone(result['has_personal_mention'])
                self.assertNotIn('private body', str(result))

    def test_source_inventory_wrapped_as_conversation_keeps_history_counts(self):
        target = reads.ReadTarget('Gsource', 'Gsource', 'mpim',
                                 conversation=SimpleNamespace(source_activity_ts='102.000001'))
        result, _, history, _ = self.refresh(target)
        history.assert_called_once()
        self.assertEqual(result['count_source'], 'slack')
        self.assertEqual(result['unread_count'], 23)

    def test_missing_capability_context_keeps_history_and_retry_after_behavior(self):
        result, _, history, _ = self.refresh(self.target(), context=False)
        history.assert_called_once()
        self.assertNotEqual(result['count_source'], 'relay')
        with self.assertRaises(reads.BudgetDeferred) as failure:
            self.refresh(self.target(public=True), context=False, fail_history=True)
        self.assertEqual(failure.exception.read_state_method, 'conversations.history')

    @override_settings(MESSAGE_SYNC_RELAY_READ_COUNTS=False)
    def test_disabled_flag_keeps_existing_numeric_counts(self):
        result, _, history, _ = self.refresh(self.target())
        history.assert_called_once()
        self.assertEqual(result['unread_count'], 23)

    def test_foreign_mapping_or_context_never_claims_relay_count_authority(self):
        target = self.target()
        for context in (None, {}, {'channel_id': ROOM, 'source_id': 'Cforeign'},
                        {'channel_id': '00000000-0000-4000-8000-000000000002', 'source_id': target.slack_id}):
            self.assertFalse(relay_read_counts.enabled_for(target, context))
        target.channel_id = 'source-directory-id'
        self.assertFalse(relay_read_counts.enabled_for(target, self.context()))

    def test_metadata_hint_preserves_microseconds_without_fabricating_counts(self):
        result = reads.read_state_snapshot({'last_read': '100.000001',
            'latest': {'ts': '999.000001', 'subtype': 'channel_join'}}, kind='public_channel',
            messages=[], owner_id='UOWNER', cursor_only=True, source_activity_ts='100.000002')
        self.assertTrue(result['is_unread'])
        self.assertEqual(result['latest_ts'], '100.000002')
        self.assertIsNone(result['unread_count'])
        self.assertIsNone(reads.read_state_snapshot({'last_read': 'invalid'}, kind='im',
            messages=[], owner_id='UOWNER', cursor_only=True))
