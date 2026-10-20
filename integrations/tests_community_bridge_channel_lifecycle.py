import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from slack_sdk.errors import SlackApiError

from integrations.message_sync_models import BridgeSyncState
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeChannelRefresh,
    CommunityBridgeLifecycleState,
    CommunityBridgeReceipt,
)
from integrations.services.community_bridge import channel_lifecycle
from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgeError,
    BuzzBridgePermanentError,
)
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.message_sync.scheduler import BudgetDeferred


WORKSPACE = "T05N9C1QSJC"
APP = "ABRIDGE"
CHANNEL = "C0BNEWROOM1"
ROOM = "5f0c4b1e-8a35-5d55-9d2e-0b6f4b8c2a11"
LEGACY_ROOM = "0403bbad-3aba-5e1f-a19d-88fbbf06565a"

LIFECYCLE_SETTINGS = dict(
    COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED=True,
    MESSAGE_SYNC_ENABLED=False,
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID=WORKSPACE,
    MESSAGE_SYNC_SLACK_APP_ID=APP,
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
    SLACK_BRIDGE_SIGNING_SECRET="bridge-secret",
    BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
    BUZZ_BRIDGE_ADAPTER_TOKEN="a" * 32,
    BUZZ_BRIDGE_CALLBACK_SECRET="b" * 32,
)


def slack_channel(channel_id=CHANNEL, name="demo-day", *, created=None, **extra):
    return {
        "id": channel_id,
        "name": name,
        "created": int(time.time()) if created is None else created,
        "is_private": False,
        "is_archived": False,
        "is_member": False,
        "topic": {"value": "Friday 5pm"},
        "purpose": {"value": "Demo day planning"},
        **extra,
    }


def event_payload(event, *, team=WORKSPACE, app=APP, event_id="Ev0001"):
    return {"type": "event_callback", "team_id": team, "api_app_id": app, "event_id": event_id, "event": event}


def slack_error(code):
    return SlackApiError(code, {"ok": False, "error": code})


@override_settings(**LIFECYCLE_SETTINGS)
class ChannelLifecycleIntakeTests(TestCase):
    def test_other_events_fall_through_to_message_ingestion(self):
        self.assertIsNone(channel_lifecycle.record_lifecycle_event(event_payload({"type": "message"})))
        self.assertIsNone(channel_lifecycle.record_lifecycle_event({"type": "url_verification"}))

    @override_settings(COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED=False)
    def test_disabled_flag_acknowledges_without_queueing(self):
        result = channel_lifecycle.record_lifecycle_event(
            event_payload({"type": "channel_created", "channel": slack_channel()})
        )
        self.assertEqual(result, {"status": "ignored", "reason": "channel_lifecycle_disabled"})
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())
        self.assertFalse(CommunityBridgeLifecycleState.objects.exists())

    def test_foreign_workspace_or_app_is_ignored(self):
        for payload in (
            event_payload({"type": "channel_rename", "channel": slack_channel()}, team="TOTHER"),
            event_payload({"type": "channel_rename", "channel": slack_channel()}, app="AOTHER"),
        ):
            with self.subTest(payload=payload["team_id"] + payload["api_app_id"]):
                result = channel_lifecycle.record_lifecycle_event(payload)
                self.assertEqual(result["reason"], "channel_lifecycle_foreign_source")
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())

    def test_events_queue_one_refresh_per_channel_and_fix_the_auto_mirror_boundary(self):
        before = timezone.now()
        channel_lifecycle.record_lifecycle_event(
            event_payload({"type": "channel_created", "channel": slack_channel()})
        )
        result = channel_lifecycle.record_lifecycle_event(
            event_payload({"type": "channel_archive", "channel": CHANNEL}, event_id="Ev0002")
        )
        self.assertEqual(result, {"status": "accepted", "reason": "channel_archive"})
        refresh = CommunityBridgeChannelRefresh.objects.get()
        self.assertEqual((refresh.slack_channel_id, refresh.reason), (CHANNEL, "channel_archive"))
        boundary = CommunityBridgeLifecycleState.objects.get(slack_workspace_id=WORKSPACE)
        self.assertGreaterEqual(boundary.auto_mirror_since, before)

    def test_channel_id_change_rekeys_mapping_and_history_state(self):
        row = CommunityBridgeChannel.objects.create(
            slack_workspace_id=WORKSPACE, slack_channel_id="C0OLDID0001", slack_channel_name="events",
            destination_platform="buzz", destination_channel_id=LEGACY_ROOM, enabled=True,
        )
        state = BridgeSyncState.objects.create(public_channel=row, workspace_id=WORKSPACE, source_channel_id="C0OLDID0001")
        channel_lifecycle.record_lifecycle_event(event_payload({
            "type": "channel_id_changed", "old_channel_id": "C0OLDID0001", "new_channel_id": "C0NEWID0001",
        }))
        row.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(row.slack_channel_id, "C0NEWID0001")
        self.assertEqual(row.destination_channel_id, LEGACY_ROOM)
        self.assertTrue(row.room_sync_pending)
        self.assertEqual(state.source_channel_id, "C0NEWID0001")
        self.assertTrue(CommunityBridgeChannelRefresh.objects.filter(slack_channel_id="C0NEWID0001").exists())

    def test_signed_event_view_routes_lifecycle_events_without_message_receipts(self):
        body = json.dumps(event_payload({"type": "channel_created", "channel": slack_channel(name="x" * 120)})).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new(b"bridge-secret", f"v0:{timestamp}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
        response = APIClient().post(
            reverse("community_bridge_slack_events"), data=body, content_type="application/json",
            HTTP_X_SLACK_REQUEST_TIMESTAMP=timestamp, HTTP_X_SLACK_SIGNATURE=f"v0={digest}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "accepted")
        self.assertFalse(CommunityBridgeReceipt.objects.exists())
        self.assertTrue(CommunityBridgeChannelRefresh.objects.filter(slack_channel_id=CHANNEL).exists())

    def test_durable_inbox_routes_lifecycle_events(self):
        from integrations.services.message_sync import inbox

        payload = event_payload({"type": "channel_rename", "channel": slack_channel(name="demo-day-2026")})
        inbox.enqueue_slack_callback(payload)
        with patch.object(inbox, "expand_authorization_page", side_effect=lambda value: (value, True)):
            self.assertEqual(inbox.process_inbox_once(), 1)
        self.assertFalse(CommunityBridgeReceipt.objects.exists())
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().reason, "channel_rename")


class LifecycleHarness(TestCase):
    def setUp(self):
        super().setUp()
        settings_override = override_settings(**LIFECYCLE_SETTINGS)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.slack = MagicMock()
        self.slack.conversations_join.return_value = {"ok": True}
        client_patch = patch.object(SlackBridgeClient, "get_client", return_value=self.slack)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.rooms = []
        self.upsert = MagicMock(side_effect=self._fake_upsert)
        upsert_patch = patch.object(BuzzBridgeClient, "upsert_public_channel", self.upsert)
        upsert_patch.start()
        self.addCleanup(upsert_patch.stop)
        CommunityBridgeLifecycleState.objects.create(
            slack_workspace_id=WORKSPACE, auto_mirror_since=timezone.now() - timedelta(minutes=5),
        )

    def _fake_upsert(self, **kwargs):
        self.rooms.append(kwargs)
        return {"channel_id": kwargs.get("channel_id") or ROOM, "created": not kwargs.get("channel_id"), "changed": ["name"]}

    def apply(self, channel=None, **extra):
        state = channel_lifecycle.SlackChannelState.from_slack(channel or slack_channel(**extra))
        return channel_lifecycle.apply_channel_state(state, workspace_id=WORKSPACE, source="event")

    def mirrored_row(self):
        self.slack.conversations_info.return_value = {"ok": True, "channel": slack_channel()}
        self.assertEqual(self.apply(), "synced")
        self.rooms.clear()
        self.upsert.reset_mock()
        self.slack.conversations_join.reset_mock()
        return CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)


class ChannelLifecycleApplyTests(LifecycleHarness):
    def test_manifest_decisions_are_seeded_as_disabled_rows(self):
        excluded = CommunityBridgeChannel.objects.get(slack_channel_id="C09LVV0FUQK")
        self.assertEqual((excluded.mirror_decision, excluded.enabled), ("exclude", False))
        self.assertEqual(
            CommunityBridgeChannel.objects.filter(hold_reason="manifest", slack_visibility="private").count(), 25,
        )

    def test_new_public_channel_mirrors_via_refresh_and_is_eligible_for_history(self):
        self.slack.conversations_info.return_value = {"ok": True, "channel": slack_channel()}
        channel_lifecycle.queue_refresh(WORKSPACE, CHANNEL, reason="channel_created")
        self.assertEqual(channel_lifecycle.process_refreshes_once(), 1)

        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.mirror_decision, row.lifecycle_source), ("mirror", "event"))
        self.assertTrue(row.enabled)
        self.assertEqual(row.destination_channel_id, ROOM)
        self.assertEqual(row.destination_channel_name, "demo-day")
        self.assertFalse(row.room_sync_pending)
        self.slack.conversations_join.assert_called_once_with(channel=CHANNEL)
        self.assertEqual(self.rooms, [{
            "slack_workspace_id": WORKSPACE, "slack_channel_id": CHANNEL, "name": "demo-day",
            "archived": False, "about": "Demo day planning", "topic": "Friday 5pm", "channel_id": "",
        }])
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())
        # The exact selection history.seed_states() uses to start a 30-day import.
        self.assertTrue(CommunityBridgeChannel.objects.filter(
            pk=row.pk, enabled=True, destination_platform="buzz", sync_state__isnull=True,
        ).exists())

    def test_older_unlisted_channels_are_held_without_a_room(self):
        self.assertEqual(self.apply(created=int(time.time()) - 86400), "unchanged")
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.mirror_decision, row.hold_reason), ("hold", "backlog"))
        self.assertFalse(row.enabled)
        self.upsert.assert_not_called()
        self.slack.conversations_join.assert_not_called()

    def test_excluded_shared_private_and_archived_channels_never_mirror(self):
        self.apply(channel_id="C09LVV0FUQK", name="ethan_test")
        self.assertEqual(CommunityBridgeChannel.objects.get(slack_channel_id="C09LVV0FUQK").mirror_decision, "exclude")
        self.apply(channel_id="C0SHARED001", is_ext_shared=True)
        self.assertEqual(CommunityBridgeChannel.objects.get(slack_channel_id="C0SHARED001").hold_reason, "shared_channel")
        self.assertEqual(self.apply(channel_id="C0PRIVATE01", is_private=True), "skipped")
        self.assertEqual(self.apply(channel_id="C0ARCHIVED1", is_archived=True), "skipped")
        self.assertFalse(CommunityBridgeChannel.objects.filter(slack_channel_id__in=["C0PRIVATE01", "C0ARCHIVED1"]).exists())
        self.upsert.assert_not_called()

    def test_rename_and_topic_follow_slack_once(self):
        self.mirrored_row()
        self.assertEqual(self.apply(name="demo-day-2026", topic={"value": "Saturday"}), "synced")
        self.assertEqual(len(self.rooms), 1)
        self.assertEqual(
            (self.rooms[0]["channel_id"], self.rooms[0]["name"], self.rooms[0]["topic"]),
            (ROOM, "demo-day-2026", "Saturday"),
        )
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.slack_channel_name, row.destination_channel_name), ("demo-day-2026", "demo-day-2026"))
        self.assertEqual(self.apply(name="demo-day-2026", topic={"value": "Saturday"}), "unchanged")
        self.assertEqual(len(self.rooms), 1)

    def test_operator_rooms_keep_their_text_until_slack_changes_it(self):
        CommunityBridgeChannel.objects.create(
            slack_workspace_id=WORKSPACE, slack_channel_id="C05RG6CT9PB", slack_channel_name="events",
            destination_platform="buzz", destination_workspace_id="chat.mlai.au",
            destination_channel_id=LEGACY_ROOM, enabled=True,
        )
        legacy = dict(channel_id="C05RG6CT9PB", name="events", is_member=True, created=1)
        self.assertEqual(self.apply(**legacy), "synced")
        self.assertEqual(self.rooms[-1], {
            "slack_workspace_id": WORKSPACE, "slack_channel_id": "C05RG6CT9PB", "name": "events",
            "archived": False, "about": None, "topic": None, "channel_id": LEGACY_ROOM,
        })
        self.slack.conversations_join.assert_not_called()
        self.assertEqual(self.apply(**legacy), "unchanged")
        self.assertEqual(self.apply(**legacy, topic={"value": "Demo night Thursday"}), "synced")
        self.assertEqual(
            (self.rooms[-1]["topic"], self.rooms[-1]["about"]), ("Demo night Thursday", "Demo day planning"),
        )
        row = CommunityBridgeChannel.objects.get(slack_channel_id="C05RG6CT9PB")
        self.assertEqual((row.mirror_decision, row.enabled, row.destination_channel_id), ("mirror", True, LEGACY_ROOM))

    def test_archive_unarchive_and_delete_follow_slack(self):
        self.mirrored_row()
        self.apply(is_archived=True)
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertTrue(self.rooms[-1]["archived"])
        self.assertFalse(row.enabled)
        self.apply()
        row.refresh_from_db()
        self.assertFalse(self.rooms[-1]["archived"])
        self.assertTrue(row.enabled)
        self.slack.conversations_info.side_effect = slack_error("channel_not_found")
        state = channel_lifecycle.fetch_channel_state(CHANNEL)
        self.assertFalse(state.exists)
        channel_lifecycle.apply_channel_state(state, workspace_id=WORKSPACE, source="event")
        row.refresh_from_db()
        self.assertTrue(self.rooms[-1]["archived"])
        self.assertTrue(row.slack_deleted)
        self.assertFalse(row.enabled)
        self.assertEqual(row.destination_channel_id, ROOM)

    def test_conversion_to_private_fails_closed(self):
        self.mirrored_row()
        self.apply(is_private=True)
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.mirror_decision, row.hold_reason), ("hold", "converted_to_private"))
        self.assertFalse(row.enabled)
        self.assertTrue(self.rooms[-1]["archived"])

    def test_name_conflicts_hold_the_channel_without_a_room(self):
        self.upsert.side_effect = BuzzBridgePermanentError("conflict", adapter_error="name_conflict")
        self.assertEqual(self.apply(name="events"), "held")
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.mirror_decision, row.hold_reason, row.room_sync_error), ("hold", "name_conflict", "name_conflict"))
        self.assertEqual(row.destination_channel_id, "")
        self.assertFalse(row.room_sync_pending)
        self.assertFalse(row.enabled)

    def test_channels_the_bot_cannot_join_wait_without_a_room(self):
        self.slack.conversations_join.side_effect = slack_error("missing_scope")
        self.assertEqual(self.apply(), "waiting_for_bot")
        row = CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL)
        self.assertEqual((row.room_sync_error, row.room_sync_pending, row.enabled), ("bot_not_in_channel", True, False))
        self.upsert.assert_not_called()

    def test_retryable_room_failures_and_deferrals_requeue_the_refresh(self):
        self.slack.conversations_info.return_value = {"ok": True, "channel": slack_channel()}
        self.upsert.side_effect = BuzzBridgeError("adapter down", adapter_error="upstream_unavailable")
        channel_lifecycle.queue_refresh(WORKSPACE, CHANNEL, reason="channel_created")
        self.assertEqual(channel_lifecycle.process_refreshes_once(), 0)
        refresh = CommunityBridgeChannelRefresh.objects.get()
        self.assertEqual((refresh.attempts, refresh.last_error_code), (1, "room_sync_retry"))
        self.assertGreater(refresh.available_at, timezone.now())

        refresh.available_at = timezone.now()
        refresh.save()
        self.slack.conversations_info.side_effect = BudgetDeferred(30)
        self.assertEqual(channel_lifecycle.process_refreshes_once(), 0)
        refresh.refresh_from_db()
        self.assertEqual((refresh.attempts, refresh.last_error_code), (1, "budget_deferred"))
        self.assertGreater(refresh.available_at, timezone.now() + timedelta(seconds=25))


class ChannelLifecycleReconcileTests(LifecycleHarness):
    def test_pass_pages_then_requeues_unseen_mapped_channels(self):
        CommunityBridgeChannel.objects.create(
            slack_workspace_id=WORKSPACE, slack_channel_id="C0GONE00001", slack_channel_name="old",
            destination_platform="buzz", destination_channel_id=LEGACY_ROOM, enabled=True,
        )
        self.slack.conversations_list.side_effect = [
            {"ok": True, "channels": [slack_channel()], "response_metadata": {"next_cursor": "page-2"}},
            {"ok": True, "channels": [slack_channel("C0BACKLOG01", "old-topic", created=1)],
             "response_metadata": {"next_cursor": ""}},
        ]
        self.assertEqual(channel_lifecycle.reconcile_once(), "page")
        self.assertEqual(channel_lifecycle.reconcile_once(), "complete")
        self.assertEqual(channel_lifecycle.reconcile_once(), "idle")
        self.assertEqual(self.slack.conversations_list.call_args_list[1].kwargs["cursor"], "page-2")
        self.assertEqual(CommunityBridgeChannel.objects.get(slack_channel_id=CHANNEL).mirror_decision, "mirror")
        self.assertEqual(CommunityBridgeChannel.objects.get(slack_channel_id="C0BACKLOG01").mirror_decision, "hold")
        self.assertEqual(
            list(CommunityBridgeChannelRefresh.objects.values_list("slack_channel_id", "reason")),
            [("C0GONE00001", "reconcile_missing")],
        )
        state = CommunityBridgeLifecycleState.objects.get()
        self.assertEqual(state.reconcile_cursor, "")
        self.assertIsNotNone(state.last_full_pass_at)

    def test_budget_deferral_keeps_the_cursor_and_backs_off(self):
        self.slack.conversations_list.side_effect = BudgetDeferred(45)
        self.assertEqual(channel_lifecycle.reconcile_once(), "deferred")
        state = CommunityBridgeLifecycleState.objects.get()
        self.assertGreater(state.lease_expires_at, timezone.now() + timedelta(seconds=40))
        self.assertEqual(channel_lifecycle.reconcile_once(), "idle")

    @override_settings(COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED=False)
    def test_disabled_lanes_do_nothing(self):
        channel_lifecycle.queue_refresh(WORKSPACE, CHANNEL, reason="channel_created")
        self.assertEqual(channel_lifecycle.process_refreshes_once(), 0)
        self.assertEqual(channel_lifecycle.reconcile_once(), "disabled")
        self.slack.conversations_info.assert_not_called()


@override_settings(**LIFECYCLE_SETTINGS)
class BuzzBridgePublicChannelClientTests(TestCase):
    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_payload_omits_unmanaged_text_and_maps_conflicts(self, post):
        post.return_value = SimpleNamespace(
            ok=True, status_code=200, json=lambda: {"channel_id": ROOM, "created": True, "changed": ["created"]},
        )
        result = BuzzBridgeClient.upsert_public_channel(
            slack_workspace_id=WORKSPACE, slack_channel_id=CHANNEL, name="demo-day", archived=False, topic="Friday",
        )
        self.assertEqual(result, {"channel_id": ROOM, "created": True, "changed": ["created"]})
        self.assertTrue(post.call_args.args[0].endswith("/v1/public-channels"))
        self.assertEqual(post.call_args.kwargs["json"], {
            "slack_workspace_id": WORKSPACE, "slack_channel_id": CHANNEL, "name": "demo-day",
            "archived": False, "topic": "Friday",
        })

        post.return_value = SimpleNamespace(ok=False, status_code=409, json=lambda: {"error": "name_conflict"})
        with self.assertRaises(BuzzBridgePermanentError) as raised:
            BuzzBridgeClient.upsert_public_channel(
                slack_workspace_id=WORKSPACE, slack_channel_id=CHANNEL, name="events", archived=False,
            )
        self.assertEqual(raised.exception.adapter_error, "name_conflict")

        post.return_value = SimpleNamespace(
            ok=True, status_code=200, json=lambda: {"channel_id": ROOM, "created": False, "changed": []},
        )
        with self.assertRaises(BuzzBridgeError):
            BuzzBridgeClient.upsert_public_channel(
                slack_workspace_id=WORKSPACE, slack_channel_id=CHANNEL, name="events", archived=False,
                channel_id=LEGACY_ROOM,
            )
