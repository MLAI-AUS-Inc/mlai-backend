"""Source-state repair preserves newer callbacks and partial-list uncertainty."""
from django.test import TransactionTestCase
from django.utils import timezone
from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery
from integrations.services.community_bridge.store import ingest_slack_event
from integrations.services.message_sync.public_repair import repair_observed_message


class PublicRepairTests(TransactionTestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(slack_workspace_id="T1", slack_channel_id="C1",
            destination_platform="buzz", destination_workspace_id="test.invalid", destination_channel_id="fixture")
        self.message = {"ts": "1700000000.000001", "user": "U1", "text": "original"}
        ingest_slack_event({"team_id": "T1", "event_id": "original", "event": {
            **self.message, "type": "message", "channel": "C1", "channel_type": "channel",
        }})

    def repair(self, **values):
        repair_observed_message(self.channel, {**self.message, **values}, read_started_at=timezone.now())

    def test_changed_content_without_edited_timestamp_repairs_once_and_can_change_back(self):
        self.repair(text="updated")
        self.repair(text="updated")
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="edit").count(), 1)
        self.repair()
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="edit").count(), 2)

    def test_callback_newer_than_source_read_wins(self):
        started = timezone.now()
        ingest_slack_event({"team_id": "T1", "event_id": "newer-callback", "event": {
            "type": "message", "subtype": "message_changed", "channel": "C1", "channel_type": "channel",
            "message": {**self.message, "text": "newer"}, "event_ts": "1700000001.000001",
        }})
        repair_observed_message(self.channel, self.message, read_started_at=started)
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="edit").count(), 1)

    def test_partial_reaction_users_add_known_users_without_removing_omitted_users(self):
        self.repair(reactions=[{"name": "thumbsup", "users": ["U1", "U2"], "count": 2}])
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="reaction_add").count(), 2)
        self.repair(reactions=[{"name": "thumbsup", "users": ["U1"], "count": 2}])
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="reaction_remove").count(), 0)

    def test_complete_reaction_state_repairs_remove_and_readd_without_duplicates(self):
        reaction = [{"name": "thumbsup", "users": ["U1"], "count": 1}]
        self.repair(reactions=reaction)
        self.repair(reactions=[])
        self.repair(reactions=[])
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="reaction_remove").count(), 1)
        self.repair(reactions=reaction)
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="reaction_add").count(), 2)
