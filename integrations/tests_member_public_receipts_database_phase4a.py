"""Disposable DB coverage of cross-app receipt/outbox uniqueness.

Run only through the separately approved migration-inventory test runner.
"""
from copy import deepcopy

from django.test import TestCase, override_settings

from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery, CommunityBridgeReceipt
from integrations.services.community_bridge import store
from integrations.tests_member_public_receipts_phase4a import envelope, event_for


@override_settings(MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True,
                   SLACK_MEMBER_CHANNELS_ENABLED=True, MESSAGE_SYNC_SLACK_APP_ID='ABOT',
                   MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER',
                   MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID='TTESTMEMBERS', SLACK_MEMBER_CHANNEL_ALLOWED_IDS='')
class PublicReceiptOutboxDatabaseTests(TestCase):
    def setUp(self):
        CommunityBridgeChannel.objects.create(slack_workspace_id='TTESTMEMBERS', slack_channel_id='CTESTMEMBERS',
            audience='community', destination_platform='buzz',
            destination_channel_id='00000000-0000-0000-0000-000000000002', enabled=True)

    def test_bot_and_member_copies_enqueue_each_operation_once(self):
        for index, operation in enumerate(('create', 'edit', 'delete', 'reaction_add', 'reaction_remove')):
            with self.subTest(operation=operation):
                first = store.ingest_slack_event(envelope(event_for(operation), event_id=f'EUSER{index}'))
                second = store.ingest_slack_event(envelope(event_for(operation), app='ABOT', event_id=f'EBOT{index}'))
                self.assertEqual(first['status'], 'enqueued')
                self.assertEqual(second['status'], 'duplicate')
                self.assertEqual(first['receipt_id'], second['receipt_id'])
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 5)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 5)

    def test_later_edit_and_reaction_readd_are_not_hidden_by_first_receipt(self):
        first_edit = envelope(event_for('edit'))
        second_edit = deepcopy(first_edit)
        second_edit['event']['message']['edited']['ts'] = '1700000003.000003'
        added = envelope(event_for('reaction_add'))
        removed = envelope(event_for('reaction_remove'))
        readded = deepcopy(added)
        readded['event']['event_ts'] = '1700000005.000005'
        for candidate in (first_edit, second_edit, added, removed, readded):
            self.assertEqual(store.ingest_slack_event(candidate)['status'], 'enqueued')
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 5)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 5)
