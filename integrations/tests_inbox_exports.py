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


class CursorDependencySignalTests(SimpleTestCase):
    def test_retry_dependencies_are_classified_without_callback_or_exception_contents(self):
        from django.db import DatabaseError
        from community_chat.adapter import MembershipAdapterUnavailable
        for error, dependency, reason in (
            (DatabaseError("synthetic-private-database-details"), "database", "database_unavailable"),
            (MembershipAdapterUnavailable("synthetic-private-adapter-details"), "relay_adapter", "adapter_unavailable"),
            (RuntimeError("inbox_delivery_link_pending"), "bridge_delivery", "anchor_pending"),
            (RuntimeError("private_inbox_anchor_pending"), "bridge_delivery", "anchor_pending"),
            (RuntimeError("synthetic-private-cursor-details"), "read_cursor", "processing_unavailable"),
        ):
            request = APIRequestFactory().post("/callback", InboxExportTests().payload(), format="json")
            with self.subTest(dependency=dependency, reason=reason), patch(
                "integrations.api_views_bridge.BuzzBridgeClient.validate_callback_signature", return_value=True,
            ), patch.object(exports, "accept", side_effect=error), self.assertLogs(
                "integrations.api_views_bridge", level="WARNING",
            ) as logged:
                response = BuzzCommunityBridgeEventView.as_view()(request)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data, {"error": "read_cursor_retry"})
            self.assertEqual(len(logged.output), 1)
            self.assertIn(f"dependency={dependency} reason={reason}", logged.output[0])
            self.assertNotIn("synthetic-private", str(logged.output))
            self.assertNotIn("account_key", str(logged.output))

    @override_settings(MESSAGE_SYNC_INBOX_READ_EXPORT=False)
    def test_disabled_export_is_a_configuration_signal_and_unsigned_requests_cannot_emit_it(self):
        for valid in (False, True):
            request = APIRequestFactory().post("/callback", InboxExportTests().payload(), format="json")
            with patch(
                "integrations.api_views_bridge.BuzzBridgeClient.validate_callback_signature", return_value=valid,
            ), patch("integrations.api_views_bridge.logger") as logger:
                response = BuzzCommunityBridgeEventView.as_view()(request)
            self.assertEqual(response.status_code, 503 if valid else 403)
            if valid:
                self.assertIn("reason=inbox_export_disabled", logger.info.call_args.args[0])
            else:
                logger.info.assert_not_called()
                logger.warning.assert_not_called()


class PublicAnchorDeliveryTests(SimpleTestCase):
    link = InboxExportTests.link

    def test_terminal_public_anchor_falls_back_and_retryable_anchor_waits(self):
        from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink
        anchor = 'b' * 64
        inbound = self.link(source_id='100.000001', destination_id='c' * 64)
        for delivery_status in ('dead', 'completed', 'pending', 'processing', 'waiting_parent', 'failed'):
            with self.subTest(status=delivery_status):
                links = Mock()
                links.filter.return_value = []
                delivery = Mock()
                delivery.exclude.side_effect = lambda **kwargs: SimpleNamespace(
                    exists=lambda: delivery_status not in kwargs['status__in'])
                with patch.object(CommunityBridgeMessageLink.objects, 'filter', return_value=links), patch.object(
                    CommunityBridgeDelivery.objects, 'filter', return_value=delivery
                ) as query, patch.object(exports, 'latest_inbound_before', return_value=inbound) as fallback:
                    if delivery_status in {'dead', 'completed'}:
                        self.assertEqual(exports.public_anchor_timestamp('mapped', anchor, 101000000), '100.000001')
                        fallback.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, '^inbox_delivery_link_pending$'):
                            exports.public_anchor_timestamp('mapped', anchor, 101000000)
                        fallback.assert_not_called()
                    query.assert_called_once_with(channel='mapped', source_platform='buzz',
                                                  source_message_id=anchor, delivery_type='create')

    def test_completed_native_anchor_uses_exact_link_without_history_fallback(self):
        from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink
        anchor = 'b' * 64
        links = Mock()
        links.filter.return_value = [self.link('buzz', 'slack', anchor, '101.000002')]
        with patch.object(CommunityBridgeMessageLink.objects, 'filter', return_value=links), patch.object(
            CommunityBridgeDelivery.objects, 'filter'
        ) as delivery, patch.object(exports, 'latest_inbound_before') as fallback:
            self.assertEqual(exports.public_anchor_timestamp('mapped', anchor, 100000000, unread=True), '101.000001')
            self.assertEqual(exports.public_anchor_resolution('mapped', anchor, 100000000, unread=True),
                             ('101.000001', '101.000002'))
        delivery.assert_not_called()
        fallback.assert_not_called()

    def test_inbound_or_invalid_direct_link_is_not_a_native_boundary(self):
        from integrations.models import CommunityBridgeMessageLink
        anchor = 'b' * 64
        for link in (self.link(), self.link('buzz', 'slack', anchor, 'invalid')):
            with self.subTest(source=link.source_platform):
                links = Mock()
                links.filter.return_value = [link]
                with patch.object(CommunityBridgeMessageLink.objects, 'filter', return_value=links):
                    self.assertIsNone(exports.public_anchor_resolution('mapped', anchor, 100000000)[1])


class NativeCursorBoundaryTests(SimpleTestCase):
    def fixture(self, op='read'):
        payload = InboxExportTests().payload(3, op)
        payload['read_through_us'] = '99999999' if op == 'unread' else '100000000'
        export = exports.parse_export(payload)
        key = exports.room_key(export['community_id'], export['channel_id'])
        state, _ = exports.coalesce({}, export, now=1000)
        source_ts = '102.499999' if op == 'unread' else '102.500000'
        pending = state[key]['pending']
        pending.update(source_ts=source_ts, native_source_ts='102.500000')
        authority = SimpleNamespace(consent_generation='consent-v1')
        with patch('integrations.services.slack_chat_read_state._cache_key', return_value='scoped'):
            confirmation = exports.native_confirmation(pending, source_ts, authority, object())
        context = {'community_id': export['community_id'], 'channel_id': export['channel_id'],
                   'account_key': export['account_key'],
                   'applied_revision': 3, 'authority': 'scoped', 'consent': 'consent-v1'}
        return state, key, pending, confirmation, context

    def test_confirmed_native_read_and_unread_echo_use_original_display_boundary(self):
        for op, raw, expected in [('read', '102.500000', '100.000000'),
                                  ('unread', '102.499999', '99.999999')]:
            with self.subTest(op=op):
                state, key, _, confirmation, context = self.fixture(op)
                state = exports.settle(state, key, 3, confirmation=confirmation)
                self.assertEqual(exports.native_source_frontier(state, context, raw), (expected, True))

    def test_external_native_read_or_unread_maps_the_adjacent_boundary_without_echo(self):
        for op, raw, expected in [('read', '102.499999', '99.999999'),
                                  ('unread', '102.500000', '100.000000')]:
            with self.subTest(op=op):
                state, key, _, confirmation, context = self.fixture(op)
                state = exports.settle(state, key, 3, confirmation=confirmation)
                self.assertEqual(exports.native_source_frontier(state, context, raw), (expected, False))

    def test_unrelated_slack_positions_are_never_translated(self):
        state, key, _, confirmation, context = self.fixture()
        state = exports.settle(state, key, 3, confirmation=confirmation)
        for raw in ('0', '102.499998', '102.500001', '103', 'NaN'):
            with self.subTest(raw=raw):
                from integrations.services.message_sync.inbox_observations import timestamp
                self.assertEqual(exports.native_source_frontier(state, context, raw), (timestamp(raw), False))

    def test_probe_fence_and_oauth_consent_scope_must_match_the_confirmation(self):
        state, key, _, confirmation, context = self.fixture()
        state = exports.settle(state, key, 3, confirmation=confirmation)
        for changes in ({'applied_revision': 2}, {'authority': 'new-oauth'}, {'consent': 'new-consent'},
                        {'account_key': 'c'*64}):
            with self.subTest(changes=changes):
                self.assertEqual(exports.native_source_frontier(state, {**context, **changes}, '102.500000'),
                                 ('102.500000', False))
        state[key]['received'] = 4
        self.assertEqual(exports.native_source_frontier(state, context, '102.500000'), ('102.500000', False))

    def test_permanent_give_up_or_a_later_slack_cursor_does_not_create_a_native_receipt(self):
        state, key, pending, _, context = self.fixture()
        authority = SimpleNamespace(consent_generation='consent-v1')
        self.assertIsNone(exports.native_confirmation(pending, '103', authority, object()))
        self.assertIsNone(exports.native_confirmation(pending, None, authority, object()))
        state = exports.settle(state, key, 3)
        self.assertEqual(exports.native_source_frontier(state, context, '102.500000'), ('102.500000', False))

    def test_repeated_completion_preserves_receipt_and_newer_export_removes_it(self):
        state, key, _, confirmation, context = self.fixture()
        state = exports.settle(state, key, 3, confirmation=confirmation)
        self.assertEqual(exports.settle(state, key, 3), state)
        self.assertEqual(exports.settle(state, key, 2, confirmation={}), state)
        next_export = exports.parse_export(InboxExportTests().payload(4, 'unread'))
        next_state, _ = exports.coalesce(state, next_export, now=1001)
        self.assertNotIn('confirmed_native', next_state[key])
        self.assertEqual(exports.native_source_frontier(next_state, context, '102.500000'), ('102.500000', False))

    def test_disconnect_retains_only_revision_fences(self):
        state, key, _, confirmation, _ = self.fixture()
        state = exports.settle(state, key, 3, confirmation=confirmation)
        self.assertEqual(exports.give_up_on_disconnect({exports.KEY: state}),
                         {exports.KEY: {key: {'received': 3, 'applied': 3}}})

    def test_native_read_confirmation_retains_boundary_before_pending_is_removed(self):
        from integrations.services import slack_chat_read_state as reads
        state, key, _, confirmation, context = self.fixture()
        connection = SimpleNamespace(sync_cursor={exports.KEY: state}, save=Mock())
        target = reads.ReadTarget(context['channel_id'], 'Csynthetic', 'public_channel')
        with patch.object(reads, '_cache_key', return_value='scoped'):
            exports.confirmed_read(connection, target, '102.500000',
                                   authority=SimpleNamespace(consent_generation='consent-v1'))
        self.assertEqual(connection.sync_cursor[exports.KEY][key]['confirmed_native'], confirmation)
        self.assertNotIn('pending', connection.sync_cursor[exports.KEY][key])

    @override_settings(MESSAGE_SYNC_INBOX_READ_EXPORT=True)
    def test_native_unread_flush_records_confirmation_before_source_echo(self):
        from contextlib import nullcontext
        from integrations.services import slack_chat_read_state as reads
        connection, target, authority, key = ExportDeliveryTests().pending('unread')
        authority.consent_generation = 'consent-v1'
        pending = connection.sync_cursor[exports.KEY][key]['pending']
        pending.update(read_through_us=99999999, native_source_ts='102.500000', source_ts='102.499999')
        order = []
        def echo(*args, **kwargs):
            order.append('echo')
            receipt = connection.sync_cursor[exports.KEY][key]['confirmed_native']
            self.assertEqual(receipt['anchor_display_us'], 100000000)
            self.assertEqual(receipt['source_us'], 102499999)
            self.assertEqual(connection.sync_cursor[exports.KEY][key]['applied'], 3)
        with patch.object(exports.transaction, 'atomic', side_effect=nullcontext), patch.object(
            reads, '_lock_slack_grant_api_authority', return_value=(None, connection)), patch.object(
            reads, '_targets_for_keys', return_value=[target]), patch.object(reads, '_cache_key', return_value='scoped'), patch.object(
            exports, 'apply_unread', return_value={'synced': True, 'last_read': '102.499999'}), patch(
            'integrations.services.message_sync.inbox_observations.confirmed_locked', side_effect=echo), patch.object(
            exports.time, 'time', return_value=200):
            self.assertEqual(exports.flush_once('grant', authority, {'a'*64}), 1)
        self.assertEqual(order, ['echo'])
        self.assertNotIn('pending', connection.sync_cursor[exports.KEY][key])


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
