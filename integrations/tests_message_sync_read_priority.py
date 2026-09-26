"""Priority, version ordering and durable notification failures."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.services import slack_chat_read_state as reads, slack_owner_inventory
from integrations.services.message_sync import read_priority as priority, read_snapshots, read_state


class SelectionTests(SimpleTestCase):
    def test_priority_is_bounded_and_quiet_conversation_cannot_starve(self):
        hot = reads.ReadTarget('hot', 'D1', 'im')
        unread = reads.ReadTarget('unread', 'D2', 'im')
        quiet = reads.ReadTarget('quiet', 'D3', 'im')
        values = {'D1': {'fetched_at': 900}, 'D2': {'is_unread': True, 'fetched_at': 800}, 'D3': {'fetched_at': 100}}
        cursor = {priority.KEY: {'D1': {'until': 1100}}}
        for turn, expected in ((0, hot), (1, hot), (2, hot), (3, quiet)):
            chosen = priority.select_target([hot, unread, quiet], values, lambda t: t.slack_id,
                                            cursor, now=1000, turn=turn)
            self.assertEqual(chosen, expected)

    def test_fresh_or_expired_visible_hint_yields_to_known_unread(self):
        hot = reads.ReadTarget('hot', 'D1', 'im')
        unread = reads.ReadTarget('unread', 'D2', 'im')
        values = {'D1': {'fetched_at': 990}, 'D2': {'is_unread': True, 'fetched_at': 800}}
        cursor = {priority.KEY: {'D1': {'until': 1100}}}
        self.assertEqual(priority.select_target([hot, unread], values, lambda t: t.slack_id,
                         cursor, now=1000, turn=0), unread)
        values['D1']['fetched_at'] = 700
        self.assertEqual(priority.select_target([hot, unread], values, lambda t: t.slack_id,
                         cursor, now=1101, turn=0), unread)

    def test_failing_room_cools_down_without_blocking_another_room(self):
        targets = [reads.ReadTarget('room1', 'D1', 'im'), reads.ReadTarget('room2', 'D2', 'im')]
        chosen = priority.select_target(targets, {}, lambda t: t.slack_id,
            {read_state.KEY: {'retries': {'D1': 2000}}}, now=1000, turn=0)
        self.assertEqual(chosen.slack_id, 'D2')

    def test_recent_unknown_gets_one_priority_turn_without_starving_unread_or_old_rooms(self):
        now = 1_800_000_000
        old = reads.ReadTarget('old', 'D1', 'im', source_activity_ts=str(now - 60 * 86400))
        recent = reads.ReadTarget('recent', 'D2', 'im', source_activity_ts=str(now - 86400))
        unread = reads.ReadTarget('unread', 'D3', 'im')
        values = {'D3': {'available': True, 'is_unread': True, 'fetched_at': now - 120}}
        targets = [old, recent, unread]
        for turn, expected in ((0, unread), (1, unread), (2, recent), (3, old)):
            self.assertEqual(priority.select_target(targets, values, lambda t: t.slack_id,
                             {}, now=now, turn=turn), expected)
        # A source observation, even a read one, removes the recent-unknown priority.
        values['D2'] = {'available': True, 'is_unread': False, 'fetched_at': now}
        self.assertEqual(priority.select_target(targets, values, lambda t: t.slack_id,
                         {}, now=now, turn=2), unread)

    def test_recent_unknown_priority_requires_valid_activity_and_source_authority(self):
        now = 1_800_000_000
        recent = reads.ReadTarget('recent', 'D1', 'im', source_activity_ts=str(now - 10))
        malformed = reads.ReadTarget('malformed', 'D2', 'im', source_activity_ts='nan')
        future = reads.ReadTarget('future', 'D3', 'im', source_activity_ts=str(now + 301))
        excluded = reads.ReadTarget('excluded', 'D4', 'im', source_activity_ts=str(now - 10))
        values = {'D4': {'available': False, 'excluded': True, 'fetched_at': now - 120}}
        for target in (malformed, future, excluded):
            self.assertEqual(priority.select_target([target, recent], values, lambda t: t.slack_id,
                             {}, now=now, turn=2), recent)
        self.assertEqual(priority.select_target([recent, malformed], values, lambda t: t.slack_id,
                         {read_state.KEY: {'retries': {'D1': now + 60}}}, now=now, turn=2), malformed)

    def test_source_activity_only_enters_consented_scope_eligible_targets(self):
        rows = [
            SimpleNamespace(slack_conversation_id='D1', kind='im', source_activity_ts='1800000000'),
            SimpleNamespace(slack_conversation_id='G1', kind='private_channel', source_activity_ts='1800000000'),
        ]
        directory = SimpleNamespace(filter=lambda **_kwargs: SimpleNamespace(order_by=lambda *_fields: rows))
        grant = SimpleNamespace(connection=object(), owner_conversation_inventory=directory)
        authority = SimpleNamespace(scopes={'im:read'})
        with patch.object(slack_owner_inventory, 'enabled', return_value=True), patch.object(
            slack_owner_inventory, 'has_metadata_consent', return_value=True
        ):
            targets = slack_owner_inventory.source_read_targets(grant, authority, [])
        self.assertEqual([(target.slack_id, target.source_activity_ts) for target in targets],
                         [('D1', '1800000000')])
        with patch.object(slack_owner_inventory, 'enabled', return_value=True), patch.object(
            slack_owner_inventory, 'has_metadata_consent', return_value=False
        ):
            self.assertEqual(slack_owner_inventory.source_read_targets(grant, authority, []), [])


@override_settings(MESSAGE_SYNC_ENABLED=True)
class SnapshotTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        reads.cache.clear()
        self.target = reads.ReadTarget('room', 'D1', 'im')
        self.authority = reads._capture_slack_grant_api_authority(self.grant)

    def test_visible_refresh_is_authorized_deduplicated_and_does_not_call_slack(self):
        with patch.object(reads, '_targets_for_keys', return_value=[self.target]), patch.object(reads, '_call_slack_with_grant_authority') as source:
            reads.read_state_page(self.user, public_key=self.owner_key, channel_ids=['room', 'not-allowed'])
            reads.read_state_page(self.user, public_key=self.owner_key, channel_ids=['room'])
        source.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(set(self.connection.sync_cursor[priority.KEY]), {'D1'})

    def test_event_hint_is_scoped_to_explicit_authorized_owner(self):
        for owner, expected in [('UOTHER', False), ('UOWNER', True)]:
            priority.invalidate_event({'team_id': 'TIOAUTH', 'authorizations': [{'user_id': owner}],
                'event': {'type': 'message', 'channel': 'D1', 'text': 'must never be retained'}})
            self.connection.refresh_from_db()
            self.assertEqual(bool(self.connection.sync_cursor.get(priority.KEY)), expected)
        self.assertNotIn('must never be retained', str(self.connection.sync_cursor))

    def test_directory_revision_increases_when_clock_moves_backwards(self):
        with patch.object(reads, '_targets_for_keys', return_value=[self.target]), patch.object(reads.time, 'time_ns', return_value=2000):
            first = reads.read_state_page(self.user, public_key=self.owner_key)
        with patch.object(reads, '_targets_for_keys', return_value=[]), patch.object(reads.time, 'time_ns', return_value=1000):
            second = reads.read_state_page(self.user, public_key=self.owner_key)
        self.assertGreater(second['directory_revision'], first['directory_revision'])
        self.assertEqual(second['authorized_channel_ids'], [])

    def test_group_history_quota_does_not_block_independent_dm_read_refresh(self):
        group = reads.ReadTarget('group', 'G1', 'mpim')
        dm = reads.ReadTarget('direct', 'D1', 'im')
        reads.cache.set(reads._cache_key(self.authority, dm), {'fetched_at': 100})
        calls = []
        def refresh(_grant, _authority, target):
            calls.append(target.slack_id)
            if target.slack_id == 'G1':
                from integrations.services.message_sync.scheduler import BudgetDeferred
                # An actual provider 429 carries the stage from refresh_target
                # but is not a pre-request admission deferral.
                error = BudgetDeferred(30)
                error.read_state_method = 'conversations.history'
                raise error
            return {'available': True}
        with patch.object(reads, '_targets_for_keys', return_value=[dm, group]), patch.object(reads, 'refresh_target', side_effect=refresh):
            self.assertEqual(read_state.refresh_read_state_once(), 0)
            self.connection.refresh_from_db()
            self.connection.sync_cursor[read_state.KEY]['due'] = 0
            self.connection.save(update_fields=['sync_cursor'])
            self.assertEqual(read_state.refresh_read_state_once(), 1)
        self.assertEqual(calls, ['G1', 'D1'])

    def test_newer_revision_survives_clock_regression_and_notification_failure(self):
        key = reads._cache_key(self.authority, self.target)
        with patch.object(reads, '_call_slack_with_grant_authority', return_value={
            'channel': {'id': 'D1', 'last_read': '100.000001', 'latest': {'ts': '101.000001'}, 'unread_count_display': 3}
        }), patch.object(read_snapshots.time, 'time_ns', return_value=1000):
            first = reads.refresh_target(self.grant, self.authority, self.target)
            second = reads.refresh_target(self.grant, self.authority, self.target)
        self.assertGreater(second['revision'], first['revision'])
        self.connection.refresh_from_db()
        pending = self.connection.sync_cursor[read_snapshots.KEY]['pending']
        with patch('integrations.services.community_bridge.buzz.BuzzBridgeClient.notify_read_state', side_effect=TimeoutError):
            read_snapshots.flush_notification(self.authority)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[read_snapshots.KEY]['pending'], pending)
        self.assertEqual(reads.cache.get(key), second)
        self.connection.sync_cursor[read_snapshots.KEY]['due'] = 0
        self.connection.save(update_fields=['sync_cursor'])
        with patch('integrations.services.community_bridge.buzz.BuzzBridgeClient.notify_read_state') as notify:
            read_snapshots.flush_notification(self.authority)
        notify.assert_called_once_with([self.owner_key], revision=pending)
        self.connection.refresh_from_db()
        self.assertNotIn('pending', self.connection.sync_cursor[read_snapshots.KEY])
