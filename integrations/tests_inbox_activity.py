"""Database-free proofs for delivery activity in Slack's ordering domain."""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, override_settings
from integrations.services.message_sync import read_activity as activity


class DeliveryActivityTests(SimpleTestCase):
    def delivery(self, **changes):
        values = dict(status="completed", delivery_type="create", source_platform="slack",
                      target_platform="buzz", source_message_id="100.000001",
                      source_parent_message_id="", payload={})
        return SimpleNamespace(**(values | changes))

    def link(self, **changes):
        return SimpleNamespace(**(dict(source_deleted_at=None, destination_deleted_at=None,
                                       destination_message_id="200.000002") | changes))

    def test_inbound_and_outbound_use_confirmed_slack_timestamp(self):
        self.assertEqual(activity.delivery_activity(self.delivery(), self.link()), "100.000001")
        self.assertEqual(activity.delivery_activity(self.delivery(
            source_platform="buzz", target_platform="slack", source_message_id="a" * 64),
            self.link()), "200.000002")

    def test_only_completed_countable_current_links_contribute(self):
        for changes in [dict(status="pending"), dict(delivery_type="edit"),
                        dict(source_platform="discord"), dict(source_parent_message_id="99.0")]:
            self.assertEqual(activity.delivery_activity(self.delivery(**changes), self.link()), "")
        for link in [None, self.link(source_deleted_at=1), self.link(destination_deleted_at=1),
                     self.link(destination_message_id="nan")]:
            delivery = self.delivery(source_platform="buzz", target_platform="slack")
            self.assertEqual(activity.delivery_activity(delivery, link), "")
        self.assertEqual(activity.delivery_activity(self.delivery(source_parent_message_id="99.0",
            payload={"metadata": {"broadcast": True}}), self.link()), "100.000001")

    def test_locked_frontier_is_monotonic_and_idempotent(self):
        state = SimpleNamespace(public_channel_id=1, private_conversation_id=None,
                                latest_source_activity="200.000002", save=MagicMock())
        activity.advance_public_activity(state, [{"ts": "100.000001"}])
        activity.advance_public_activity(state, [{"ts": "200.000002"}])
        state.save.assert_not_called()
        activity.advance_public_activity(state, [{"ts": "300.000003"}])
        self.assertEqual(state.latest_source_activity, "300.000003")
        state.save.assert_called_once_with(update_fields=["latest_source_activity"])

    @override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=False)
    def test_default_off_has_no_database_effect(self):
        activity.record_public_delivery(1)

    def test_delivery_hook_waits_for_successful_commit(self):
        from contextlib import nullcontext
        from integrations.services.community_bridge import store
        callbacks = []
        manager = MagicMock()
        with patch.object(store.CommunityBridgeDelivery, "objects", manager), patch.object(
            store, "guard_delivery"
        ), patch.object(store.transaction, "atomic", return_value=nullcontext()), patch.object(
            store.transaction, "on_commit", side_effect=lambda callback, **kwargs: callbacks.append(callback)
        ), patch.object(activity, "record_public_delivery") as record:
            store.complete_delivery(delivery_id=123)
            record.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            manager.filter.return_value.update.assert_called_once()
            callbacks[0]()
            record.assert_called_once_with(123)
