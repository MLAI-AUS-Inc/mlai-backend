"""Privacy and event-family contracts; no database or Slack calls."""
from contextlib import ExitStack, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services.community_bridge import member_channel_events as router

WORKSPACE, CHANNEL = 'TTESTMEMBERS', 'CTESTMEMBERS'
ROOM, GENERATION = '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001'


def payload(event, **changes):
    return dict(type='event_callback', api_app_id='AUSER', team_id=WORKSPACE,
                event_id='E1', event_time=1700000000, event=event, **changes)


def public_row(**changes):
    return SimpleNamespace(**(dict(pk=7, destination_platform='buzz', destination_workspace_id='relay', destination_channel_id=ROOM,
        slack_visibility='public', slack_archived=False, slack_deleted=False, mirror_decision='mirror',
        hold_reason='', slack_seen_at=timezone.now(), audience='members', enabled=True,
        slack_workspace_id=WORKSPACE, slack_channel_id=CHANNEL, audience_generation=GENERATION,
        audience_hash='ab' * 32, audience_sync_pending=False, room_sync_pending=False, room_sync_error='',
        room_state={'applied': {'audience': 'members'}, 'audience_applied': {
            'generation': GENERATION, 'hash': 'ab' * 32, 'workspace_id': WORKSPACE,
            'channel_id': CHANNEL, 'destination_channel_id': ROOM}}, save=Mock()) | changes))


@override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER', MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID=WORKSPACE)
class MemberEventRouterTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.query = self.stack.enter_context(patch.object(router.CommunityBridgeChannel.objects, 'filter'))
        self.query.return_value.first.return_value = public_row()
        locked = self.stack.enter_context(patch.object(router.CommunityBridgeChannel.objects, 'select_for_update'))
        locked.return_value.filter.return_value = self.query.return_value
        self.stack.enter_context(patch.object(router.transaction, 'atomic', side_effect=nullcontext))
        self.private = self.stack.enter_context(patch.object(router.SlackDmMirrorConversation.objects, 'filter'))
        self.private.return_value.exists.return_value = False
        self.enabled = self.stack.enter_context(patch.object(router.access, 'enabled', return_value=True))
        self.scope = self.stack.enter_context(patch.object(router.channels, 'member_channel_in_scope', return_value=True))
        self.refresh = self.stack.enter_context(patch.object(router.channels, 'queue_refresh'))
        self.reduce = self.stack.enter_context(patch.object(router.channels, 'mark_source_reduction'))
        self.grants = self.stack.enter_context(patch.object(router, '_subject_grants', return_value=[SimpleNamespace(pk=3)]))
        self.join = self.stack.enter_context(patch.object(router.membership, 'mark_joined'))
        self.leave = self.stack.enter_context(patch.object(router.membership, 'mark_left'))
        self.ingest = self.stack.enter_context(patch('integrations.services.community_bridge.store.ingest_slack_event', return_value={'status': 'accepted'}))

    def test_content_families_share_authoritative_public_mapping(self):
        events = [
            {'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'ts': '1700000000.000001', 'user': 'UACTOR'},
            {'type': 'message', 'subtype': 'message_changed', 'channel': CHANNEL, 'message': {'ts': '1700000000.000001', 'user': 'UACTOR'}},
            {'type': 'message', 'subtype': 'message_deleted', 'channel': CHANNEL, 'deleted_ts': '1700000000.000001'},
            *({'type': kind, 'item': {'type': 'message', 'channel': CHANNEL, 'ts': '1700000000.000001'}, 'user': 'UACTOR'}
              for kind in ('reaction_added', 'reaction_removed')),
        ]
        for event in events:
            with self.subTest(event=event['type'], subtype=event.get('subtype')):
                self.ingest.reset_mock()
                self.assertEqual(router.route_member_public_event(payload(event)), {'status': 'accepted'})
                self.assertEqual(self.ingest.call_args.kwargs, {'public_source_verified': True})
                self.assertEqual(self.ingest.call_args.args[0]['event']['channel_type'], 'channel')

    def test_no_callback_recipient_or_item_user_is_membership_subject(self):
        for event in [
            {'type': 'message', 'channel': CHANNEL, 'subtype': 'message_changed', 'user': 'UOUTER'},
            {'type': 'message', 'channel': CHANNEL, 'subtype': 'message_deleted', 'user': 'UOUTER'},
            {'type': 'reaction_added', 'item': {'channel': CHANNEL, 'type': 'message'}, 'item_user': 'UITEM', 'user': 'UREACTOR'},
        ]:
            router.route_member_public_event(payload(event, authorizations=[{'user_id': 'URECIPIENT'}]))
        self.grants.assert_not_called()
        self.join.assert_not_called()

    def test_bot_marked_messages_never_infer_human_membership(self):
        for changes in ({'bot_id': 'BBOT'}, {'bot_profile': {'id': 'BBOT'}}, {'subtype': 'bot_message'}):
            with self.subTest(changes=changes):
                event = {'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel',
                         'user': 'UACTOR', **changes}
                self.assertEqual(router.route_member_public_event(payload(event)), {'status': 'accepted'})
        self.grants.assert_not_called()
        self.join.assert_not_called()

    def test_join_uses_signed_subject_and_single_letter_type_needs_mapping(self):
        event = {'type': 'member_joined_channel', 'channel': CHANNEL, 'channel_type': 'C', 'user': 'USUBJECT'}
        router.route_member_public_event(payload(event))
        self.grants.assert_called_once_with(WORKSPACE, 'USUBJECT')
        self.assertIs(self.join.call_args.kwargs['public_verified'], True)
        self.query.return_value.first.return_value = public_row(slack_seen_at=None)
        self.join.reset_mock()
        self.assertEqual(router.route_member_public_event(payload(event))['status'], 'member_audience_pending')
        self.join.assert_not_called()
        hint = router.channels.info_reader_hint(self.refresh.call_args.kwargs['reason'])
        self.assertEqual((hint.grant_id, hint.observed_at), (3, router._observed_at(payload(event), event)))

    def test_legacy_mapping_hint_cannot_replace_current_reader_authority(self):
        self.query.return_value.first.return_value = public_row(slack_seen_at=None, audience='community')
        for subtype in ('message_changed', 'message_deleted', {'malformed': True}):
            self.assertIsNone(router.route_member_public_event(payload({
                'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'subtype': subtype,
            })))
        with override_settings(SLACK_COMMUNITY_SOURCE='both'):
            self.assertEqual(router.route_member_public_event(payload({
                'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'user': 'UACTOR',
            })), {'status': 'community_source_pending'})
        self.ingest.assert_not_called()

    def test_members_source_without_server_reader_policy_keeps_public_content_pending(self):
        self.query.return_value.first.return_value = public_row(slack_seen_at=None, audience='community')
        with override_settings(SLACK_COMMUNITY_SOURCE='members'):
            result = router.route_member_public_event(payload({
                'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'user': 'UACTOR',
            }))
        self.assertEqual(result, {'status': 'community_source_pending'})
        self.ingest.assert_not_called()

    def test_stale_or_missing_members_public_proof_queues_refresh_for_all_activity(self):
        events = [
            {'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'user': 'UACTOR'},
            {'type': 'message', 'channel': CHANNEL, 'subtype': 'message_changed'},
            {'type': 'message', 'channel': CHANNEL, 'subtype': 'message_deleted'},
            {'type': 'reaction_added', 'item': {'channel': CHANNEL, 'type': 'message'}},
            {'type': 'member_joined_channel', 'channel': CHANNEL, 'channel_type': 'C', 'user': 'UACTOR'},
        ]
        for observed in (None, timezone.now() - timedelta(seconds=301), 'malformed'):
            self.query.return_value.first.return_value = public_row(slack_seen_at=observed)
            for event in events:
                with self.subTest(observed=observed, event=event):
                    self.assertEqual(router.route_member_public_event(payload(event))['status'], 'member_audience_pending')
                    hint = router.channels.info_reader_hint(self.refresh.call_args.kwargs['reason'])
                    if event['type'] == 'member_joined_channel' or router._ordinary_create(event):
                        self.assertEqual(hint.grant_id, 3)
                    else:
                        self.assertIsNone(hint)
                        self.assertEqual(self.refresh.call_args.kwargs['reason'], 'public_source_unverified')
        self.ingest.assert_not_called()
        self.join.assert_not_called()

    def test_current_locked_pending_or_mismatched_ack_keeps_content_waiting(self):
        candidate = payload({'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'user': 'UACTOR'})
        for changes in (dict(audience_sync_pending=True), dict(room_sync_pending=True),
                        dict(audience_hash='cd' * 32), dict(room_state={}), dict(enabled=False)):
            original, current = public_row(), public_row(**changes)
            self.query.return_value.first.side_effect = [original, current]
            with self.subTest(changes=changes):
                self.assertEqual(router.route_member_public_event(candidate)['status'], 'member_audience_pending')
        self.ingest.assert_not_called()

    def test_unknown_leave_can_only_queue_metadata_refresh_while_enabled_in_scope(self):
        self.query.return_value.first.return_value = None
        candidate = payload({'type': 'member_left_channel', 'channel': CHANNEL, 'channel_type': 'C', 'user': 'USUBJECT'})
        self.assertIsNone(router.route_member_public_event(candidate))
        self.refresh.assert_called_once_with(WORKSPACE, CHANNEL, reason='public_source_unverified')
        self.leave.assert_not_called()
        self.enabled.return_value = False
        self.refresh.reset_mock()
        self.assertIsNone(router.route_member_public_event(candidate))
        self.refresh.assert_not_called()

    def test_lifecycle_family_extracts_nested_ids_without_message_receipts(self):
        for kind in ('channel_created', 'channel_rename', 'channel_unarchive', 'channel_unshared'):
            event = {'type': kind, 'channel': {'id': CHANNEL} if kind in {'channel_created', 'channel_rename'} else CHANNEL}
            self.assertEqual(router.route_member_public_event(payload(event))['status'], 'member_refresh_queued')
            self.refresh.assert_called_with(WORKSPACE, CHANNEL, reason=kind)
        self.ingest.assert_not_called()

    def test_known_retired_private_ownership_overrides_public_cache_and_hint(self):
        self.private.return_value.exists.return_value = True
        self.assertIsNone(router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel'})))
        self.reduce.assert_called_once()
        self.assertEqual(self.reduce.call_args.kwargs['reason'], 'converted_to_private')
        self.ingest.assert_not_called()
        self.join.assert_not_called()

    def test_private_nested_flags_override_public_cache(self):
        for event in [
            {'type': 'message', 'channel': CHANNEL, 'channel_type': 'group'},
            {'type': 'message', 'channel': CHANNEL, 'message': {'is_private': True}},
            {'type': 'reaction_added', 'item': {'channel': CHANNEL, 'channel_type': 'group'}},
        ]:
            with self.subTest(event=event):
                self.assertIsNone(router.route_member_public_event(payload(event)))
        self.ingest.assert_not_called()

    def test_connect_envelope_and_nested_flags_fail_closed_even_off(self):
        self.enabled.return_value = False
        for event, extra in [
            ({'type': 'message', 'channel': CHANNEL}, {'is_ext_shared_channel': True}),
            ({'type': 'reaction_added', 'item': {'channel': CHANNEL, 'is_ext_shared': True}}, {}),
            ({'type': 'channel_shared', 'channel': CHANNEL}, {}),
            ({'type': 'message', 'channel': CHANNEL, 'message': {'is_shared': 'false'}}, {}),
            ({'type': 'channel_created', 'channel': {'id': CHANNEL, 'pending_shared': ['TOTHER']}}, {}),
        ]:
            with self.subTest(event=event):
                self.assertEqual(router.route_member_public_event(payload(event, **extra))['status'], 'member_source_reduction_queued')
                self.assertEqual(self.reduce.call_args.kwargs['reason'], 'slack_connect')
        self.ingest.assert_not_called()

    def test_off_controls_reduce_known_rows_and_unknown_rows_stay_inert(self):
        self.enabled.return_value = False
        events = [
            {'type': 'channel_archive', 'channel': CHANNEL},
            {'type': 'channel_deleted', 'channel': CHANNEL},
            {'type': 'channel_id_changed', 'old_channel_id': CHANNEL, 'new_channel_id': 'CNEWROOM'},
            {'type': 'member_left_channel', 'channel': CHANNEL, 'user': 'USUBJECT'},
        ]
        for event in events:
            self.assertIsNotNone(router.route_member_public_event(payload(event)))
        self.leave.assert_called_once()
        self.assertIs(self.leave.call_args.kwargs['public_verified'], True)
        self.query.return_value.first.return_value = None
        self.reduce.reset_mock()
        for event in events:
            self.assertIsNone(router.route_member_public_event(payload(event)))
        self.reduce.assert_not_called()

    def test_id_change_callback_holds_unmapped_source_without_activating_rekey_helper(self):
        self.query.return_value.first.return_value = public_row(destination_channel_id='')
        candidate = payload({'type': 'channel_id_changed', 'old_channel_id': CHANNEL, 'new_channel_id': 'CNEWROOM'})
        with patch.object(router.channels, 'rekey_channel') as rekey:
            for enabled in (True, False):
                self.enabled.return_value = enabled
                self.assertEqual(router.route_member_public_event(candidate)['status'], 'member_source_reduction_queued')
                self.assertEqual(self.reduce.call_args.kwargs['reason'], 'rekey_requires_room_migration')
            self.query.return_value.first.return_value = None
            self.assertIsNone(router.route_member_public_event(candidate))
        rekey.assert_not_called()
        self.ingest.assert_not_called()
        self.join.assert_not_called()

    def test_wrong_app_workspace_and_invalid_identity_do_not_discover(self):
        event = {'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel'}
        for candidate in [dict(payload(event), api_app_id='ABOT'), dict(payload(event), team_id='TOTHER'),
                          payload(dict(event, channel={'unexpected': True})), payload(dict(event, channel='C'))]:
            self.assertIsNone(router.route_member_public_event(candidate))
        self.query.assert_not_called()

    def test_unknown_and_unverified_sources_refresh_without_join_or_content(self):
        self.query.return_value.first.return_value = None
        self.assertIsNone(router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL, 'channel_type': 'channel', 'user': 'UACTOR'})))
        self.refresh.assert_called_once()
        self.assertEqual(router.channels.info_reader_hint(self.refresh.call_args.kwargs['reason']).grant_id, 3)
        self.join.assert_not_called()
        self.ingest.assert_not_called()

    def test_unknown_probe_hint_uses_only_ordinary_nonbot_actor_or_join_subject(self):
        self.query.return_value.first.return_value = None
        events = [
            ({'type': 'message', 'user': 'UACTOR'}, True),
            ({'type': 'message', 'user': 'UACTOR', 'subtype': 'thread_broadcast'}, True),
            ({'type': 'member_joined_channel', 'user': 'USUBJECT'}, True),
            ({'type': 'message', 'user': 'UACTOR', 'bot_id': 'BBOT'}, False),
            ({'type': 'message', 'user': 'UACTOR', 'bot_profile': {'id': 'BBOT'}}, False),
            ({'type': 'message', 'subtype': 'message_changed', 'user': 'UOUTER'}, False),
            ({'type': 'message', 'subtype': 'message_deleted', 'user': 'UOUTER'}, False),
            ({'type': 'reaction_added', 'user': 'UREACTOR', 'item_user': 'UITEM',
              'item': {'channel': CHANNEL, 'type': 'message'}}, False),
        ]
        for event, expected in events:
            event = {'channel': CHANNEL, **event}
            self.refresh.reset_mock()
            self.grants.reset_mock()
            with self.subTest(event=event):
                router.route_member_public_event(payload(event, authorizations=[{'user_id': 'URECIPIENT'}]))
                hint = router.channels.info_reader_hint(self.refresh.call_args.kwargs['reason'])
                self.assertEqual(hint is not None, expected)
                if expected:
                    self.grants.assert_called_once_with(WORKSPACE, event['user'])
                    self.assertEqual(hint.grant_id, 3)
                else:
                    self.grants.assert_not_called()
        self.join.assert_not_called()
        self.ingest.assert_not_called()

    def test_unknown_probe_hint_requires_usable_subject_and_observation(self):
        self.query.return_value.first.return_value = None
        for changes in ({'event_ts': True}, {'event_ts': 'NaN'}, {'event_ts': '-1'}):
            self.grants.reset_mock()
            router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL, 'user': 'UACTOR', **changes}))
            self.assertEqual(self.refresh.call_args.kwargs['reason'], 'public_source_unverified')
            self.grants.assert_not_called()
        self.grants.return_value = []
        router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL, 'user': 'UNLINKED'}))
        self.assertEqual(self.refresh.call_args.kwargs['reason'], 'public_source_unverified')

    def test_flag_and_scope_gate_only_new_content(self):
        for flag, scope in [(False, True), (True, False)]:
            self.enabled.return_value, self.scope.return_value = flag, scope
            self.assertEqual(router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL}))['status'], 'ignored')
        self.ingest.assert_not_called()
        self.refresh.assert_not_called()

    @override_settings(SLACK_COMMUNITY_SOURCE='bot')
    def test_retired_bot_setting_cannot_bypass_current_member_reader_policy(self):
        self.query.return_value.first.return_value = public_row(audience='community')
        result = router.route_member_public_event(payload({'type': 'message', 'channel': CHANNEL}))
        self.assertEqual(result, {'status': 'community_source_pending'})
        self.ingest.assert_not_called()

    def test_unusable_timestamp_does_not_invent_membership_observation(self):
        for stamp in (True, [], 'NaN', '-1', '1e999999'):
            with self.subTest(stamp=stamp):
                self.assertIsNone(router._observed_at({'event_time': 1700000000}, {'event_ts': stamp}))


@override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER')
class CallbackDispatchTests(SimpleTestCase):
    def test_shared_order_and_falsey_handled_results_do_not_fall_through(self):
        private, public = Mock(return_value={'status': 'private'}), Mock()
        with patch.object(router.channels, 'route_member_public_event', return_value={}) as member:
            self.assertEqual(router.dispatch_slack_callback({'api_app_id': 'AUSER'}, private_ingest=private, public_ingest=public), {})
        private.assert_not_called()
        public.assert_not_called()
        member.assert_called_once()

    def test_unowned_user_callback_never_reaches_generic_public_store(self):
        private, public = Mock(return_value=None), Mock()
        with patch.object(router.channels, 'route_member_public_event', return_value=None):
            self.assertEqual(router.dispatch_slack_callback({'api_app_id': 'AUSER'}, private_ingest=private, public_ingest=public)['reason'], 'unowned_member_event')
            private.return_value = {'status': 'private'}
            self.assertEqual(router.dispatch_slack_callback({'api_app_id': 'AUSER'}, private_ingest=private, public_ingest=public)['status'], 'private')
        public.assert_not_called()

    def test_bot_generic_fallback_retains_baseline(self):
        private, public = Mock(return_value=None), Mock(return_value={'status': 'bot'})
        with patch.object(router.channels, 'route_member_public_event', return_value=None):
            self.assertEqual(router.dispatch_slack_callback({'api_app_id': 'ABOT'}, private_ingest=private, public_ingest=public), {'status': 'bot'})
        public.assert_called_once_with({'api_app_id': 'ABOT'})
