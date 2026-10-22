"""Disposable DB regressions for durable Members callbacks awaiting audience.

The acknowledgement fixture is synthetic; this is not live adapter evidence.
"""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import BridgeSyncInbox, CommunityBridgeDelivery, CommunityBridgeReceipt, SlackChannelMembership
from integrations.services.community_bridge import member_channels as channels
from integrations.services.community_bridge.member_audiences import audience_hash
from integrations.services.message_sync import inbox
from integrations.tests_slack_member_access import MemberAccessFixture
from integrations.tests_member_public_receipts_phase4a import event_for


@override_settings(MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True,
    SLACK_MEMBER_CHANNELS_ENABLED=True, MESSAGE_SYNC_SLACK_USER_APP_ID='AUSER', MESSAGE_SYNC_SLACK_APP_ID='ABOT',
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID='TTEST', SLACK_MEMBER_CHANNEL_ALLOWED_IDS='',
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class PendingMemberCallbackDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.channel.destination_channel_id = str(uuid.uuid4())
        self.channel.slack_seen_at = timezone.now()
        self.channel.room_state = {'applied': {'audience': 'members'}}
        self.channel.save()

    def queue(self, operation='create', identity='EPENDING'):
        stamp = f'{timezone.now().timestamp():.6f}'
        event = event_for(operation)
        if event['type'] == 'message':
            event.update(channel='CROOM', event_ts=stamp)
            if operation == 'create':
                event.update(user=self.grant.slack_user_id, ts=stamp)
        else:
            event['item']['channel'] = 'CROOM'
        inbox.enqueue_slack_callback({'type': 'event_callback', 'team_id': 'TTEST', 'api_app_id': 'AUSER',
            'event_id': identity, 'event_time': int(timezone.now().timestamp()), 'event': event,
            'authorizations': [{'team_id': 'TTEST', 'user_id': self.grant.slack_user_id}]})

    def due(self):
        BridgeSyncInbox.objects.filter(status='pending').update(available_at=timezone.now() - timedelta(seconds=1))

    def acknowledge(self):
        self.channel.refresh_from_db()
        generation, digest = str(uuid.uuid4()), audience_hash({self.device.public_key})
        self.channel.audience_generation, self.channel.audience_hash = uuid.UUID(generation), digest
        self.channel.audience_sync_pending, self.channel.enabled = False, True
        self.channel.room_state = {**self.channel.room_state, 'audience_applied': {
            'generation': generation, 'hash': digest, 'workspace_id': 'TTEST', 'channel_id': 'CROOM',
            'destination_channel_id': self.channel.destination_channel_id}}
        self.channel.save()

    def test_first_message_commits_join_but_retains_same_receipt_until_ack_then_enqueues_once(self):
        self.queue()
        self.assertEqual(inbox.process_inbox_once(), 0)
        waiting = BridgeSyncInbox.objects.get()
        original_id = waiting.pk
        self.assertEqual((waiting.status, waiting.last_error_code), ('pending', 'member_audience_pending'))
        self.assertTrue(waiting.encrypted_payload)
        self.assertEqual(SlackChannelMembership.objects.get().state, 'joined')
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.acknowledge()
        self.due()
        self.assertEqual(inbox.process_inbox_once(), 1)
        waiting.refresh_from_db()
        self.assertEqual((waiting.pk, waiting.status, waiting.encrypted_payload), (original_id, 'completed', ''))
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)
        self.assertEqual(inbox.process_inbox_once(), 0)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)
        self.provider.assert_not_called()

    def test_newer_message_from_current_joined_member_does_not_wake_audience_again(self):
        self.join(self.grant, observed_at=timezone.now() - timedelta(seconds=5))
        self.acknowledge()
        before = SlackChannelMembership.objects.get().observed_at
        self.queue()
        with patch.object(channels, 'mark_member_channels_pending') as pending:
            self.assertEqual(inbox.process_inbox_once(), 1)
        pending.assert_not_called()
        current = SlackChannelMembership.objects.get()
        self.assertGreater(current.observed_at, before)
        self.assertEqual(current.source, 'message')
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.audience_sync_pending)
        self.assertTrue(self.channel.enabled)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_message_refresh_after_expired_probe_or_epoch_still_wakes_audience(self):
        from integrations.services.slack_member_membership_epoch import KEY

        self.grant.consented_at = timezone.now() - timedelta(minutes=30)
        self.grant.save(update_fields=('consented_at',))
        for index, expired_probe in enumerate((True, False)):
            SlackChannelMembership.objects.all().delete()
            observed = timezone.now() - timedelta(minutes=11 if expired_probe else 1)
            self.join(self.grant, source='probe' if expired_probe else 'event', observed_at=observed)
            self.grant.connection.sync_cursor = {} if expired_probe else {
                KEY: {'not_before': (timezone.now() - timedelta(seconds=1)).isoformat()}}
            self.grant.connection.save(update_fields=('sync_cursor',))
            self.acknowledge()
            self.queue(identity=f'EREFRESH{index}')
            with patch.object(channels, 'mark_member_channels_pending', wraps=channels.mark_member_channels_pending) as pending:
                self.assertEqual(inbox.process_inbox_once(), 0)
            pending.assert_called_once()
            self.channel.refresh_from_db()
            self.assertTrue(self.channel.audience_sync_pending)
            self.assertEqual(SlackChannelMembership.objects.get().source, 'message')
            BridgeSyncInbox.objects.all().update(available_at=timezone.now() + timedelta(hours=1))

    def test_pending_edits_deletes_and_reactions_are_retained_until_ack(self):
        self.join(self.grant)
        self.channel.audience_sync_pending = True
        self.channel.save()
        for index, operation in enumerate(('edit', 'delete', 'reaction_add', 'reaction_remove')):
            self.queue(operation, f'EPENDING{index}')
            self.assertEqual(inbox.process_inbox_once(), 0)
        self.assertEqual(BridgeSyncInbox.objects.filter(status='pending').count(), 4)
        self.assertTrue(all(row.encrypted_payload for row in BridgeSyncInbox.objects.all()))
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.acknowledge()
        self.due()
        for _ in range(4):
            self.assertEqual(inbox.process_inbox_once(), 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 4)
        self.assertTrue(all(not row.encrypted_payload for row in BridgeSyncInbox.objects.all()))

    def test_waiting_ciphertext_survives_flag_rollback_and_stale_metadata(self):
        self.queue()
        self.assertEqual(inbox.process_inbox_once(), 0)
        self.due()
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False):
            self.assertEqual(inbox.process_inbox_once(), 0)
        row = BridgeSyncInbox.objects.get()
        self.assertEqual((row.status, row.last_error_code), ('pending', 'member_audience_pending'))
        self.assertTrue(row.encrypted_payload)
        self.due()
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False):
            self.assertEqual(inbox.process_inbox_once(), 0)
        row.refresh_from_db()
        self.assertEqual((row.status, row.last_error_code), ('pending', 'member_audience_pending'))
        self.assertTrue(row.encrypted_payload)
        self.channel.slack_seen_at = timezone.now() - timedelta(seconds=301)
        self.channel.save(update_fields=('slack_seen_at',))
        self.due()
        self.assertEqual(inbox.process_inbox_once(), 0)
        row.refresh_from_db()
        self.assertTrue(row.encrypted_payload)
        self.assertEqual(row.status, 'pending')
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
