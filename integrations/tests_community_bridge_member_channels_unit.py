"""Database-free tests for the inactive member-channel foundations."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services.community_bridge import member_channels as channels
from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgeError,
    BuzzBridgePermanentError,
)


WORKSPACE = "TTESTMEMBERS"
CHANNEL = "CTESTMEMBERS"
ROOM = "5f0c4b1e-8a35-5d55-9d2e-0b6f4b8c2a11"


def channel_row(**changes):
    values = dict(
        audience="members", slack_channel_id=CHANNEL, slack_channel_name="planning", slack_visibility="public",
        slack_archived=False, slack_deleted=False, slack_topic="Friday", slack_purpose="Planning",
        slack_created_at=None, lifecycle_source="", room_state={}, enabled=False,
        destination_channel_id="", mirror_decision="mirror", hold_reason="",
    )
    return SimpleNamespace(**(values | changes))


class MemberChannelStateTests(SimpleTestCase):
    def test_slack_state_retains_membership_as_observation_only(self):
        state = channels.SlackChannelState.from_slack({
            "id": CHANNEL, "name": "planning", "created": 1234, "is_member": True,
            "topic": {"value": "Friday"}, "purpose": {"value": "Planning"},
        })
        row = channel_row()
        channels._observe(row, state, timezone.now())
        self.assertTrue(state.is_member)
        self.assertFalse(row.enabled)
        self.assertFalse(hasattr(row, "slack_bot_member"))
        self.assertEqual((row.slack_topic, row.slack_purpose), ("Friday", "Planning"))
        self.assertEqual(row.slack_created_at.timestamp(), 1234)

    def test_private_deleted_and_slack_connect_observations_stop_delivery(self):
        for state, expected in [
            (channels.SlackChannelState(CHANNEL, is_private=True), "converted_to_private"),
            (channels.SlackChannelState(CHANNEL, is_ext_shared=True), "slack_connect"),
            (channels.SlackChannelState(CHANNEL, is_archived=True), ""),
            (channels.SlackChannelState(CHANNEL, exists=False), ""),
        ]:
            with self.subTest(expected=expected):
                row = channel_row(enabled=True)
                channels._observe(row, state, timezone.now())
                self.assertFalse(row.enabled)
                self.assertEqual(row.hold_reason, expected)
                if expected:
                    self.assertEqual(row.mirror_decision, "hold")

    def test_desired_state_has_explicit_immutable_audience(self):
        for audience in ("community", "members"):
            with self.subTest(audience=audience):
                row = channel_row(audience=audience, lifecycle_source="event")
                self.assertEqual(channels._desired_room(row), {
                    "audience": audience, "name": "planning", "archived": False,
                    "about": "Planning", "topic": "Friday",
                })
        with self.assertRaisesMessage(ValueError, "invalid_bridge_room_audience"):
            channels._desired_room(channel_row(audience="public"))

    def test_observation_cannot_retarget_another_slack_identity(self):
        row = channel_row(enabled=True)
        with self.assertRaisesMessage(ValueError, "slack_channel_identity_mismatch"):
            channels._observe(row, channels.SlackChannelState("COTHER"), timezone.now())
        self.assertTrue(row.enabled)
        self.assertFalse(hasattr(row, "slack_seen_at"))

    def test_disabled_delivery_gate_is_not_room_drift(self):
        row = channel_row(destination_channel_id=ROOM)
        row.room_state = {"applied": channels._desired_room(row)}
        self.assertFalse(channels._room_out_of_date(row))
        self.assertFalse(row.enabled)

    def test_hold_never_creates_room_but_existing_room_can_be_archived(self):
        row = channel_row(mirror_decision="hold")
        self.assertIsNone(channels._desired_room(row))
        row.destination_channel_id = ROOM
        row.slack_deleted = True
        self.assertTrue(channels._desired_room(row)["archived"])

    def test_operator_text_is_preserved_until_slack_changes_it(self):
        row = channel_row(room_state={"text_baseline": ["Planning", "Friday"]})
        self.assertIsNone(channels._desired_room(row)["topic"])
        row.slack_topic = "Saturday"
        self.assertEqual(channels._desired_room(row)["topic"], "Saturday")

    def test_queue_rejects_invalid_identity_before_database_access(self):
        for workspace, channel in [("", CHANNEL), (WORKSPACE, "DNOTPUBLIC")]:
            with self.subTest(workspace=workspace, channel=channel):
                with self.assertRaisesMessage(ValueError, "invalid_slack_channel_identity"):
                    channels.queue_refresh(workspace, channel, reason="event")


class MemberChannelAdapterContractTests(SimpleTestCase):
    def upsert(self, **changes):
        return BuzzBridgeClient.upsert_public_channel(**({
            "slack_workspace_id": WORKSPACE, "slack_channel_id": CHANNEL,
            "audience": "members", "name": "planning", "archived": False,
        } | changes))

    @patch.object(BuzzBridgeClient, "_post_adapter")
    def test_audience_is_required_and_sent_for_both_room_types(self, post):
        post.return_value = {"channel_id": ROOM, "created": True, "changed": ["created"]}
        for audience in ("community", "members"):
            with self.subTest(audience=audience):
                self.assertEqual(self.upsert(audience=audience)["channel_id"], ROOM)
                self.assertEqual(post.call_args.args, ("v1/public-channels", {
                    "slack_workspace_id": WORKSPACE, "slack_channel_id": CHANNEL,
                    "audience": audience, "name": "planning", "archived": False,
                }))
        with self.assertRaises(TypeError):
            BuzzBridgeClient.upsert_public_channel(
                slack_workspace_id=WORKSPACE, slack_channel_id=CHANNEL,
                name="planning", archived=False,
            )

    @patch.object(BuzzBridgeClient, "_post_adapter")
    def test_invalid_audience_is_never_sent(self, post):
        for audience in ("", "public", "MEMBERS", None):
            with self.subTest(audience=audience):
                with self.assertRaises(BuzzBridgePermanentError):
                    self.upsert(audience=audience)
        post.assert_not_called()

    @patch.object(BuzzBridgeClient, "_post_adapter")
    def test_existing_room_uuid_is_exact_and_wrong_reply_is_rejected(self, post):
        post.return_value = {"channel_id": "0403bbad-3aba-5e1f-a19d-88fbbf06565a"}
        with self.assertRaisesMessage(BuzzBridgeError, "wrong public channel"):
            self.upsert(channel_id=ROOM, topic="Friday")
        self.assertEqual(post.call_args.args[1]["channel_id"], ROOM)
        self.assertEqual(post.call_args.args[1]["topic"], "Friday")

    @override_settings(
        BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
        BUZZ_BRIDGE_ADAPTER_TOKEN="synthetic-test-token",
    )
    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_http_request_carries_audience_and_preserves_conflict_error(self, post):
        post.return_value = SimpleNamespace(
            ok=True, status_code=200,
            json=lambda: {"channel_id": ROOM, "created": True, "changed": ["created"]},
        )
        self.upsert()
        self.assertTrue(post.call_args.args[0].endswith("/v1/public-channels"))
        self.assertEqual(post.call_args.kwargs["json"]["audience"], "members")
        post.return_value = SimpleNamespace(
            ok=False, status_code=409, json=lambda: {"error": "name_conflict"},
        )
        with self.assertRaises(BuzzBridgePermanentError) as raised:
            self.upsert()
        self.assertEqual(raised.exception.adapter_error, "name_conflict")
