"""Source-state repair preserves newer callbacks and partial-list uncertainty."""
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery
from integrations.services.community_bridge.store import ingest_slack_event
from integrations.services.message_sync.public_repair import repair_observed_message


from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS
from integrations.services.message_sync import member_history
from integrations.services.message_sync.scheduler import schedule_job, claim_job, finish_job
from integrations.services import slack_dm_mirror as dm


@override_settings(**SETTINGS)
class PublicRepairTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.channel.audience = "community"
        self.channel.save()
        self.state.public_channel = self.channel
        self.repair_number = 0
        self.message = {"ts": "1700000000.000001", "user": "U1", "text": "original"}
        ingest_slack_event({"team_id": "TTEST", "event_id": "original", "event": {
            **self.message, "type": "message", "channel": "CROOM", "channel_type": "channel",
        }})

    def repair(self, **values):
        self.fenced_repair({**self.message, **values}, timezone.now())

    def fenced_repair(self, message, started):
        self.repair_number += 1
        schedule_job(self.state, "thread", source_object_key=str(self.repair_number))
        lease = claim_job(kinds=["thread"])
        self.assertIsNotNone(lease)
        def page(bound, state, **kwargs):
            repair_observed_message(state.public_channel, message, read_started_at=started)
            finish_job(bound, checkpoint=bound.checkpoint, complete=True)
        member_history._community_attempt(lease, self.state, page,
            dm._capture_slack_grant_api_authority(self.a, refresh_token=False))

    def test_changed_content_without_edited_timestamp_repairs_once_and_can_change_back(self):
        self.repair(text="updated")
        self.repair(text="updated")
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="edit").count(), 1)
        self.repair()
        self.assertEqual(CommunityBridgeDelivery.objects.filter(delivery_type="edit").count(), 2)

    def test_callback_newer_than_source_read_wins(self):
        started = timezone.now()
        ingest_slack_event({"team_id": "TTEST", "event_id": "newer-callback", "event": {
            "type": "message", "subtype": "message_changed", "channel": "CROOM", "channel_type": "channel",
            "message": {**self.message, "text": "newer"}, "event_ts": "1700000001.000001",
        }})
        self.fenced_repair(self.message, started)
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
