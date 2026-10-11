"""Owner hints preserve explicit Slack recipient and grant authority."""
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
