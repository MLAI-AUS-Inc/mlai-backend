"""Source-derived receipt identity preserves revisions across app callbacks."""
from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from integrations.services.community_bridge import store

TS, REVISION = '1700000000.000001', '1700000001.000002'


def envelope(event, app='AUSER', event_id='EONE'):
    return {'api_app_id': app, 'team_id': 'TTESTMEMBERS', 'event_id': event_id, 'event': event}


def event_for(operation):
    base = {'type': 'message', 'channel': 'CTESTMEMBERS', 'channel_type': 'channel',
            'ts': TS, 'user': 'UAUTHOR', 'text': 'synthetic', 'event_ts': REVISION}
    if operation == 'edit':
        base.update(subtype='message_changed', message={'ts': TS, 'user': 'UAUTHOR', 'text': 'changed', 'edited': {'ts': REVISION}})
    elif operation == 'delete':
        base.update(subtype='message_deleted', deleted_ts=TS, previous_message={'ts': TS, 'user': 'UAUTHOR'})
    elif operation.startswith('reaction'):
        base = {'type': 'reaction_added' if operation == 'reaction_add' else 'reaction_removed',
                'item': {'type': 'message', 'channel': 'CTESTMEMBERS', 'channel_type': 'channel', 'ts': TS},
                'user': 'UREACTOR', 'reaction': 'thumbsup', 'event_ts': REVISION}
    return base


class PublicReceiptIdentityTests(SimpleTestCase):
    def key(self, payload):
        normalized = store._normalize_slack_event(payload)
        self.assertIsNotNone(normalized)
        return store._public_slack_receipt_key(payload, normalized)

    def test_app_copies_dedupe_each_content_family(self):
        for operation in ('create', 'edit', 'delete', 'reaction_add', 'reaction_remove'):
            with self.subTest(operation=operation):
                user = envelope(event_for(operation))
                bot = envelope(event_for(operation), app='ABOT', event_id='EDIFFERENT')
                self.assertEqual(self.key(user), self.key(bot))
                self.assertLessEqual(len(self.key(user)), 255)
                self.assertNotIn('synthetic', self.key(user))

    def test_edit_revisions_and_reaction_transitions_remain_distinct(self):
        first = envelope(event_for('edit'))
        second = deepcopy(first)
        second['event']['message']['edited']['ts'] = '1700000002.000003'
        self.assertNotEqual(self.key(first), self.key(second))
        first = envelope(event_for('reaction_add'))
        removed = envelope(event_for('reaction_remove'))
        readded = deepcopy(first)
        readded['event']['event_ts'] = '1700000003.000004'
        self.assertEqual(len({self.key(first), self.key(removed), self.key(readded)}), 3)

    def test_thread_broadcast_promotion_is_not_lost_to_create_dedupe(self):
        first = envelope(event_for('create'))
        broadcast = deepcopy(first)
        broadcast['event'].update(subtype='thread_broadcast', thread_ts='1699999999.000001')
        self.assertNotEqual(self.key(first), self.key(broadcast))

    def test_content_and_callback_retry_ids_cannot_split_create_identity(self):
        first = envelope(event_for('create'))
        second = deepcopy(first)
        second.update(event_id='ERETRY')
        second['event'].update(text='unfurl changed', event_ts='1700000005.000006')
        self.assertEqual(self.key(first), self.key(second))

    def test_workspace_channel_actor_and_reaction_are_part_of_identity(self):
        original = envelope(event_for('reaction_add'))
        variants = []
        for key, value in [('user', 'UOTHER'), ('reaction', 'heart')]:
            candidate = deepcopy(original)
            candidate['event'][key] = value
            variants.append(candidate)
        candidate = deepcopy(original)
        candidate['event']['item']['channel'] = 'COTHER'
        variants.append(candidate)
        variants.append(dict(original, team_id='TOTHER'))
        keys = {self.key(candidate) for candidate in [original, *variants]}
        self.assertEqual(len(keys), 5)

    def test_malformed_or_oversized_identity_is_not_coerced(self):
        original = envelope(event_for('reaction_add'))
        normalized = store._normalize_slack_event(original)
        for candidate, norm in [
            (dict(original, team_id=[]), normalized),
            (dict(original, team_id='T' * 100), normalized),
            (original, dict(normalized, source_channel_id='GPRIVATE')),
            (dict(original, event=dict(original['event'], event_ts=True)), normalized),
            (dict(original, event=dict(original['event'], event_ts='NaN')), normalized),
            (dict(original, event=dict(original['event'], reaction='x' * 65)), normalized),
            (dict(original, event=dict(original['event'], user='CRECIPIENT')), normalized),
        ]:
            with self.subTest(candidate=candidate):
                self.assertIsNone(store._public_slack_receipt_key(candidate, norm))

    @override_settings(MESSAGE_SYNC_SLACK_APP_ID='ABOT', MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER')
    def test_flag_off_preserves_exact_legacy_receipt_key(self):
        candidate = envelope(event_for('create'))
        with patch('integrations.services.slack_member_access.enabled', return_value=False), patch.object(store, 'ingest_inbound_event', return_value={}) as ingest:
            store.ingest_slack_event(candidate)
        self.assertEqual(ingest.call_args.kwargs['receipt_key'], 'EONE')

    @override_settings(MESSAGE_SYNC_SLACK_APP_ID='ABOT', MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER')
    def test_enabled_scoped_app_copies_receive_identical_receipt_keys(self):
        with patch('integrations.services.slack_member_access.enabled', return_value=True), patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=True), patch.object(store, 'ingest_inbound_event', return_value={}) as ingest:
            store.ingest_slack_event(envelope(event_for('create')))
            first = ingest.call_args.kwargs['receipt_key']
            store.ingest_slack_event(envelope(event_for('create'), app='ABOT', event_id='ETWO'))
            self.assertEqual(ingest.call_args.kwargs['receipt_key'], first)

    @override_settings(MESSAGE_SYNC_SLACK_APP_ID='ABOT', MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER')
    def test_scope_private_and_external_overrides_preserve_legacy_identity(self):
        with patch('integrations.services.slack_member_access.enabled', return_value=True), patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=False), patch.object(store, 'ingest_inbound_event', return_value={}) as ingest:
            store.ingest_slack_event(envelope(event_for('create')))
            self.assertEqual(ingest.call_args.kwargs['receipt_key'], 'EONE')
        with patch('integrations.services.slack_member_access.enabled', return_value=True), patch('integrations.services.community_bridge.member_channels.member_channel_in_scope', return_value=True):
            candidate = envelope(dict(event_for('create'), channel_type='group'))
            self.assertFalse(store._public_receipt_identity_enabled(candidate, store._normalize_slack_event(candidate), public_source_verified=True))
            candidate = dict(envelope(event_for('create')), is_ext_shared_channel=True)
            self.assertFalse(store._public_receipt_identity_enabled(candidate, store._normalize_slack_event(candidate), public_source_verified=True))
