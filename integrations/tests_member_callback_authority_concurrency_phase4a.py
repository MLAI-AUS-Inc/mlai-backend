"""PostgreSQL-only callback/account/channel lock regressions.

Run only with the approved disposable migration inventory. No provider calls.
"""
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from integrations.models import (
    BridgeSyncInbox, CommunityBridgeChannel, CommunityBridgeReceipt,
    ExternalServiceConnection, SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync import inbox, member_callback_authority as fence


@skipUnless(connection.vendor == 'postgresql', 'Real row locks require PostgreSQL')
class CallbackAccountChannelConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.low = get_user_model().objects.create_user(email='callback-low@example.com')
        self.high = get_user_model().objects.create_user(email='callback-high@example.com')
        self.low_connection = ExternalServiceConnection.objects.create(user=self.low, provider='slack', access_token='synthetic-no-provider')
        high_connection = ExternalServiceConnection.objects.create(user=self.high, provider='slack', access_token='synthetic-no-provider')
        SlackDmMirrorGrant.objects.create(user=self.high, connection=high_connection,
            slack_workspace_id='TTESTCALLBACK', slack_user_id='URECIPIENT', consented_at=timezone.now())
        self.channel = CommunityBridgeChannel.objects.create(slack_workspace_id='TTESTCALLBACK',
            slack_channel_id='CTESTCALLBACK', audience='members', destination_platform='buzz', enabled=False)

    def queue(self):
        inbox.enqueue_slack_callback({'api_app_id': 'ATESTCALLBACK', 'team_id': 'TTESTCALLBACK', 'event_id': 'ETESTCALLBACK',
            'event': {'type': 'message', 'channel': 'CTESTCALLBACK', 'channel_type': 'channel',
                      'ts': '1700000000.000001', 'user': 'UACTOR', 'text': 'synthetic'},
            'authorizations': [{'team_id': 'TTESTCALLBACK', 'user_id': 'URECIPIENT'}]})

    def threaded(self, target, errors):
        def run():
            close_old_connections()
            try:
                target()
            except BaseException as exc:
                errors.append(type(exc).__name__)
            finally:
                connection.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_sorted_callback_users_precede_channel_and_do_not_cycle_with_audience_order(self):
        SlackDmMirrorGrant.objects.create(user=self.low, connection=self.low_connection,
            slack_workspace_id='TTESTCALLBACK', slack_user_id='UACTOR', consented_at=timezone.now())
        self.queue()
        low_locked, snapshot_seen, audience_done = (threading.Event() for _ in range(3))
        errors, outcomes = [], []
        original = fence.relevant_user_ids

        def snapshot(payload):
            result = original(payload)
            snapshot_seen.set()
            return result

        def audience_order():
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.low.pk)
                low_locked.set()
                if not snapshot_seen.wait(5):
                    raise AssertionError('callback snapshot timed out')
                get_user_model().objects.select_for_update().get(pk=self.high.pk)
                CommunityBridgeChannel.objects.select_for_update().get(pk=self.channel.pk)
            audience_done.set()

        def hint(_):
            self.assertTrue(audience_done.is_set())
            get_user_model().objects.select_for_update().get(pk=self.high.pk)

        def activity(_):
            CommunityBridgeChannel.objects.select_for_update().get(pk=self.channel.pk)

        with patch.object(fence, 'relevant_user_ids', side_effect=snapshot), \
             patch('integrations.services.message_sync.read_priority.invalidate_event', side_effect=hint), \
             patch('integrations.services.message_sync.read_activity.record_public_event', side_effect=activity), \
             patch('integrations.services.community_bridge.member_channel_events.dispatch_slack_callback', return_value={'status': 'ignored'}):
            audience = self.threaded(audience_order, errors)
            self.assertTrue(low_locked.wait(5))
            callback = self.threaded(lambda: outcomes.append(inbox.process_inbox_once()), errors)
            try:
                callback.join(10)
                audience.join(10)
            finally:
                snapshot_seen.set()
        self.assertFalse(callback.is_alive())
        self.assertFalse(audience.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [1])
        self.assertEqual(BridgeSyncInbox.objects.get().status, 'completed')

    def test_new_lower_actor_after_channel_is_retried_before_any_new_user_lock(self):
        self.queue()
        channel_locked, actor_ready = threading.Event(), threading.Event()
        errors, outcomes = [], []

        def activity(_):
            CommunityBridgeChannel.objects.select_for_update().get(pk=self.channel.pk)
            channel_locked.set()
            if not actor_ready.wait(5):
                raise AssertionError('new actor timed out')

        def add_then_hold_actor():
            if not channel_locked.wait(5):
                raise AssertionError('callback channel timed out')
            with transaction.atomic():
                SlackDmMirrorGrant.objects.create(user=self.low, connection=self.low_connection,
                    slack_workspace_id='TTESTCALLBACK', slack_user_id='UACTOR', consented_at=timezone.now())
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.low.pk)
                actor_ready.set()
                CommunityBridgeChannel.objects.select_for_update().get(pk=self.channel.pk)

        def dispatch(_, *, private_ingest=None, public_ingest=None):
            self.assertIsNone(private_ingest)
            self.assertIsNone(public_ingest)
            CommunityBridgeReceipt.objects.create(platform='slack', receipt_key='atomic-rollback-proof')
            grant = SlackDmMirrorGrant.objects.get(slack_workspace_id='TTESTCALLBACK', slack_user_id='UACTOR')
            dm._capture_slack_grant_api_authority(grant)  # Must reject before refreshing or locking User.
            raise AssertionError('new actor unexpectedly accepted')

        with patch('integrations.services.message_sync.read_priority.invalidate_event'), \
             patch('integrations.services.message_sync.read_activity.record_public_event', side_effect=activity), \
             patch('integrations.services.community_bridge.member_channel_events.dispatch_slack_callback', side_effect=dispatch), \
             patch.object(dm, '_refresh_slack_grant_token_if_due', side_effect=AssertionError('provider refresh forbidden')) as refresh:
            actor = self.threaded(add_then_hold_actor, errors)
            callback = self.threaded(lambda: outcomes.append(inbox.process_inbox_once()), errors)
            try:
                callback.join(10)
                actor.join(10)
            finally:
                actor_ready.set()
                channel_locked.set()
        self.assertFalse(callback.is_alive())
        self.assertFalse(actor.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [0])
        refresh.assert_not_called()
        row = BridgeSyncInbox.objects.get()
        self.assertEqual((row.status, row.last_error_code), ('pending', 'CallbackAuthorityChanged'))
        self.assertTrue(row.encrypted_payload)
        self.assertFalse(CommunityBridgeReceipt.objects.filter(receipt_key='atomic-rollback-proof').exists())
