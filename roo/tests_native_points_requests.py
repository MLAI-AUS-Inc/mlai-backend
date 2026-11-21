from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory
import requests

from agent_runtime.native_crypto import public_key, signing_key
from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeChannel, CommunityBridgeMessageLink
from roo.models import Ledger, PointsRequest, PointsRequestDelivery
from roo.native_points_requests import RooRelay, deliver_ready, handle_event
from roo.points_request_workflow import ROO_POINTS_CHANNEL_ID, approve_locked_request
from roo.views import PointsRequestViewSet


@override_settings(
    ROO_POINTS_LEVEL_UP_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="roo-points-test",
)
class NativePointsRequestTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(email="javelin@example.test", first_name="Javelin")
        self.admin = get_user_model().objects.create_superuser(email="alan@example.test", password="unused")
        self.relay_key = signing_key("12" * 32)
        self.member_key = public_key(signing_key("13" * 32))
        self.admin_key = public_key(signing_key("14" * 32))
        for user, key in [(self.member, self.member_key), (self.admin, self.admin_key)]:
            CommunityChatDevice.objects.create(
                user=user, public_key=key, status="verified",
                verified_at=timezone.now() - timedelta(minutes=5),
            )
        self.relay = SimpleNamespace(key=self.relay_key, pubkey=public_key(self.relay_key), publish=Mock())

    def event(self, author, event_id, kind, content, tags, *, include_channel=True):
        channel_tags = [["h", str(ROO_POINTS_CHANNEL_ID)]] if include_channel else []
        return dict(
            id=event_id, pubkey=author, kind=kind, content=content,
            tags=[*channel_tags, *tags],
            created_at=int(timezone.now().timestamp()),
        )

    def test_relay_http_query_uses_an_array_of_filters(self):
        relay = RooRelay("12" * 32)
        relay.post = Mock(return_value=[])
        self.assertEqual(relay.query(ROO_POINTS_CHANNEL_ID, [9], limit=200), [])
        relay.post.assert_called_once_with(
            "/query", [{"#h": [str(ROO_POINTS_CHANNEL_ID)], "kinds": [9], "limit": 200}],
        )

    def test_approval_creates_distinct_thread_and_repost_with_durable_native_ids(self):
        source = self.event(self.member_key, "ab" * 32, 9, "@Roo give me 30 points for volunteering",
                            [["p", self.relay.pubkey]])
        handle_event(self.relay, source)
        handle_event(self.relay, source)
        deliver_ready(self.relay)
        row = PointsRequest.objects.get()
        target = ["e", row.native_summary_id]
        invalid_reactions = [
            self.event(self.member_key, "ac" * 32, 7, "✅", [target], include_channel=False),
            self.event(self.admin_key, "ae" * 32, 7, "✅", [
                ["h", "11111111-1111-4111-8111-111111111111"], target,
            ], include_channel=False),
            self.event(self.admin_key, "af" * 32, 7, "✅", [["e", source["id"]]], include_channel=False),
        ]
        for reaction in invalid_reactions:
            handle_event(self.relay, reaction)
            self.assertFalse(Ledger.objects.exists())

        reaction = self.event(self.admin_key, "ad" * 32, 7, "✔️", [target], include_channel=False)
        handle_event(self.relay, reaction)
        handle_event(self.relay, reaction)
        row.refresh_from_db()
        self.assertEqual(row.status, "approved")
        self.assertEqual(Ledger.objects.filter(reference_type="POINTS_REQUEST").count(), 1)
        celebration = row.deliveries.get(stage="celebration")
        self.assertEqual((celebration.transition["from"], celebration.transition["to"]), (0, 2))

        deliver_ready(self.relay)
        self.assertEqual(self.relay.publish.call_count, 4)
        events = [call.args[1] for call in self.relay.publish.call_args_list]
        self.assertIn("Points request approved", events[1]["content"])
        self.assertEqual(events[2]["content"], events[3]["content"])
        self.assertIn("@Javelin", events[2]["content"])
        self.assertIn(["p", self.member_key], events[3]["tags"])
        self.assertIn(["e", source["id"], "", "reply"], events[2]["tags"])
        self.assertFalse(any(tag[0] == "e" for tag in events[3]["tags"]))
        self.assertNotEqual(events[2]["id"], events[3]["id"])

    def test_worker_reuses_signed_event_after_lost_response(self):
        source = self.event(self.member_key, "ab" * 32, 9, "@Roo give me 1 point for helping",
                            [["p", self.relay.pubkey]])
        handle_event(self.relay, source)
        self.relay.publish.side_effect = requests.Timeout()
        deliver_ready(self.relay)
        delivery = PointsRequestDelivery.objects.get()
        original = delivery.signed_event
        self.assertIsNone(delivery.delivered_at)
        PointsRequestDelivery.objects.filter(pk=delivery.pk).update(available_at=timezone.now())
        self.relay.publish.side_effect = None
        deliver_ready(self.relay)
        self.assertEqual(self.relay.publish.call_args.args[1], original)
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivered_at)


    @override_settings(COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TLOCAL",
                       COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO")
    def test_bridged_level_up_waits_for_exact_roo_confirmation(self):
        channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TLOCAL", slack_channel_id="CLOCAL",
            destination_platform="buzz", audience="community", mirror_decision="mirror",
            destination_channel_id="11111111-1111-4111-8111-111111111111",
        )
        with transaction.atomic():
            row = PointsRequest.objects.create(
                requester_slack_id="UONE", target_slack_id="UONE",
                slack_channel_id="CLOCAL", slack_thread_ts="100.1",
                points=30, reason="Volunteering",
            )
            approve_locked_request(row, self.member, admin_slack_id="UADMIN")

        deliver_ready(self.relay)
        self.relay.publish.assert_not_called()
        row.deliveries.filter(stage="celebration").update(external_confirmation_id="101.2")
        bridged = CommunityBridgeMessageLink.objects.create(
            channel=channel, source_platform="slack", source_channel_id="CLOCAL",
            source_author_id="UROO", source_message_id="101.2", source_parent_message_id="100.1",
            destination_platform="buzz", destination_channel_id=channel.destination_channel_id,
            destination_message_id="01" * 32, destination_parent_message_id="02" * 32,
        )
        row.deliveries.update(available_at=timezone.now())
        deliver_ready(self.relay)
        self.assertEqual(self.relay.publish.call_count, 2)
        _, original = self.relay.publish.call_args_list[0].args
        self.assertIn(["e", "02" * 32, "", "reply"], original["tags"])
        self.assertGreater(original["created_at"], int(bridged.created_at.timestamp()))

        with transaction.atomic():
            unchanged = PointsRequest.objects.create(
                native_channel_id=ROO_POINTS_CHANNEL_ID, native_root_id="03" * 32,
                native_recipient=self.member, requester_slack_id="", target_slack_id="",
                points=1, reason="Helping",
            )
            approve_locked_request(unchanged, self.member, admin=self.admin)
        self.assertEqual(list(unchanged.deliveries.values_list("stage", flat=True)), ["confirmation"])

    @override_settings(COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TLOCAL",
                       COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
                       ROO_API_KEY="test-roo", INTERNAL_API_KEY="test-internal")
    def test_committed_slack_award_reclaims_confirmation_and_delivers_both_celebrations(self):
        channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TLOCAL", slack_channel_id="CLOCAL",
            destination_platform="buzz", audience="community", mirror_decision="mirror",
            destination_channel_id="11111111-1111-4111-8111-111111111111",
        )
        with transaction.atomic():
            request = PointsRequest.objects.create(
                requester_slack_id="UONE", target_slack_id="UONE",
                slack_channel_id="CLOCAL", slack_thread_ts="100.1",
                slack_summary_message_ts="100.2", points=30, reason="Volunteering",
            )
            approve_locked_request(request, self.member, admin_slack_id="UADMIN", recover_slack=True)
        claim = PointsRequestViewSet.as_view({"post": "claim_slack_confirmation"})
        factory = APIRequestFactory()

        def take(key="test-roo"):
            return claim(factory.post("/claim/", HTTP_X_API_KEY=key))

        self.assertEqual(take("test-internal").status_code, 403)
        first = take()
        self.assertEqual(first.data["request_id"], request.pk)
        self.assertEqual(first.data["attempt"], 1)
        self.assertEqual(take().status_code, 204)
        self.relay.publish.assert_not_called()

        request.deliveries.filter(stage="slack_confirmation").update(available_at=timezone.now())
        second = take()
        self.assertEqual(second.data["attempt"], 2)
        self.assertEqual(second.data["text"], first.data["text"])
        CommunityBridgeMessageLink.objects.create(
            channel=channel, source_platform="slack", source_channel_id="CLOCAL",
            source_author_id="UROO", source_message_id="101.2", source_parent_message_id="100.1",
            destination_platform="buzz", destination_channel_id=channel.destination_channel_id,
            destination_message_id="01" * 32, destination_parent_message_id="02" * 32,
            source_payload={"text": first.data["text"]},
        )
        request.deliveries.filter(stage="slack_confirmation").update(available_at=timezone.now())
        self.assertEqual(take().data["existing_message_id"], "101.2")
        confirm = PointsRequestViewSet.as_view({"post": "slack_confirmation"})
        receipt = {"message_id": "101.2", "channel_id": "CLOCAL"}
        self.assertEqual(
            confirm(factory.post("/receipt/", receipt, HTTP_X_API_KEY="test-roo"), pk=request.pk).status_code,
            204,
        )
        self.assertIsNotNone(request.deliveries.get(stage="slack_confirmation").delivered_at)
        self.assertEqual(take().status_code, 204)
        deliver_ready(self.relay)
        self.assertEqual(self.relay.publish.call_count, 2)
        (original, reply), (destination, repost) = [call.args for call in self.relay.publish.call_args_list]
        self.assertEqual(str(original), channel.destination_channel_id)
        self.assertEqual(destination, ROO_POINTS_CHANNEL_ID)
        self.assertEqual(reply["content"], repost["content"])
        self.assertIn(["e", "02" * 32, "", "reply"], reply["tags"])
        self.assertFalse(any(tag[0] == "e" for tag in repost["tags"]))
        self.assertEqual(Ledger.objects.filter(reference_type="POINTS_REQUEST").count(), 1)
