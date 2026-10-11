"""Database-free source fences, coalescing, consent and retry regressions."""
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.test import SimpleTestCase, override_settings
from integrations.services import slack_chat_read_state as reads
from integrations.services.community_bridge.buzz import BuzzBridgeClient, BuzzBridgeError
from integrations.services.message_sync import inbox_observations as obs, inbox_exports as exports


@override_settings(MESSAGE_SYNC_INBOX_CURSOR_PUSH=True)
class InboxObservationTests(SimpleTestCase):
    now = 1000

    def context(self, applied=3):
        return {'community_id': str(uuid.UUID(int=1)), 'account_key': 'a'*64,
                'channel_id': str(uuid.UUID(int=2)), 'source_id': 'Dsynthetic',
                'authority': 'scoped', 'consent': 'consent-v1', 'applied_revision': applied}

    def key(self):
        return exports.room_key(self.context()['community_id'], self.context()['channel_id'])

    def test_exact_microseconds_and_zero_never_use_float(self):
        for value, expected in [('0', '0.000000'), ('123456789012.123456', '123456789012.123456'),
                                ('123.2', '123.200000')]:
            self.assertEqual(obs.timestamp(value), expected)
        for value in (None, 123.2, '-1', 'NaN', '1.0000001', '1234567890123.123456'):
            self.assertIsNone(obs.timestamp(value))

    def test_coalescing_retains_probe_fence_and_regress_until_delivered(self):
        rows, _ = obs.coalesce({}, self.context(3), '12', observed_at=1000, now=1000)
        rows[self.key()]['delivered_ts'] = '12.000000'
        rows, _ = obs.coalesce(rows, self.context(3), '8', observed_at=1001, now=1001)
        rows, _ = obs.coalesce(rows, self.context(7), '9', observed_at=1002, now=1002)
        self.assertEqual(rows[self.key()]['pending']['op'], 'regress')
        self.assertEqual(rows[self.key()]['pending']['applied_revision'], 7)
        rows, _ = obs.coalesce(rows, self.context(7), '13', observed_at=1003, now=1003)
        self.assertEqual(rows[self.key()]['pending']['op'], 'observe')

    def test_old_invalid_and_expired_info_cannot_replace_newer_probe(self):
        rows, _ = obs.coalesce({}, self.context(), '12', observed_at=1000, now=1000)
        for stamp, observed, now in [('10', 999, 1000), ('NaN', 1001, 1001), ('10', 1001, 1200)]:
            self.assertEqual(obs.coalesce(rows, self.context(), stamp, observed_at=observed, now=now), (rows, False))

    def test_monotonic_source_revision_survives_clock_reversal_and_new_consent(self):
        with patch.object(obs.time, 'time_ns', return_value=1):
            rows, _ = obs.coalesce({}, self.context(), '0', observed_at=1000, now=1000, revision_floor=2**53)
        self.assertEqual(rows[self.key()]['revision'], 2**53+1)
        self.assertEqual(exports.give_up_on_disconnect({obs.KEY: rows, obs.REVISION_KEY: 2**53+1}), {obs.REVISION_KEY: 2**53+1})

    @override_settings(MESSAGE_SYNC_INBOX_CURSOR_PUSH=False)
    def test_disabled_paths_and_source_only_inventory_never_access_database(self):
        self.assertIsNone(obs.capture(object(), reads.ReadTarget('', 'Dsource', 'im')))
        obs.flush(object(), object(), set())
        obs.publication(1)

    def test_source_only_inventory_does_not_create_a_relay_cursor(self):
        target = reads.ReadTarget('not-a-room', 'Dsource', 'im', source_inventory=object())
        self.assertIsNone(obs.capture(object(), target))

    def fixtures(self):
        context = self.context()
        target = reads.ReadTarget(context['channel_id'], context['source_id'], 'im')
        authority = SimpleNamespace(user_id=1, consent_generation=context['consent'], scopes={'im:read'})
        connection = SimpleNamespace(sync_cursor={}, save=Mock())
        grant = SimpleNamespace(consented_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return target, authority, connection, grant

    def test_capture_recovers_old_consent_once_and_freezes_fence_before_probe(self):
        target, authority, connection, grant = self.fixtures()
        with patch.object(obs.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            obs, 'community_id', return_value=self.context()['community_id']), patch(
            'community_chat.inbox_accounts.account_key', return_value='a'*64), patch.object(
            reads, '_cache_key', return_value='scoped'), patch.object(reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            BuzzBridgeClient, 'inbox_export_frontier', return_value=3) as recovery:
            context = obs.capture(authority, target)
            self.assertEqual(context['applied_revision'], 3)
            exports.save(connection, exports.settle(connection.sync_cursor[exports.KEY], self.key(), 7))
            obs.observe_locked(connection, context, '12', observed_at=__import__('time').time())
            self.assertEqual(connection.sync_cursor[obs.KEY][self.key()]['pending']['applied_revision'], 3)
            self.assertEqual(obs.capture(authority, target)['applied_revision'], 7)
            recovery.assert_called_once()

    def test_refresh_queues_metadata_before_history_budget_deferral(self):
        target, authority, connection, grant = self.fixtures()
        grant.slack_user_id = 'Uowner'
        order = []
        def metadata(*args, **kwargs):
            order.append('info')
            return {'channel': {'id': target.slack_id, 'last_read': '12.000000', 'is_member': True}}
        def history(*args, **kwargs):
            order.append('history')
            raise reads.BudgetDeferred(30)
        def observed(*args, **kwargs):
            order.append('observation')
        with patch.object(reads.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads.cache, 'get', return_value=None), patch.object(reads.cache, 'set'), patch.object(
            obs, 'capture', side_effect=lambda *args: (order.append('capture') or self.context())), patch.object(
            obs, 'observe_locked', side_effect=observed), patch.object(reads, '_call_slack_with_grant_authority', side_effect=metadata), patch.object(
            reads, '_unread_messages', side_effect=history), patch.object(reads, '_cache_key', return_value='scoped'):
            with self.assertRaises(reads.BudgetDeferred):
                reads.refresh_target(grant, authority, target)
        self.assertEqual(order, ['capture', 'info', 'observation', 'history'])

    def test_confirmation_uses_settled_export_revision(self):
        target, authority, connection, grant = self.fixtures()
        row = self.context(3)
        connection.sync_cursor = {obs.KEY: {self.key(): row}, exports.KEY: {self.key(): {'received': 7, 'applied': 7}}}
        with patch.object(reads, '_cache_key', return_value='scoped'), patch.object(obs.time, 'time', return_value=1000):
            obs.confirmed_locked(connection, authority, target, '12', observed_at=1000)
        self.assertEqual(connection.sync_cursor[obs.KEY][self.key()]['pending']['applied_revision'], 7)

    @override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=True)
    def test_new_source_baseline_gets_reserved_safety_capacity_without_displacing_hot_rooms(self):
        from integrations.services.message_sync.targeted_reads import select_targeted
        target, _, _, _ = self.fixtures()
        hot = reads.ReadTarget(str(uuid.UUID(int=3)), 'Dhot', 'im', source_activity_ts='1100')
        snapshots = {'scoped': {'available': True, 'last_read': '1200', 'fetched_at': 1199},
                     'hot': {'available': True, 'last_read': '1000', 'fetched_at': 1100}}
        cursor = {obs.KEY: {'hot': {'channel_id': hot.channel_id, 'authority': 'hot', 'last_read': '1000'}}}
        key = lambda value: 'scoped' if value is target else 'hot'
        self.assertIs(select_targeted([target, hot], snapshots, key, cursor, now=1200, turn=0), hot)
        self.assertIs(select_targeted([target, hot], snapshots, key, cursor, now=1200, turn=9), target)

    def test_transport_retry_keeps_identity_and_expiry_requests_fresh_metadata(self):
        target, authority, connection, grant = self.fixtures()
        rows, _ = obs.coalesce({}, self.context(), '12', observed_at=1000, now=1000)
        connection.sync_cursor = {obs.KEY: rows}
        pending = dict(rows[self.key()]['pending'])
        with patch.object(obs.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads, '_targets_for_keys', return_value=[target]), patch.object(reads, '_cache_key', return_value='scoped'), patch.object(
            obs.time, 'time', return_value=1001), patch.object(BuzzBridgeClient, 'push_inbox_cursors', side_effect=BuzzBridgeError('offline')):
            obs.flush(authority, grant, {'verified'})
        self.assertEqual(connection.sync_cursor[obs.KEY][self.key()]['pending'], pending)
        self.assertEqual(connection.sync_cursor[obs.KEY][self.key()]['due'], 1031)
        with patch.object(obs.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads, '_targets_for_keys', return_value=[target]), patch.object(reads, '_cache_key', return_value='scoped'), patch.object(
            obs.time, 'time', return_value=1200), patch.object(BuzzBridgeClient, 'push_inbox_cursors') as submit:
            obs.flush(authority, grant, {'verified'})
        submit.assert_not_called()
        self.assertNotIn('pending', connection.sync_cursor[obs.KEY][self.key()])
        self.assertEqual(connection.sync_cursor['message_sync_read_state_v1']['due'], 0)

    def test_frontier_client_checks_echo_scope_and_lossless_revision(self):
        consent = datetime(2026, 1, 1, microsecond=100000, tzinfo=timezone.utc)
        context = self.context()
        response = {'community_id': context['community_id'], 'request': {
            'channel_id': context['channel_id'], 'account_key': context['account_key'],
            'consented_at': '2026-01-01T00:00:00.1Z'}, 'applied_revision': str(2**53+1)}
        with patch.object(BuzzBridgeClient, '_post_adapter', return_value=response):
            self.assertEqual(BuzzBridgeClient.inbox_export_frontier(community_id=context['community_id'],
                account_key=context['account_key'], channel_id=context['channel_id'], consented_at=consent), 2**53+1)
        response['community_id'] = str(uuid.UUID(int=3))
        with patch.object(BuzzBridgeClient, '_post_adapter', return_value=response), self.assertRaises(BuzzBridgeError):
            BuzzBridgeClient.inbox_export_frontier(community_id=context['community_id'],
                account_key=context['account_key'], channel_id=context['channel_id'], consented_at=consent)

    def test_ignored_fence_preserves_delivered_regress_boundary_and_requests_new_probe(self):
        target, authority, connection, grant = self.fixtures()
        rows, _ = obs.coalesce({}, self.context(), '12', observed_at=999, now=999)
        rows[self.key()]['delivered_ts'] = '12.000000'
        rows, _ = obs.coalesce(rows, self.context(), '8', observed_at=1000, now=1000)
        connection.sync_cursor = {obs.KEY: rows}
        with patch.object(obs.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads, '_targets_for_keys', return_value=[target]), patch.object(reads, '_cache_key', return_value='scoped'), patch.object(
            obs.time, 'time', return_value=1001), patch.object(BuzzBridgeClient, 'push_inbox_cursors', return_value=[{'accepted': True, 'message': 'ignored:causal_fence:revision:7'}]):
            obs.flush(authority, grant, {'verified'})
        self.assertEqual(connection.sync_cursor[obs.KEY][self.key()]['delivered_ts'], '12.000000')
        self.assertNotIn('pending', connection.sync_cursor[obs.KEY][self.key()])
        rows, _ = obs.coalesce(connection.sync_cursor[obs.KEY], self.context(7), '9', observed_at=1002, now=1002)
        self.assertEqual(rows[self.key()]['pending']['op'], 'regress')

    def test_current_verified_routing_loss_erases_pending_source_observation(self):
        target, authority, connection, grant = self.fixtures()
        rows, _ = obs.coalesce({}, self.context(), '12', observed_at=1000, now=1000)
        connection.sync_cursor = {obs.KEY: rows}
        with patch.object(obs.transaction, 'atomic', side_effect=lambda: nullcontext()), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(grant, connection)), patch.object(
            reads, '_targets_for_keys', return_value=[]), patch.object(obs.time, 'time', return_value=1001), patch.object(
            BuzzBridgeClient, 'push_inbox_cursors') as submit:
            obs.flush(authority, grant, set())
        submit.assert_not_called()
        self.assertEqual(connection.sync_cursor[obs.KEY], {})
