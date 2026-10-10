"""Database-free export protocol, resolution and causal settlement regressions."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory
from integrations.services.message_sync import inbox_exports as exports
from integrations.api_views_bridge import BuzzCommunityBridgeEventView


class InboxExportTests(SimpleTestCase):
    def payload(self, revision=1, op='read'):
        return {'type': 'read_cursor', 'community_id': str(uuid.UUID(int=1)),
                'account_key': 'a' * 64, 'channel_id': str(uuid.UUID(int=2)),
                'op': op, 'read_through_event': 'b' * 64,
                'read_through_us': '100000001', 'revision': str(revision)}

    def test_wire_rejects_extra_fields_and_lossy_revisions(self):
        self.assertEqual(exports.parse_export(self.payload(2**53 + 1))['revision'], 2**53 + 1)
        for changes in ({'revision': 1}, {'revision': str(2**63)}, {'revision': '0'},
                        {'account_key': 'email'}, {'op': 'delete'}, {'user_id': 1}):
            with self.assertRaises(ValueError):
                exports.parse_export({**self.payload(), **changes})

    def test_newest_unread_wins_and_duplicate_callback_is_idempotent(self):
        read = exports.parse_export(self.payload())
        state, changed = exports.coalesce({}, read, now=100)
        self.assertTrue(changed)
        self.assertEqual(exports.coalesce(state, read, now=200), (state, False))
        unread = exports.parse_export(self.payload(2, 'unread'))
        next_state, changed = exports.coalesce(state, unread, now=101)
        key = exports.room_key(read['community_id'], read['channel_id'])
        self.assertTrue(changed)
        self.assertEqual(next_state[key]['pending']['op'], 'unread')
        self.assertEqual(exports.coalesce(next_state, read, now=300), (next_state, False))

    def test_confirmation_only_settles_covered_revision(self):
        payload = exports.parse_export(self.payload(3))
        key = exports.room_key(payload['community_id'], payload['channel_id'])
        state, _ = exports.coalesce({}, payload, now=100)
        partial = exports.settle(state, key, 2)
        self.assertEqual(partial[key]['applied'], 2)
        self.assertIn('pending', partial[key])
        complete = exports.settle(partial, key, 3)
        self.assertNotIn('pending', complete[key])
        self.assertEqual(exports.settle(complete, key, 1), complete)

    def link(self, source='slack', destination='buzz', source_id='100.000001', destination_id='b'*64, **kwargs):
        return SimpleNamespace(source_platform=source, destination_platform=destination,
            source_message_id=source_id, destination_message_id=destination_id,
            source_deleted_at=None, destination_deleted_at=None, **kwargs)

    def test_inbound_native_and_fallback_resolution_preserve_exact_slack_time(self):
        event = 'b'*64
        inbound = self.link()
        self.assertEqual(exports.resolve_link_timestamp([inbound], event, 0), '100.000001')
        self.assertEqual(exports.resolve_link_timestamp([inbound], event, 0, unread=True), '100.000000')
        native = self.link('buzz', 'slack', event, '101.000002')
        self.assertEqual(exports.resolve_link_timestamp([native], event, 0), '101.000002')
        future = self.link(source_id='102.000003', destination_id='c'*64)
        self.assertEqual(exports.resolve_link_timestamp([inbound, future], None, 101000000), '100.000001')
        inbound.source_deleted_at = 'deleted'
        self.assertEqual(exports.resolve_link_timestamp([inbound], event, 0), '0.000000')

    def test_disconnect_erases_source_identifiers_but_releases_pending_fence(self):
        payload = exports.parse_export(self.payload(3))
        state, _ = exports.coalesce({}, payload, now=100)
        key = exports.room_key(payload['community_id'], payload['channel_id'])
        state[key]['pending']['source_id'] = 'DPRIVATE'
        value = exports.give_up_on_disconnect({exports.KEY: state, 'legacy': 'private'})
        self.assertEqual(value, {exports.KEY: {key: {'received': 3, 'applied': 3}}})
        self.assertNotIn('DPRIVATE', str(value))

    def test_read_confirmation_does_not_settle_an_explicit_unread(self):
        payload = exports.parse_export(self.payload(3, 'unread'))
        state, _ = exports.coalesce({}, payload, now=100)
        key = exports.room_key(payload['community_id'], payload['channel_id'])
        state[key]['pending']['source_ts'] = '100.000000'
        connection = SimpleNamespace(sync_cursor={exports.KEY: state}, save=Mock())
        exports.confirmed_read(connection, SimpleNamespace(channel_id=payload['channel_id']), '101.000000')
        connection.save.assert_not_called()

    @override_settings(MESSAGE_SYNC_INBOX_READ_EXPORT=False)
    def test_disabled_route_and_signature_failure_never_access_database(self):
        factory = APIRequestFactory()
        for valid, expected in [(False, 403), (True, 503)]:
            request = factory.post('/callback', self.payload(), format='json')
            with patch('integrations.services.community_bridge.buzz.BuzzBridgeClient.validate_callback_signature', return_value=valid):
                response = BuzzCommunityBridgeEventView.as_view()(request)
            self.assertEqual(response.status_code, expected)


@override_settings(MESSAGE_SYNC_INBOX_READ_EXPORT=True)
class ExportDeliveryTests(InboxExportTests):
    def pending(self, op='read', requested_at=100):
        from integrations.services.slack_chat_read_state import ReadTarget
        export = exports.parse_export(self.payload(3, op))
        state, _ = exports.coalesce({}, export, now=requested_at)
        key = exports.room_key(export['community_id'], export['channel_id'])
        state[key]['pending'].update(source_ts='100.000001', public_key='a'*64, source_id='C1', device={'device_id': 'd', 'verified_at': 'v'})
        connection = SimpleNamespace(sync_cursor={exports.KEY: state}, save=Mock())
        target = ReadTarget(export['channel_id'], 'C1', 'public_channel')
        authority = SimpleNamespace(scopes={'im:read', 'channels:read', 'channels:write'})
        return connection, target, authority, key

    def flush(self, *, result=None, error=None, op='read', targets=True, now=200):
        from contextlib import nullcontext
        from integrations.services import slack_chat_read_state as reads
        connection, target, authority, key = self.pending(op)
        with patch.object(exports.transaction, 'atomic', side_effect=nullcontext), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(None, connection)
        ), patch.object(reads, '_targets_for_keys', return_value=[target] if targets else []), patch.object(
            reads, 'apply_read', return_value=result, side_effect=error
        ) as apply, patch.object(exports, 'apply_unread', return_value=result, side_effect=error) as unread, patch(
            'integrations.services.message_sync.receipts.complete_read'
        ), patch.object(exports.time, 'time', return_value=now):
            returned = exports.flush_once('grant', authority, {'a'*64})
        return returned, connection.sync_cursor[exports.KEY][key], apply, unread

    def test_confirmed_read_releases_fence_only_after_provider_success(self):
        returned, state, apply, _ = self.flush(result={'synced': True, 'last_read': '100.000001'})
        self.assertEqual(returned, 1)
        self.assertEqual(state['applied'], 3)
        self.assertNotIn('pending', state)
        self.assertEqual(apply.call_args.kwargs['device_binding'], {'device_id': 'd', 'verified_at': 'v'})

    def test_retry_after_keeps_fence_closed_and_advances_only_retry_due(self):
        error = RuntimeError('temporary')
        error.retry_after = 60
        returned, state, _, _ = self.flush(error=error)
        self.assertIsNone(returned)
        self.assertEqual(state['applied'], 0)
        self.assertEqual(state['pending']['due'], 260)

    def test_missing_write_scope_releases_fence_permanently(self):
        error = RuntimeError('permanent')
        error.response = {'error': 'missing_scope'}
        returned, state, _, _ = self.flush(error=error)
        self.assertEqual(returned, 0)
        self.assertEqual(state['applied'], 3)
        self.assertNotIn('pending', state)

    def test_expired_or_revoked_target_does_not_write_slack(self):
        for kwargs in ({'targets': False}, {'now': 100 + 7*86400}):
            returned, state, apply, unread = self.flush(**kwargs)
            self.assertEqual(returned, 0)
            self.assertEqual(state['applied'], 3)
            apply.assert_not_called()
            unread.assert_not_called()

    def test_explicit_unread_uses_unread_path_and_settles_on_success(self):
        returned, state, apply, unread = self.flush(op='unread', result={'synced': True, 'last_read': '100.000001'})
        self.assertEqual(returned, 1)
        apply.assert_not_called()
        unread.assert_called_once()
        self.assertEqual(state['applied'], 3)

    def test_cancelled_advance_does_not_hold_future_observations_hostage(self):
        returned, state, _, _ = self.flush(result={'synced': False, 'cancelled': True})
        self.assertEqual(returned, 1)
        self.assertEqual(state['applied'], 3)

    @override_settings(MLAI_CHAT_ACCOUNT_KEY_SECRET='synthetic-account-secret-at-least-32-bytes')
    def test_duplicate_authenticated_callback_does_not_enqueue_another_intent(self):
        from contextlib import nullcontext
        from community_chat.inbox_accounts import account_key
        payload = self.payload()
        payload['account_key'] = account_key(payload['community_id'], 7)
        export = exports.parse_export(payload)
        state, _ = exports.coalesce({}, export, now=100)
        connection = SimpleNamespace(sync_cursor={exports.KEY: state}, save=Mock())
        grant = SimpleNamespace(user_id=7)
        with patch.object(exports.transaction, 'atomic', side_effect=nullcontext), patch.object(
            exports, '_candidates', return_value=[(grant, None, None)]
        ), patch.object(exports, '_locked_owner_connection', return_value=connection), patch(
            'community_chat.adapter._request', return_value=({'community_id': payload['community_id']}, 200)
        ), patch('integrations.services.message_sync.receipts.enqueue_read') as enqueue:
            self.assertEqual(exports.accept(payload), {'status': 'duplicate'})
        enqueue.assert_not_called()
        connection.save.assert_not_called()
