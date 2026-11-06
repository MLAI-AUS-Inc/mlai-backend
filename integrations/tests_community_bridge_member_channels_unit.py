"""Database-free tests for the inactive member-channel foundations."""

from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services.community_bridge import member_channels as channels, store
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


class MemberChannelInfoReaderHintTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 3, 25, tzinfo=dt_timezone.utc)
        clock = patch.object(channels.timezone, "now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.observed = datetime(2000, 1, 1, 0, 0, 0, 1, tzinfo=dt_timezone.utc)

    def hint(self, grant_id=10, observed_at=None):
        return channels.info_reader_hint_reason(grant_id, observed_at or self.observed)

    def test_codec_preserves_exact_integer_utc_microseconds(self):
        encoded = self.hint()
        self.assertEqual(encoded, f"i:a:{946684800000001:x}")
        decoded = channels.info_reader_hint(encoded)
        self.assertEqual((decoded.grant_id, decoded.observed_at), (10, self.observed))
        self.assertEqual(decoded.observed_at.utcoffset(), timedelta(0))
        self.assertEqual(self.hint(1, datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=dt_timezone.utc)), "i:1:1")

    def test_timezone_offset_canonicalizes_the_same_instant(self):
        offset = dt_timezone(timedelta(hours=11))
        local = datetime(2000, 1, 1, 11, 0, 0, 1, tzinfo=offset)
        self.assertEqual(self.hint(observed_at=local), self.hint())
        self.assertEqual(channels.info_reader_hint(self.hint(observed_at=local)).observed_at, self.observed)

    def test_encoder_bounds_positive_signed_64_bit_grant_ids_and_reason_length(self):
        largest = (1 << 63) - 1
        encoded = self.hint(largest)
        self.assertLessEqual(len(encoded), 40)
        self.assertEqual(channels.info_reader_hint(encoded).grant_id, largest)
        for grant_id in (None, True, False, 0, -1, 1.0, "1", 1 << 63):
            with self.subTest(grant_id=grant_id):
                self.assertIsNone(self.hint(grant_id))

    def test_encoder_requires_aware_positive_bounded_observation(self):
        epoch = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
        for observed in (None, True, 1, "2000-01-01", self.observed.replace(tzinfo=None),
                         epoch, epoch - timedelta(microseconds=1),
                         self.now + timedelta(minutes=5, microseconds=1),
                         datetime.max.replace(tzinfo=dt_timezone.utc)):
            with self.subTest(observed=observed):
                self.assertIsNone(channels.info_reader_hint_reason(1, observed))
        edge = self.now + timedelta(minutes=5)
        self.assertEqual(channels.info_reader_hint(self.hint(1, edge)).observed_at, edge)

    def test_decoder_rejects_noncanonical_malformed_or_overlong_reason(self):
        stamp = f"{946684800000001:x}"
        for reason in (None, True, 1, {}, "", "topic", "I:a:1", "i:A:1", "i:a:ABC",
                       "i:0:1", "i:-1:1", "i:+1:1", "i:01:1", "i:1:01", "i:1:0",
                       "i:1:-1", "i:1:+1", "i:1:1:1", "i:1:1\n", " i:1:1",
                       f"i:0a:{stamp}", f"i:a:0{stamp}", "i:8000000000000000:1",
                       "i:1:ffffffffffffffff", "i:1:" + "1" * 37):
            with self.subTest(reason=reason):
                self.assertIsNone(channels.info_reader_hint(reason))

    def test_decoder_rechecks_future_limit_independently_of_encoder(self):
        encoded = self.hint(1, self.now + timedelta(minutes=5))
        with patch.object(channels.timezone, "now", return_value=self.now - timedelta(microseconds=1)):
            self.assertIsNone(channels.info_reader_hint(encoded))

    def test_generic_refresh_keeps_explicit_reader_hint(self):
        explicit = self.hint()
        for incoming in ("metadata_audit", "owner_open_priority", "topic" * 20):
            with self.subTest(incoming=incoming):
                self.assertEqual(channels._refresh_reason(explicit, incoming), explicit)
        self.assertEqual(channels._refresh_reason("metadata_audit", explicit), explicit)

    def test_only_newer_explicit_observation_replaces_existing_hint(self):
        previous = self.hint(10)
        older = self.hint(11, self.observed - timedelta(microseconds=1))
        equal = self.hint(11)
        newer = self.hint(11, self.observed + timedelta(microseconds=1))
        self.assertEqual(channels._refresh_reason(previous, older), previous)
        self.assertEqual(channels._refresh_reason(previous, equal), previous)
        self.assertEqual(channels._refresh_reason(previous, newer), newer)

    def test_invalid_encoded_input_is_generic_and_cannot_displace_valid_hint(self):
        explicit = self.hint()
        malformed = ("i:8000000000000000:1", "i:01:1", "i:1:1" + " " * 50)
        for incoming in malformed:
            with self.subTest(incoming=incoming):
                self.assertEqual(channels._refresh_reason(explicit, incoming), explicit)
                generic = channels._refresh_reason("metadata_audit", incoming)
                self.assertEqual(generic, "public_source_unverified")
                self.assertIsNone(channels.info_reader_hint(generic))
        self.assertEqual(channels._refresh_reason(malformed[0], explicit), explicit)

    def test_generic_reasons_keep_latest_text_with_existing_field_bound(self):
        self.assertEqual(channels._refresh_reason("rename", "topic"), "topic")
        self.assertEqual(channels._refresh_reason("rename", "topic" * 20), ("topic" * 20)[:40])


@override_settings(MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID=WORKSPACE)
class MemberChannelScopeTests(SimpleTestCase):
    def test_empty_cohort_still_requires_exact_configured_workspace_and_identity(self):
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS=""):
            self.assertTrue(channels.member_channel_in_scope(WORKSPACE, CHANNEL))
            self.assertFalse(channels.member_channel_in_scope("TOTHER", CHANNEL))
            self.assertFalse(channels.member_channel_in_scope(WORKSPACE, "DPRIVATE"))

    def test_canary_cohort_does_not_admit_other_public_channels(self):
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="C0CAPNW3QLT, CSECOND"):
            self.assertTrue(channels.member_channel_in_scope(WORKSPACE, "C0CAPNW3QLT"))
            self.assertTrue(channels.member_channel_in_scope(WORKSPACE, "CSECOND"))
            self.assertFalse(channels.member_channel_in_scope(WORKSPACE, CHANNEL))

    def test_malformed_or_unbounded_explicit_cohort_denies_expansion(self):
        for cohort in [None, [], " ", ",", CHANNEL + ",", "DPRIVATE", CHANNEL + ",invalid",
                       CHANNEL + "\n", CHANNEL + "\r", CHANNEL + "\x00", CHANNEL + "\u2028",
                       ",".join([CHANNEL] * 1001), "C" * 25001]:
            with self.subTest(cohort_type=type(cohort).__name__), override_settings(
                SLACK_MEMBER_CHANNEL_ALLOWED_IDS=cohort,
            ):
                self.assertFalse(channels.member_channel_in_scope(WORKSPACE, CHANNEL))

    def test_deploy_validation_rejects_malformed_cohort_before_host_writes(self):
        from scripts.validate_message_sync_deploy_config import validate

        for cohort in [None, " ", ",", "DPRIVATE", CHANNEL + ",", CHANNEL + "\n", CHANNEL + "\u2028",
                       ",".join([CHANNEL] * 1001)]:
            with self.subTest(cohort_type=type(cohort).__name__):
                with self.assertRaisesMessage(ValueError, "SLACK_MEMBER_CHANNEL_ALLOWED_IDS"):
                    validate({"SLACK_MEMBER_CHANNEL_ALLOWED_IDS": cohort})
        for cohort in ["", CHANNEL, CHANNEL + ", C0CAPNW3QLT"]:
            with self.subTest(cohort=cohort):
                validate({"SLACK_MEMBER_CHANNEL_ALLOWED_IDS": cohort})


class InactiveLifecycleIngressTests(SimpleTestCase):
    def test_lifecycle_events_never_enter_message_normalization_or_receipts(self):
        channel = {"id": CHANNEL, "name": "synthetic-channel-" + "x" * 150}
        events = [
            {"type": kind, "channel": channel}
            for kind in ("channel_created", "channel_rename")
        ] + [
            {"type": kind, "channel": CHANNEL}
            for kind in ("channel_archive", "channel_unarchive", "channel_deleted")
        ] + [{"type": "channel_id_changed", "old_channel_id": CHANNEL, "new_channel_id": "CNEW"}]
        for durable in (False, True):
            for event in events:
                with self.subTest(durable=durable, event=event["type"]), override_settings(
                    MESSAGE_SYNC_ENABLED=durable,
                ), patch.object(store, "_normalize_slack_event") as normalize, patch.object(
                    store, "ingest_inbound_event",
                ) as receipt:
                    result = store.ingest_slack_event({"event_id": "EvSYNTHETIC", "event": event})
                    self.assertEqual(result, {"status": "ignored", "reason": "channel_lifecycle_inactive"})
                    normalize.assert_not_called()
                    receipt.assert_not_called()

    def test_messages_and_reactions_keep_the_existing_ingestion_route(self):
        for kind in ("message", "reaction_added", "reaction_removed"):
            event = {"type": kind}
            if kind == "message":
                event["channel"] = CHANNEL
            normalized = {"source_channel_id": CHANNEL, "source_message_id": "1.0"}
            with self.subTest(event=kind), patch.object(
                store, "_normalize_slack_event", return_value=normalized,
            ) as normalize, patch.object(
                store, "ingest_inbound_event", return_value={"status": "accepted"},
            ) as receipt:
                payload = {"event_id": "EvSYNTHETIC", "event": event}
                self.assertEqual(store.ingest_slack_event(payload), {"status": "accepted"})
                normalize.assert_called_once_with(payload)
                self.assertEqual(receipt.call_args.kwargs["source_channel_id"], CHANNEL)
                self.assertEqual(receipt.call_args.kwargs["event_type"], kind)
                self.assertEqual(receipt.call_args.kwargs["normalized_event"], normalized)


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
