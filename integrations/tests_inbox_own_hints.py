"""Owner hints preserve recipient authority and public author probe scope."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings
from integrations.services.message_sync import read_priority as priority


@override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=True)
class OwnHintTests(SimpleTestCase):
    def test_own_post_prompts_a_probe_without_retaining_content(self):
        grant = SimpleNamespace(slack_user_id='UOWNER')
        query = MagicMock()
        query.filter.return_value.order_by.return_value = [grant]
        payload = {'team_id': 'TTEST', 'authorizations': [{'user_id': 'UOWNER'}],
                   'event': {'type': 'message', 'channel': 'D1', 'user': 'UOWNER', 'text': 'secret'}}
        with patch('integrations.models.SlackDmMirrorGrant.objects.select_related', return_value=query), patch(
            'integrations.services.slack_chat_read_state._capture_slack_grant_api_authority', return_value='authority'
        ), patch.object(priority, 'enqueue_refresh') as enqueue:
            priority.invalidate_event(payload)
        query.filter.assert_called_once_with(slack_workspace_id='TTEST', slack_user_id__in={'UOWNER'},
            status='active', revoked_at__isnull=True, connection__status__in=('connected', 'syncing'))
        self.assertEqual(enqueue.call_args.kwargs, {'reason': 'own_message'})
        self.assertNotIn('secret', str(enqueue.call_args))

    def test_own_hint_survives_budget_pause_and_visibility_merges(self):
        hints = priority.merged_hints({}, ['D1'], now=100, reason='own_message')
        self.assertTrue(priority.hint_pending(hints['D1'], 1000))
        merged = priority.merged_hints(hints, ['D1'], now=110, reason='visible')
        self.assertEqual(merged['D1']['reason'], 'own_message')
        self.assertEqual(merged['D1']['generation'], hints['D1']['generation'])

    def test_activity_renews_generation_without_losing_visible_priority(self):
        first = priority.merged_hints({}, ['D1'], now=100, reason='visible')
        second = priority.merged_hints(first, ['D1'], now=110, reason='activity')
        self.assertEqual(second['D1']['reason'], 'visible')
        self.assertNotEqual(first['D1']['generation'], second['D1']['generation'])
        self.assertTrue(priority.hint_pending(second['D1'], 1000))

    @override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=False)
    def test_disabled_merge_keeps_legacy_hint_shape(self):
        hint = priority.merged_hints({}, ['D1'], now=100, reason='visible')['D1']
        self.assertNotIn('dirty', hint)
        self.assertFalse(priority.hint_pending(hint, 1000))


@override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=True)
class PublicOwnHintTests(SimpleTestCase):
    def run_event(self, *, mapped=True, authorizations=None, **event_overrides):
        grants = [SimpleNamespace(slack_user_id='UOWNER'), SimpleNamespace(slack_user_id='URECIPIENT')]
        query = MagicMock()
        query.filter.side_effect = lambda **kw: SimpleNamespace(
            order_by=lambda *_args: [grant for grant in grants if grant.slack_user_id in kw['slack_user_id__in']])
        mapping = MagicMock()
        mapping.filter.return_value.exclude.return_value.exists.return_value = mapped
        event = {'type': 'message', 'channel': 'C1', 'channel_type': 'channel',
                 'user': 'UOWNER', 'thread_ts': '100.000001', 'text': 'do not retain this body',
                 **event_overrides}
        payload = {'team_id': 'TTEST', 'authorizations': authorizations if authorizations is not None else [{'user_id': 'URECIPIENT'}], 'event': event}
        with patch('integrations.models.SlackDmMirrorGrant.objects.select_related', return_value=query), patch(
            'integrations.models.CommunityBridgeChannel.objects', mapping
        ), patch('integrations.services.slack_chat_read_state._capture_slack_grant_api_authority',
                 side_effect=lambda grant: grant.slack_user_id), patch.object(priority, 'enqueue_refresh') as enqueue:
            priority.invalidate_event(payload)
        return query, mapping, enqueue

    def test_public_own_reply_without_recipient_authorization_prompts_probe(self):
        query, mapping, enqueue = self.run_event()
        self.assertEqual(query.filter.call_args.kwargs['slack_user_id__in'], {'UOWNER', 'URECIPIENT'})
        mapping.filter.assert_called_once_with(slack_workspace_id='TTEST', slack_channel_id='C1',
                                               destination_platform='buzz', enabled=True)
        mapping.filter.return_value.exclude.assert_called_once_with(destination_channel_id='')
        self.assertEqual(enqueue.call_args_list[0].kwargs, {'reason': 'own_message'})
        self.assertEqual(enqueue.call_args_list[1].kwargs, {'reason': 'activity'})
        self.assertNotIn('do not retain this body', str(enqueue.call_args_list))

    def test_public_author_with_no_user_recipients_still_prompts_one_probe(self):
        query, _, enqueue = self.run_event(authorizations=[])
        self.assertEqual(query.filter.call_args.kwargs['slack_user_id__in'], {'UOWNER'})
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.kwargs, {'reason': 'own_message'})

    def test_private_missing_type_unmapped_bot_edit_and_delete_do_not_add_author(self):
        cases = [({'channel_type': 'group'}, True), ({'channel_type': 'im'}, True),
                 ({'channel_type': 'mpim'}, True), ({'channel_type': None}, True),
                 ({'bot_id': 'B1'}, True), ({'subtype': 'message_changed'}, True),
                 ({'subtype': 'message_deleted'}, True), ({'user': ''}, True), ({}, False)]
        for event, mapped in cases:
            with self.subTest(event=event, mapped=mapped):
                query, _, _ = self.run_event(mapped=mapped, **event)
                self.assertEqual(query.filter.call_args.kwargs['slack_user_id__in'], {'URECIPIENT'})

    @override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=False)
    def test_disabled_public_author_path_keeps_explicit_recipients(self):
        query, mapping, _ = self.run_event()
        self.assertEqual(query.filter.call_args.kwargs['slack_user_id__in'], {'URECIPIENT'})
        mapping.filter.assert_not_called()

    def test_public_author_still_requires_current_active_grant(self):
        query, _, _ = self.run_event()
        self.assertEqual(query.filter.call_args.kwargs,
                         {'slack_workspace_id': 'TTEST', 'slack_user_id__in': {'UOWNER', 'URECIPIENT'},
                          'status': 'active', 'revoked_at__isnull': True,
                          'connection__status__in': ('connected', 'syncing')})
