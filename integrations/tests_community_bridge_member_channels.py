"""Disposable-database regressions for retained member-channel helpers."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from integrations.models import CommunityBridgeChannel, CommunityBridgeChannelRefresh
from integrations.message_sync_models import BridgeSyncState
from integrations.services.community_bridge import member_channels as channels
from integrations.services.community_bridge.buzz import BuzzBridgeClient, BuzzBridgePermanentError
from integrations.services.community_bridge.slack import SlackBridgeClient


WORKSPACE = "TTESTMEMBERS"
CHANNEL = "CTESTMEMBERS"
ROOM = "5f0c4b1e-8a35-5d55-9d2e-0b6f4b8c2a11"


class MemberChannelQueueTests(TestCase):
    def test_reader_hint_coalesces_by_observation_and_survives_generic_and_stale_claim(self):
        observed = timezone.now() - timedelta(seconds=1)
        first = channels.info_reader_hint_reason(10, observed)
        newer = channels.info_reader_hint_reason(11, observed + timedelta(microseconds=1))
        older = channels.info_reader_hint_reason(12, observed - timedelta(microseconds=1))
        channels.queue_refresh(WORKSPACE, CHANNEL, reason=first)
        claimed = channels.claim_refreshes()[0]
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="metadata_audit")
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().reason, first)
        channels.queue_refresh(WORKSPACE, CHANNEL, reason=newer)
        channels.queue_refresh(WORKSPACE, CHANNEL, reason=older)
        self.assertFalse(channels.complete_refresh(claimed))
        channels._retry_refresh(claimed, seconds=60, code="old_error")
        row = CommunityBridgeChannelRefresh.objects.get()
        self.assertEqual((row.reason, row.attempts, row.last_error_code), (newer, 0, ""))

    def test_malformed_hint_cannot_be_truncated_into_authority_or_replace_valid_hint(self):
        valid = channels.info_reader_hint_reason(10, timezone.now())
        channels.queue_refresh(WORKSPACE, CHANNEL, reason=valid)
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="i:1:1" + " " * 50)
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().reason, valid)
        CommunityBridgeChannelRefresh.objects.all().delete()
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="i:1:1" + " " * 50)
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().reason, "public_source_unverified")

    def test_queue_coalesces_and_claim_is_a_lease_without_reader_io(self):
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="rename")
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="topic")
        with patch.object(SlackBridgeClient, "get_client") as slack:
            claimed = channels.claim_refreshes()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].reason, "topic")
        self.assertEqual(channels.claim_refreshes(), [])
        slack.assert_not_called()
        self.assertTrue(channels.complete_refresh(claimed[0]))
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())

    def test_new_event_survives_old_claim_completion_retry_and_abandonment(self):
        for action in ("complete", "retry", "abandon"):
            with self.subTest(action=action):
                channels.queue_refresh(WORKSPACE, CHANNEL, reason="old")
                claimed = channels.claim_refreshes()[0]
                channels.queue_refresh(WORKSPACE, CHANNEL, reason="new")
                if action == "complete":
                    self.assertFalse(channels.complete_refresh(claimed))
                else:
                    claimed.attempts = 99 if action == "abandon" else 0
                    channels._retry_refresh(claimed, seconds=30, code="old_error")
                row = CommunityBridgeChannelRefresh.objects.get()
                self.assertEqual((row.reason, row.attempts, row.last_error_code), ("new", 0, ""))
                row.delete()

    def test_budget_deferral_does_not_consume_attempt_and_exhaustion_is_bounded(self):
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="event")
        row = channels.claim_refreshes()[0]
        before = timezone.now()
        channels._retry_refresh(row, seconds=45, code="budget_deferred", count_attempt=False)
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)
        self.assertGreaterEqual(row.available_at, before + timedelta(seconds=45))
        row.attempts = channels.REFRESH_MAX_ATTEMPTS - 1
        channels._retry_refresh(row, seconds=10, code="unavailable")
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())

    def test_expired_worker_cannot_complete_a_newer_lease(self):
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="event")
        old = channels.claim_refreshes()[0]
        CommunityBridgeChannelRefresh.objects.filter(pk=old.pk).update(
            available_at=timezone.now() - timedelta(seconds=1),
        )
        current = channels.claim_refreshes()[0]
        self.assertFalse(channels.complete_refresh(old))
        self.assertTrue(channels.complete_refresh(current))


class MemberChannelRoomTests(TestCase):
    def row(self, **changes):
        return CommunityBridgeChannel.objects.create(**({
            "slack_workspace_id": WORKSPACE, "slack_channel_id": CHANNEL,
            "slack_channel_name": "planning", "destination_platform": "buzz",
            "audience": "members", "enabled": False, "room_sync_pending": True,
            "lifecycle_source": "event", "slack_purpose": "Planning", "slack_topic": "Friday",
        } | changes))

    def upsert(self, **changes):
        return patch.object(BuzzBridgeClient, "upsert_public_channel", return_value={
            "channel_id": ROOM, "created": True, "changed": ["created"], **changes,
        })

    def test_sync_is_audience_explicit_and_never_opens_delivery_or_joins_slack(self):
        for audience in ("members", "community"):
            with self.subTest(audience=audience):
                row = self.row(audience=audience)
                with self.upsert() as upsert, patch.object(SlackBridgeClient, "get_client") as slack:
                    self.assertEqual(channels.sync_room(row.pk), "synced")
                row.refresh_from_db()
                self.assertEqual(row.destination_channel_id, ROOM)
                self.assertFalse(row.enabled)
                self.assertFalse(row.room_sync_pending)
                self.assertEqual(upsert.call_args.kwargs["audience"], audience)
                self.assertEqual(row.room_state["applied"]["audience"], audience)
                slack.assert_not_called()
                row.delete()

    def test_metadata_update_preserves_existing_community_delivery_gate(self):
        row = self.row(audience="community", enabled=True, destination_channel_id=ROOM)
        with self.upsert():
            self.assertEqual(channels.sync_room(row.pk), "synced")
        row.refresh_from_db()
        self.assertTrue(row.enabled)

    def test_archive_stops_delivery_and_operator_text_remains_unmanaged(self):
        row = self.row(
            audience="community", enabled=True, destination_channel_id=ROOM,
            lifecycle_source="", slack_archived=True,
        )
        with self.upsert() as upsert:
            self.assertEqual(channels.sync_room(row.pk), "synced")
        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertIsNone(upsert.call_args.kwargs["about"])
        self.assertIsNone(upsert.call_args.kwargs["topic"])
        self.assertTrue(upsert.call_args.kwargs["archived"])

    def test_room_audience_cannot_be_changed_after_successful_sync(self):
        row = self.row(destination_channel_id=ROOM, room_state={"applied": {"audience": "members"}})
        CommunityBridgeChannel.objects.filter(pk=row.pk).update(audience="community")
        with self.upsert() as upsert:
            self.assertEqual(channels.sync_room(row.pk), "failed")
        upsert.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.room_sync_error, "immutable_room_audience")
        self.assertEqual(row.destination_channel_id, ROOM)
        self.assertFalse(row.enabled)

    def test_inflight_identity_change_does_not_apply_stale_room_result(self):
        row = self.row()
        def moved(**kwargs):
            CommunityBridgeChannel.objects.filter(pk=row.pk).update(slack_workspace_id="TOTHER")
            return {"channel_id": ROOM, "changed": ["created"]}
        with patch.object(BuzzBridgeClient, "upsert_public_channel", side_effect=moved):
            self.assertEqual(channels.sync_room(row.pk), "retry")
        row.refresh_from_db()
        self.assertEqual(row.destination_channel_id, "")
        self.assertFalse(row.enabled)
        self.assertNotIn("applied", row.room_state)

    def test_name_collision_holds_new_room_without_changing_audience(self):
        row = self.row()
        with patch.object(BuzzBridgeClient, "upsert_public_channel", side_effect=BuzzBridgePermanentError(
            "synthetic conflict", adapter_error="name_conflict",
        )):
            self.assertEqual(channels.sync_room(row.pk), "held")
        row.refresh_from_db()
        self.assertEqual((row.audience, row.mirror_decision, row.hold_reason), ("members", "hold", "name_conflict"))
        self.assertFalse(row.enabled)

    def test_stale_name_conflict_cannot_replace_admin_exclude_or_new_metadata(self):
        for changes in (
            {"mirror_decision": "exclude", "hold_reason": "admin", "enabled": False},
            {"slack_channel_name": "new-name"},
        ):
            with self.subTest(changes=changes):
                row = self.row()
                def stale_conflict(**kwargs):
                    CommunityBridgeChannel.objects.filter(pk=row.pk).update(**changes)
                    raise BuzzBridgePermanentError("synthetic old request", adapter_error="name_conflict")
                with patch.object(BuzzBridgeClient, "upsert_public_channel", side_effect=stale_conflict):
                    self.assertEqual(channels.sync_room(row.pk), "retry")
                row.refresh_from_db()
                for field, value in changes.items():
                    self.assertEqual(getattr(row, field), value)
                self.assertEqual(row.room_sync_error, "")
                self.assertTrue(row.room_sync_pending)
                row.delete()

    def test_non_buzz_mapping_is_never_sent_to_room_adapter(self):
        row = self.row(destination_platform="discord")
        with self.upsert() as upsert:
            self.assertEqual(channels.sync_room(row.pk), "failed")
        upsert.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.room_sync_error, "unsupported_destination")

    def test_mapped_rekey_preserves_immutable_room_and_source_identity(self):
        for audience in ("community", "members"):
            with self.subTest(audience=audience):
                row = self.row(audience=audience, destination_channel_id=ROOM, room_sync_pending=False)
                state = BridgeSyncState.objects.create(
                    public_channel=row, workspace_id=WORKSPACE, source_channel_id=CHANNEL,
                )
                self.assertFalse(channels.rekey_channel(CHANNEL, "CNEWIDENTITY"))
                row.refresh_from_db()
                state.refresh_from_db()
                self.assertEqual((row.slack_channel_id, row.destination_channel_id, row.audience), (CHANNEL, ROOM, audience))
                self.assertEqual(state.source_channel_id, CHANNEL)
                self.assertEqual(row.room_sync_error, "rekey_requires_room_migration")
                self.assertFalse(row.room_sync_pending)
                with self.upsert() as upsert:
                    self.assertEqual(channels.sync_room(row.pk), "held")
                upsert.assert_not_called()
                queued = CommunityBridgeChannelRefresh.objects.get()
                self.assertEqual((queued.slack_channel_id, queued.reason), (CHANNEL, "rekey_requires_room_migration"))
                queued.delete()
                row.delete()

    def test_unmapped_rekey_updates_source_and_queue_without_creating_a_room(self):
        row = self.row(audience="community")
        state = BridgeSyncState.objects.create(public_channel=row, workspace_id=WORKSPACE, source_channel_id=CHANNEL)
        channels.queue_refresh(WORKSPACE, CHANNEL, reason="old")
        self.assertTrue(channels.rekey_channel(CHANNEL, "CNEWIDENTITY"))
        row.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(row.slack_channel_id, "CNEWIDENTITY")
        self.assertEqual(state.source_channel_id, "CNEWIDENTITY")
        self.assertEqual(row.destination_channel_id, "")
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().slack_channel_id, "CNEWIDENTITY")

    def test_rekey_hold_survives_an_already_inflight_adapter_response(self):
        row = self.row(destination_channel_id=ROOM)
        def rekeyed(**kwargs):
            self.assertFalse(channels.rekey_channel(CHANNEL, "CNEWIDENTITY"))
            return {"channel_id": ROOM, "changed": ["name"]}
        with patch.object(BuzzBridgeClient, "upsert_public_channel", side_effect=rekeyed):
            self.assertEqual(channels.sync_room(row.pk), "held")
        row.refresh_from_db()
        self.assertEqual(row.room_sync_error, "rekey_requires_room_migration")
        self.assertNotIn("applied", row.room_state)
