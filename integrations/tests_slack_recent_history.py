"""Database/network-free worker and authorization regressions for recent-first sync."""
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services import slack_dm_mirror as dm, slack_history_pages as api
from integrations.services.message_sync import history, private_history, recent_history
from integrations.services.message_sync.scheduler import validate_checkpoint


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True)
class RecentHistoryPolicyTests(SimpleTestCase):
    def test_permanently_failed_public_delivery_cannot_certify_source_exhaustion(self):
        from integrations.services.message_sync.head_repair import head_scope
        state = SimpleNamespace(authority_generation=1, public_channel=SimpleNamespace(
            slack_workspace_id='T1', slack_channel_id='C1', destination_workspace_id='community',
            destination_channel_id='room'), source_channel_id='C1', jobs=Mock())
        job = SimpleNamespace(pk=1, checkpoint={'authority_generation': head_scope(state),
            'timeline_complete': True, 'recent_messages': 50}, last_error_code='')
        state.jobs.select_for_update.return_value.get.return_value = job
        deliveries = Mock()
        deliveries.filter.return_value.exists.side_effect = [False, True]
        with patch.object(api, 'schedule_job', return_value=job), patch.object(
            api.CommunityBridgeDelivery.objects, 'filter', return_value=deliveries
        ):
            result = api._public_progress(state, 'a' * 64)
        self.assertEqual(result['error'], 'slack_history_unavailable')
        self.assertFalse(result['pending'])

    def test_provider_deferral_preserves_pending_and_retry_after(self):
        from datetime import timedelta
        now = api.timezone.now()
        job = SimpleNamespace(due_at=now + timedelta(seconds=90))
        with patch.object(api.timezone, 'now', return_value=now):
            for error in ('BudgetDeferred', 'SlackDmMirrorRateLimited', 'ratelimited'):
                result = api._payload(False, True, error, job=job)
                self.assertEqual(result, {'has_more': True, 'pending': True, 'retry_after_seconds': 90})
        self.assertEqual(api._payload(False, True, 'network')['error'], 'slack_history_unavailable')

    def test_only_unfinished_explicit_demand_receives_provider_priority(self):
        from integrations.services.message_sync import runner
        from integrations.services.message_sync.request_priority import current_priority
        pending = recent_history.request_batch({'recent_messages': 50}, 'a' * 64)
        completed = {**pending, 'recent_messages': 100}
        for private in (False, True):
            for progress, enabled, expected in ((pending, True, 'foreground'),
                                                (completed, True, 'background'),
                                                (pending, False, 'background')):
                with self.subTest(private=private, enabled=enabled, expected=expected):
                    state = SimpleNamespace(private_conversation_id=int(private), private_conversation=Mock(),
                                            foreground_refresh=False)
                    lease = SimpleNamespace(state_id=1, kind='archive', checkpoint=progress)
                    rows, seen = Mock(), []
                    rows.select_related.return_value.get.return_value = state
                    with override_settings(MESSAGE_SYNC_OPEN_PRIORITY_ENABLED=enabled), patch.object(
                        runner, 'claim_job', return_value=lease
                    ), patch.object(runner.BridgeSyncState.objects, 'all', return_value=rows), patch(
                        'integrations.services.slack_chat_refresh.prioritize_open_conversations', return_value=rows
                    ), patch.object(recent_history, 'private_progress', return_value=SimpleNamespace(metadata=progress)), patch.object(
                        runner, 'private_page' if private else 'public_page',
                        side_effect=lambda *_: seen.append(current_priority())
                    ), patch.object(runner, 'fail_job') as failure:
                        self.assertEqual(runner.process_history_once(seed=False), 1)
                    self.assertEqual(seen, [expected])
                    failure.assert_not_called()

    def test_restricted_app_preloads_fifty_over_four_durable_turns(self):
        progress, sizes = {}, []
        while (limit := recent_history.page_limit(progress, 15)):
            sizes.append(limit)
            progress = recent_history.advance(progress, list(range(limit)))
            validate_checkpoint(progress)
        self.assertEqual(sizes, [15, 15, 15, 5])
        self.assertEqual(progress['recent_messages'], 50)

    def test_internal_app_uses_one_fifty_message_page(self):
        self.assertEqual(recent_history.page_limit({}, 200), 50)

    def test_polling_same_boundary_cannot_expand_or_reset_the_running_batch(self):
        progress = {'recent_messages': 50, 'cursor': 'opaque-slack-cursor'}
        first = recent_history.request_batch(progress, 'a' * 64)
        self.assertEqual(first['requested_messages'], 100)
        self.assertEqual(first['cursor'], progress['cursor'])
        shared = recent_history.request_batch(first, 'b' * 64)
        self.assertEqual(shared['requested_messages'], 100)
        finished = {**shared, 'recent_messages': 100}
        for key in ('a' * 64, 'b' * 64) * 5:
            self.assertEqual(recent_history.request_batch(finished, key), finished)
            self.assertFalse(recent_history.request_pending(finished, key))
        self.assertEqual(recent_history.request_batch(finished, 'c' * 64)['requested_messages'], 150)

    def test_exhausted_source_never_reopens_from_a_new_boundary(self):
        progress = {'recent_messages': 8, 'timeline_complete': True}
        self.assertEqual(recent_history.request_batch(progress, 'new', complete=True), progress)

    def test_head_does_not_start_a_deep_import_before_archive_preload(self):
        state = SimpleNamespace(private_conversation_id=1, private_conversation=SimpleNamespace(oldest_synced_ts=''))
        self.assertIsNone(recent_history.imported_floor(state, 100))
        state.private_conversation.oldest_synced_ts = '1000.000001'
        self.assertEqual(recent_history.imported_floor(state, 100), 1000)
        self.assertEqual(recent_history.imported_floor(state, 2000), 2000)

    @override_settings(MESSAGE_SYNC_ENABLED=False)
    def test_legacy_engine_keeps_existing_archive_behavior(self):
        self.assertFalse(recent_history.enabled())
        self.assertEqual(recent_history.private_page_limit(Mock(), 200), 200)
        self.assertEqual(recent_history.imported_floor(Mock(), 100), 100)

    def test_explicit_request_does_not_bypass_backoff_or_steal_a_lease(self):
        for lease, backoff, error in (('held', 0, ''), (None, 60, 'BudgetDeferred'), (None, 0, 'network')):
            job = Mock(lease_token=lease, backoff_seconds=backoff, last_error_code=error)
            api._wake(job)
            job.save.assert_not_called()

    def test_demand_recorded_during_a_public_page_survives_its_lease_commit(self):
        from integrations.services.message_sync import scheduler
        old = {'recent_messages': 50, 'cursor': 'opaque-50', 'phase': 'recent_paused'}
        current = recent_history.request_batch(old, 'a' * 64)
        job = SimpleNamespace(checkpoint=current, save=Mock())
        state = SimpleNamespace(save=Mock())
        lease = SimpleNamespace(kind='archive')
        with patch.object(scheduler.transaction, 'atomic', side_effect=nullcontext), patch.object(
            scheduler, 'locked_job', return_value=(state, job)
        ):
            scheduler.finish_job(lease, checkpoint=old, delay_seconds=86400)
        self.assertEqual(job.checkpoint['cursor'], 'opaque-50')
        self.assertEqual(job.checkpoint['requested_messages'], 100)
        self.assertTrue(recent_history.request_pending(job.checkpoint, 'a' * 64))
        self.assertEqual(job.checkpoint['phase'], 'recent_import')
        validate_checkpoint(job.checkpoint)

    def test_demand_metadata_is_bounded_and_contains_only_hashed_boundaries(self):
        import json
        progress = {'recent_messages': 50}
        for number in range(40):
            progress = recent_history.request_batch(progress, f'{number:064x}')
            progress['recent_messages'] = progress['requested_messages']
            validate_checkpoint(progress)
        self.assertEqual(len(json.loads(progress['demand_targets'])), 16)
        self.assertLess(len(progress['demand_targets']), 1000)

    def test_scroll_during_initial_preload_reserves_the_next_fifty(self):
        progress = recent_history.request_batch({'recent_messages': 15}, 'a' * 64)
        self.assertEqual(progress['requested_messages'], 100)
        self.assertTrue(recent_history.request_pending({**progress, 'recent_messages': 50}, 'a' * 64))


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True, MESSAGE_SYNC_SLACK_DISTRIBUTION='restricted')
class RecentPublicWorkerTests(SimpleTestCase):
    now = 1_800_000_000

    def dispatch(self, responses, *, checkpoint=None):
        checkpoint = dict(checkpoint or {})
        channel = SimpleNamespace(pk=1, audience="community", enabled=True, slack_workspace_id='T1', slack_channel_id='C1',
                                  destination_workspace_id='community', destination_channel_id='room')
        state = SimpleNamespace(public_channel=channel, authority_generation=1, verified_ranges={}, save=Mock())
        lease = SimpleNamespace(kind='archive', checkpoint=checkpoint)
        client = Mock()
        client.conversations_history.side_effect = responses
        calls = []

        def finish(_lease, **kwargs):
            calls.append(kwargs)
            lease.checkpoint = kwargs['checkpoint']

        with ExitStack() as stack:
            for target, options in (
                ('integrations.services.message_sync.history.transaction.atomic', {'side_effect': nullcontext}),
                ('integrations.services.message_sync.history.locked_job', {'return_value': (state, Mock())}),
                ('integrations.services.message_sync.history.CommunityBridgeChannel.objects.select_for_update', {}),
                ('integrations.services.message_sync.history.SlackDmMirrorConversation.objects.filter', {}),
                ('integrations.services.message_sync.member_history.guard_page', {}),
                ('integrations.services.message_sync.history.ingest_slack_event', {}),
                ('integrations.services.message_sync.public_repair.repair_observed_message', {}),
                ('integrations.services.message_sync.read_activity.advance_public_activity', {}),
                ('integrations.services.message_sync.history.finish_job', {'side_effect': finish}),
                ('integrations.services.message_sync.history.time.time', {'return_value': self.now}),
            ):
                mock = stack.enter_context(patch(target, **options))
                if target.endswith('select_for_update'):
                    mock.return_value.get.return_value = channel
                elif target.endswith('SlackDmMirrorConversation.objects.filter'):
                    private_owner = mock
                    private_owner.return_value.exists.return_value = False
            for _ in responses:
                history._public_page(lease, state, client=client, member_authority="synthetic-member-scope")
            # The idle tick must preserve progress and perform no provider I/O.
            history._public_page(lease, state, client=client, member_authority="synthetic-member-scope")
        private_owner.assert_called_with(slack_workspace_id='T1', slack_conversation_id='C1')
        self.assertEqual(private_owner.call_count, len(responses))
        self.assertEqual(private_owner.return_value.exists.call_count, len(responses))
        return lease, client, state, calls

    def test_preload_stops_at_fifty_with_cursor_and_incomplete_archive_proof(self):
        responses, offset = [], 0
        for count in (15, 15, 15, 5):
            rows = [{'ts': f'{self.now - offset - i - 1}.000001', 'user': 'U1', 'text': 'synthetic'} for i in range(count)]
            offset += count
            responses.append({'ok': True, 'messages': rows, 'response_metadata': {'next_cursor': f'cursor-{offset}'}})
        lease, client, state, calls = self.dispatch(responses)
        self.assertEqual([call.kwargs['limit'] for call in client.conversations_history.call_args_list], [15, 15, 15, 5])
        self.assertEqual(lease.checkpoint['recent_messages'], 50)
        self.assertEqual(lease.checkpoint['cursor'], 'cursor-50')
        self.assertEqual(state.verified_ranges['archive']['classification'], 'incomplete')
        self.assertEqual(calls[-1]['delay_seconds'], 86400)
        self.assertNotIn('text', repr(lease.checkpoint))
        validate_checkpoint(lease.checkpoint)

    def test_next_requested_batch_resumes_the_same_source_cursor(self):
        start = recent_history.request_batch({'recent_messages': 50, 'cursor': 'cursor-50'}, 'a' * 64)
        rows = [{'ts': f'{self.now - 100 - i}.000001', 'user': 'U1'} for i in range(7)]
        lease, client, state, _ = self.dispatch([{'ok': True, 'messages': rows}], checkpoint=start)
        self.assertEqual(client.conversations_history.call_args.kwargs['cursor'], 'cursor-50')
        self.assertTrue(lease.checkpoint['timeline_complete'])
        self.assertEqual(lease.checkpoint['recent_messages'], 57)
        self.assertEqual(state.verified_ranges['archive']['classification'], 'accessible_range')

    def test_empty_source_is_terminal_without_manufacturing_fifty_messages(self):
        lease, client, state, _ = self.dispatch([{'ok': True, 'messages': []}])
        self.assertTrue(lease.checkpoint['timeline_complete'])
        self.assertEqual(client.conversations_history.call_count, 1)
        self.assertEqual(state.verified_ranges['archive']['classification'], 'empty_accessible_range')

    def test_limited_source_retains_its_qualification(self):
        _, _, state, _ = self.dispatch([{'ok': True, 'messages': [], 'is_limited': True}])
        self.assertEqual(state.verified_ranges['archive']['classification'], 'source_limited')


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True)
class RecentPrivateWorkerTests(SimpleTestCase):
    def test_main_timeline_preload_does_not_expand_threads_before_other_chats(self):
        conversation = SimpleNamespace(pk=1, grant=Mock(), slack_conversation_id='D1')
        authority = Mock()
        scan = SimpleNamespace(oldest='1700000000')
        marker = SimpleNamespace(metadata={'recent_messages': 45, 'upper_bound': '1800000000.999999'})
        with patch.object(dm, '_capture_slack_grant_api_authority', return_value=authority), patch.object(
            dm, '_history_required_scopes', return_value={'im:history'}
        ), patch.object(dm, '_prepare_history_scan_page', return_value=(scan, 'D1', '1799999900.000001', None, False)) as prepare, patch.object(
            recent_history, 'private_progress', return_value=marker
        ), patch.object(dm, '_call_slack_with_grant_authority', return_value={'messages': []}) as fetch, patch.object(dm, '_persist_history_page', return_value=0):
            dm._enqueue_history_page(conversation, conversation.grant, timeline_only=True)
        self.assertTrue(prepare.call_args.kwargs['main_first'])
        self.assertEqual(fetch.call_args.kwargs['limit'], 5)
        self.assertEqual(fetch.call_args.kwargs['latest'], '1799999900.000001')
        self.assertEqual(fetch.call_args.kwargs['oldest'], '1700000000')

    def test_paused_private_archive_does_not_call_slack_again(self):
        conversation = SimpleNamespace(pk=1, grant=Mock(), slack_conversation_id='D1',
                                       history_backfilled_at=None, refresh_from_db=Mock())
        state = SimpleNamespace(private_conversation=conversation)
        lease = SimpleNamespace(kind='archive')
        with patch.object(dm, '_enqueue_history_page') as page, patch.object(
            recent_history, 'private_progress', return_value=SimpleNamespace(metadata={'recent_messages': 50})
        ), patch.object(private_history, 'finish_job') as finish:
            private_history.private_page(lease, state)
        page.assert_not_called()
        self.assertFalse(finish.call_args.kwargs['complete'])
        self.assertEqual(finish.call_args.kwargs['delay_seconds'], 86400)

    def test_private_batch_waits_for_acknowledged_delivery_and_coalesces_other_devices(self):
        marker = SimpleNamespace(metadata={'recent_messages': 50, 'complete': False}, save=Mock())
        conversation = SimpleNamespace(history_backfilled_at=None)
        job = SimpleNamespace(pk=9, lease_token=None, backoff_seconds=0, last_error_code='', save=Mock())
        state = Mock(status='syncing')
        state.jobs.select_for_update.return_value.get.return_value = job
        status = {'queued_messages': 1}
        with patch.object(recent_history, 'private_progress', return_value=marker), patch.object(
            api, 'ensure_state', return_value=state
        ), patch.object(api.BridgeSyncState.objects, 'select_for_update') as rows, patch.object(
            api, 'schedule_job', return_value=job
        ), patch('integrations.services.slack_chat_refresh._refresh_status', return_value=status):
            rows.return_value.get.return_value = state
            self.assertTrue(api._private_progress(conversation, 'a' * 64)['pending'])
            self.assertEqual(marker.metadata['requested_messages'], 100)
            api._private_progress(conversation, 'b' * 64)
            self.assertEqual(marker.metadata['requested_messages'], 100)
            marker.metadata['recent_messages'] = 100
            self.assertTrue(api._private_progress(conversation, 'a' * 64)['pending'])
            status['queued_messages'] = 0
            result = api._private_progress(conversation, 'a' * 64)
            self.assertFalse(api._private_progress(conversation, 'b' * 64)['pending'])
            self.assertEqual(marker.metadata['requested_messages'], 100)
        self.assertEqual(result['has_more'], True)
        self.assertEqual(result['pending'], False)

    def test_invalid_request_cannot_schedule_or_read_a_conversation(self):
        for channel, key in (('not-a-uuid', 'a' * 64), ('00000000-0000-4000-8000-000000000000', 'body')):
            with self.subTest(channel=channel), patch.object(api, 'ensure_state') as state:
                with self.assertRaises(api.InventoryError):
                    api.request_history_page(Mock(), public_key='a' * 64, channel_id=channel, request_key=key)
                state.assert_not_called()


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True)
class RecentHistoryAccessTests(SimpleTestCase):
    channel = '00000000-0000-4000-8000-000000000001'

    def user(self):
        from django.contrib.auth import get_user_model
        return get_user_model()(pk=1, is_active=True)

    def test_unverified_device_cannot_schedule_even_a_public_bridge(self):
        from django.db.models.query import QuerySet
        with patch('community_chat.onboarding.require_community_access'), patch.object(
            QuerySet, 'exists', return_value=False
        ), patch.object(api, 'ensure_state') as state:
            with self.assertRaises(api.InventoryError) as caught:
                api.request_history_page(self.user(), public_key='a' * 64, channel_id=self.channel, request_key='b' * 64)
        self.assertEqual(caught.exception.code, 'device_unverified')
        state.assert_not_called()

    def test_private_room_still_requires_the_exact_owner_and_device(self):
        from django.db.models.query import QuerySet
        from rest_framework.exceptions import NotFound
        with patch('community_chat.onboarding.require_community_access'), patch.object(
            QuerySet, 'exists', return_value=True
        ), patch.object(QuerySet, 'first', return_value=None), patch(
            'integrations.services.slack_chat_refresh._authorized_conversation', side_effect=NotFound('wrong owner')
        ), patch.object(api, 'ensure_state') as state:
            with self.assertRaises(NotFound):
                api.request_history_page(self.user(), public_key='a' * 64, channel_id=self.channel, request_key='b' * 64)
        state.assert_not_called()

    def test_stale_audience_count_cannot_pause_a_replacement_private_room(self):
        marker = SimpleNamespace(metadata={'participant_hash': 'old', 'mlai_channel_id': 'old-room',
                                           'history_days': 30, 'recent_messages': 50})
        conversation = Mock(mlai_channel_id='new-room')
        conversation.deliveries.filter.return_value.first.return_value = marker
        with patch.object(dm, '_history_participant_boundary', return_value='new'), patch.object(dm, '_grant_history_days', return_value=30):
            self.assertIsNone(recent_history.private_progress(conversation))
            self.assertEqual(recent_history.private_page_limit(conversation, 200), 50)

    def test_empty_import_still_allows_future_head_repairs(self):
        from datetime import datetime, timezone
        state = SimpleNamespace(private_conversation_id=1, private_conversation=SimpleNamespace(
            oldest_synced_ts='', history_backfilled_at=datetime.fromtimestamp(1000, timezone.utc)))
        self.assertEqual(recent_history.imported_floor(state, 100), 1000)

    @override_settings(MESSAGE_SYNC_RECENT_HISTORY_ENABLED=False)
    def test_rollout_switch_preserves_full_history_for_older_clients(self):
        self.assertFalse(recent_history.enabled())


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_RECENT_HISTORY_ENABLED=True)
class RecentHistoryStatusTests(SimpleTestCase):
    def test_idle_preload_does_not_certify_full_archive_or_hide_delivery_errors(self):
        from integrations.services.slack_chat_refresh import _refresh_status
        for pending, failed, expected in ((0, 0, 'complete'), (1, 0, 'syncing'), (0, 1, 'error')):
            with self.subTest(pending=pending, failed=failed):
                rows = Mock()
                rows.filter.return_value = rows
                rows.exclude.return_value = rows
                rows.aggregate.return_value = {'imported_messages': 50, 'queued_messages': pending, 'failed_messages': failed}
                conversation = SimpleNamespace(deliveries=rows, participant_hash='audience', mlai_channel_id='room',
                    history_backfilled_at=None, last_error='', grant=SimpleNamespace(history_days=30))
                sync = SimpleNamespace(verified_ranges={'head': {'classification': 'incomplete'}}, last_error_code='')
                marker = SimpleNamespace(metadata={'complete': False, 'recent_messages': 50})
                with patch.object(api.BridgeSyncState.objects, 'filter') as states, patch.object(
                    recent_history, 'private_progress', return_value=marker
                ), patch('integrations.services.slack_chat_catalog.conversation_metadata', return_value={}):
                    states.return_value.first.return_value = sync
                    result = _refresh_status(conversation)
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['history_preload_complete'], expected == 'complete')
                self.assertFalse(result['history_scan_complete'])
                self.assertEqual(result['source_coverage']['classification'], 'incomplete')
