"""Database-free regression checks for bounded unread scheduling and signals."""
from contextlib import nullcontext
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from integrations.services import slack_chat_read_state as reads, slack_owner_inventory as inventory
from integrations.services.message_sync import inbox, read_activity, read_priority as priority, read_state


class ReadPriorityReliabilityTests(SimpleTestCase):
    now = 1_800_000_000

    def target(self, source, activity=""):
        return reads.ReadTarget(source, source, "im", source_activity_ts=activity)

    def test_new_source_activity_promotes_a_previous_read_without_guessing_unread(self):
        changed, unread, quiet = [self.target(value) for value in ("Dchanged", "Dunread", "Dquiet")]
        changed.source_activity_ts = str(self.now - 10)
        snapshots = {
            "Dchanged": {"available": True, "is_unread": False, "fetched_at": self.now - 3600},
            "Dunread": {"available": True, "is_unread": True, "fetched_at": self.now - 120},
            "Dquiet": {"available": True, "is_unread": False, "fetched_at": self.now - 7200},
        }
        for turn in range(3):
            self.assertIs(priority.select_target([changed, unread, quiet], snapshots, lambda t: t.slack_id,
                                                {}, now=self.now, turn=turn), changed)
        self.assertFalse(snapshots["Dchanged"]["is_unread"])
        self.assertIs(priority.select_target([changed, unread, quiet], snapshots, lambda t: t.slack_id,
                                            {}, now=self.now, turn=3), quiet)
        snapshots["Dchanged"]["fetched_at"] = self.now
        self.assertIs(priority.select_target([changed, unread, quiet], snapshots, lambda t: t.slack_id,
                                            {}, now=self.now + 60, turn=0), unread)

    def test_activity_hint_survives_cooldown_but_does_not_bypass_it(self):
        hinted, quiet = self.target("Dhot"), self.target("Dquiet")
        hints = priority.merged_hints({}, ["Dhot"], now=self.now - 3600, reason="activity")
        cursor = {priority.KEY: hints, "message_sync_read_state_v1": {"retries": {"Dhot": self.now + 60}}}
        self.assertTrue(priority.hint_pending(hints["Dhot"], self.now))
        self.assertIs(priority.select_target([hinted, quiet], {}, lambda t: t.slack_id,
                                            cursor, now=self.now, turn=0), quiet)
        self.assertIs(priority.select_target([hinted, quiet], {}, lambda t: t.slack_id,
                                            cursor, now=self.now + 60, turn=0), hinted)

    def test_visibility_coalesces_while_new_activity_gets_a_distinct_generation(self):
        first = priority.merged_hints({}, ["D1"], now=self.now, reason="activity")
        visible = priority.merged_hints(first, ["D1"], now=self.now + 5, reason="visible")
        self.assertEqual(visible["D1"]["generation"], first["D1"]["generation"])
        self.assertEqual(visible["D1"]["last_requested_at"], self.now)
        self.assertEqual(visible["D1"]["reason"], "activity")
        next_event = priority.merged_hints(visible, ["D1"], now=self.now + 6, reason="activity")
        self.assertNotEqual(next_event["D1"]["generation"], first["D1"]["generation"])
        self.assertEqual(next_event["D1"]["requested_at"], self.now)
        self.assertEqual(next_event["D1"]["last_requested_at"], self.now + 6)
        burst = priority.merged_hints(first, [f"D{i}" for i in range(2, 400)], now=self.now + 1, reason="activity")
        self.assertEqual(len(burst), priority.MAX_HINTS)
        self.assertIn("D1", burst)

    def test_hint_consumption_fences_new_events_new_snapshots_and_old_info_checkpoints(self):
        hint = priority.merged_hints({}, ["D1"], now=self.now, reason="activity")["D1"]
        target = self.target("D1")
        snapshot = {"available": True, "fetched_at": self.now + 1, "revision": 11}
        for current, observed, cached, consumed in (
            ({**hint, "until": self.now + 500}, snapshot, snapshot, True),
            ({**hint, "generation": "new-event"}, snapshot, snapshot, False),
            (hint, snapshot, {**snapshot, "revision": 12}, False),
            (hint, {**snapshot, "fetched_at": self.now - 1}, snapshot, False),
            (hint, {**snapshot, "available": False}, snapshot, False),
        ):
            connection = SimpleNamespace(sync_cursor={priority.KEY: {"D1": current, "D2": {"other": True}}}, save=Mock())
            with patch.object(priority.transaction, "atomic", side_effect=nullcontext), patch.object(
                reads, "_lock_slack_grant_api_authority", return_value=(None, connection)
            ), patch.object(reads, "_cache_key", return_value="key"), patch.object(reads.cache, "get", return_value=cached):
                priority.satisfy_refresh(object(), target, hint, observed)
            self.assertEqual("D1" not in connection.sync_cursor[priority.KEY], consumed)
            self.assertIn("D2", connection.sync_cursor[priority.KEY])

    def test_legacy_hint_is_consumed_after_observation_and_unroutable_hints_are_bounded(self):
        legacy = {"until": self.now - 60, "requested_at": self.now - 360, "reason": "activity"}
        hints = {"D1": legacy, "revoked": legacy,
                 "discovering": {**legacy, "until": self.now + 1}, "malformed": "bad"}
        connection = SimpleNamespace(sync_cursor={priority.KEY: hints}, save=Mock())
        priority.prune_unroutable_hints(connection, {"D1"}, now=self.now)
        self.assertEqual(set(connection.sync_cursor[priority.KEY]), {"D1", "discovering"})
        snapshot = {"available": True, "revision": 5, "fetched_at": self.now}
        with patch.object(priority.transaction, "atomic", side_effect=nullcontext), patch.object(
            reads, "_lock_slack_grant_api_authority", return_value=(None, connection)
        ), patch.object(reads, "_cache_key", return_value="key"), patch.object(reads.cache, "get", return_value=snapshot):
            priority.satisfy_refresh(object(), self.target("D1"), legacy, snapshot)
        self.assertNotIn("D1", connection.sync_cursor[priority.KEY])
        for malformed in (None, True, [], "bad", {"until": float("nan")},
                          {"until": float("inf"), "reason": "activity"},
                          {"until": self.now + 5, "requested_at": "invalid"}):
            self.assertFalse(priority.hint_pending(malformed, self.now))

    def test_lost_authority_cannot_consume_hint_or_access_snapshot(self):
        from integrations.services.message_sync.scheduler import LeaseLost
        hint = priority.merged_hints({}, ["D1"], now=self.now, reason="activity")["D1"]
        with patch.object(priority.transaction, "atomic", side_effect=nullcontext), patch.object(
            reads, "_lock_slack_grant_api_authority", side_effect=LeaseLost("expired")
        ), patch.object(reads.cache, "get") as cached:
            with self.assertRaises(LeaseLost):
                priority.satisfy_refresh(object(), self.target("D1"), hint,
                                         {"available": True, "revision": 1, "fetched_at": self.now})
        cached.assert_not_called()
        self.assertEqual(hint["requested_at"], self.now)

    def test_progress_retains_unknowns_and_pending_work_without_source_identifiers(self):
        snapshots = {"D1": {"available": True, "is_unread": False, "fetched_at": self.now - 900},
                     "D2": {"available": False, "fetched_at": self.now - 100}}
        progress = priority.observation_progress([self.target("D1"), self.target("D2"), self.target("D3")],
                                                 snapshots, lambda t: t.slack_id, now=self.now)
        progress.update(priority.hint_progress({"D1": {"reason": "activity", "requested_at": self.now - 600,
                                                        "until": self.now - 300}}, now=self.now))
        self.assertEqual(progress["unknown_snapshot_count"], 2)
        self.assertEqual(progress["oldest_observation_age_seconds"], 900)
        self.assertEqual(progress["oldest_pending_hint_age_seconds"], 600)
        self.assertEqual(progress["pending_hint_count"], 1)
        self.assertNotIn("D1", str(progress))

    def test_stage_deferral_progress_preserves_account_and_discovery_checkpoints(self):
        connection = SimpleNamespace(sync_cursor={"discovery": {"cursor": "retained"},
            priority.KEY: priority.merged_hints({}, ["D1"], now=self.now - 600, reason="activity"),
            read_state.KEY: {"turn": 5, "progress": {"last_successful_observation_at": self.now - 20}}}, save=Mock())
        lease = SimpleNamespace(turn=5)
        with patch.object(read_state.transaction, "atomic", side_effect=nullcontext), patch.object(
            read_state, "_lock", return_value=(object(), connection)
        ), patch.object(read_state, "guard_read_state"), patch.object(read_state.timezone, "now") as clock:
            clock.return_value.timestamp.return_value = self.now
            read_state.finish_read_state(lease, after="D0", delay=1, failed_source="D1", retry_seconds=60,
                                         deferred_stage="conversations.history", deferred_seconds=60)
        state = connection.sync_cursor[read_state.KEY]
        self.assertEqual(state["retries"]["D1"], self.now + 60)
        self.assertEqual(state["progress"]["deferred_stage"], "conversations.history")
        self.assertEqual(state["progress"]["deferred_until"], self.now + 60)
        self.assertEqual(state["progress"]["deferred_turn_count"], 1)
        self.assertEqual(state["progress"]["last_successful_observation_at"], self.now - 20)
        self.assertNotIn("D1", str(state["progress"]))
        self.assertEqual(connection.sync_cursor["discovery"], {"cursor": "retained"})

    def test_unavailable_source_cursor_keeps_hint_and_uses_bounded_retry(self):
        target = self.target("D1")
        hints = priority.merged_hints({}, [target.slack_id], now=self.now - 30, reason="activity")
        connection = SimpleNamespace(sync_cursor={priority.KEY: hints}, save=Mock())
        grant = SimpleNamespace(pk=1, user_id=1, connection=connection)
        authority = SimpleNamespace(scopes={"im:read"})
        lease = read_state.ReadStateLease(1, 1, 1, "lease", None, "", turn=3)
        with patch.object(read_state, "enabled", return_value=True), patch.object(
            read_state, "claim_read_state", return_value=lease
        ), patch.object(read_state.SlackDmMirrorGrant.objects, "select_related") as grants, patch.object(
            read_state.CommunityChatDevice.objects, "filter"
        ) as devices, patch.object(reads, "_assert_grant_connection_authorized"), patch.object(
            reads, "_capture_slack_grant_api_authority", return_value=authority
        ), patch("integrations.services.message_sync.read_snapshots.flush_notification"), patch.object(
            reads, "_targets_for_keys", return_value=[target]
        ), patch.object(inventory, "source_read_targets", return_value=[]), patch.object(
            reads, "_lock_slack_grant_api_authority", return_value=(grant, connection)
        ), patch.object(read_state.transaction, "atomic", side_effect=nullcontext), patch.object(
            reads, "_cache_key", return_value="cache-key"
        ), patch.object(read_state.cache, "get_many", return_value={}), patch.object(
            reads, "refresh_target", return_value={"available": False, "fetched_at": self.now}
        ), patch.object(read_state, "finish_read_state") as finish:
            grants.return_value.get.return_value = grant
            devices.return_value.values_list.return_value = ["verified-device"]
            self.assertEqual(read_state.refresh_read_state_once(), 1)
        self.assertEqual(connection.sync_cursor[priority.KEY], hints)
        self.assertEqual(finish.call_args.kwargs["failed_source"], "D1")
        self.assertEqual(finish.call_args.kwargs["retry_seconds"], 60)
        self.assertIsNone(finish.call_args.kwargs["observed_at"])

    def test_inventory_recovery_activity_cannot_overwrite_newer_metadata_or_wake_twice(self):
        row = SimpleNamespace(pk=1, source_activity_ts="100.000001")
        target = self.target("D1")
        target.source_inventory = row
        directory = Mock()
        grant = SimpleNamespace(owner_conversation_inventory=directory)
        with patch.object(inventory, "has_metadata_consent", return_value=True), patch(
            "integrations.services.message_sync.device_recovery.schedule_source_recovery_locked"
        ) as wake:
            directory.filter.return_value.update.return_value = 0
            inventory.record_read_activity_locked(grant, object(), object(), target, "102.000001")
            wake.assert_not_called()
            directory.filter.assert_called_with(pk=1, slack_conversation_id="D1", eligibility="eligible",
                                                 source_activity_ts="100.000001")
            directory.filter.return_value.update.return_value = 1
            inventory.record_read_activity_locked(grant, object(), object(), target, "102.000001")
            inventory.record_read_activity_locked(grant, object(), object(), target, "102.000001")
            wake.assert_called_once()

    def test_routed_inventory_activity_merges_without_duplicate_probe(self):
        routed = self.target("D1")
        row = SimpleNamespace(slack_conversation_id="D1", kind="im", source_activity_ts=str(self.now - 10))
        directory = SimpleNamespace(filter=lambda **_: SimpleNamespace(order_by=lambda *_: [row]))
        grant = SimpleNamespace(connection=object(), owner_conversation_inventory=directory)
        authority = SimpleNamespace(scopes={"im:read"})
        with patch.object(inventory, "enabled", return_value=True), patch.object(
            inventory, "has_metadata_consent", return_value=True
        ), patch.object(inventory.time, "time", return_value=self.now):
            self.assertEqual(inventory.source_read_targets(grant, authority, [routed]), [])
        self.assertEqual(routed.source_activity_ts, row.source_activity_ts)
        self.assertIs(routed.source_inventory, row)
        snapshots = {"Dold": {"is_unread": True, "fetched_at": self.now - 100}}
        self.assertIs(priority.select_target([self.target("Dold"), routed], snapshots, lambda t: t.slack_id,
                                            {}, now=self.now, turn=2), routed)

    def test_six_owner_large_directory_simulation_keeps_dirty_and_background_progress(self):
        # Model the unchanged global 1.2-second admission floor. This exercises
        # selection under 8,184 cached rooms, not a claimed production ETA.
        owners = []
        for owner in range(6):
            targets = [self.target(f"D{owner}-{index:04}") for index in range(1364)]
            targets[0].source_activity_ts = str(self.now - 5)
            snapshots = {target.slack_id: {"available": True, "is_unread": False,
                                           "fetched_at": self.now - 86400 + index}
                         for index, target in enumerate(targets)}
            hints = priority.merged_hints({}, [targets[1].slack_id], now=self.now - 600, reason="activity")
            owners.append((targets, snapshots, {priority.KEY: hints}))
        observed, background = set(), set()
        for call in range(72):
            owner = call % 6
            targets, snapshots, cursor = owners[owner]
            now, turn = self.now + call * 1.2, call // 6
            target = priority.select_target(targets, snapshots, lambda t: t.slack_id,
                                            cursor, now=now, turn=turn)
            self.assertIsNotNone(target)
            if target in targets[:2]:
                observed.add((owner, target.slack_id))
            if turn % 4 == 3:
                background.add(owner)
            snapshots[target.slack_id] = {"available": True, "is_unread": False, "fetched_at": now}
            cursor[priority.KEY].pop(target.slack_id, None)
            if call == 17:
                self.assertEqual(len(observed), 12)
        self.assertEqual(background, set(range(6)))
        self.assertEqual(owners[0][1][owners[0][0][0].slack_id]["fetched_at"], self.now)
        self.assertEqual(owners[0][1][owners[0][0][1].slack_id]["fetched_at"], self.now + 7.2)


class PublicReadActivityTests(SimpleTestCase):
    def test_disabled_mapping_after_lookup_cannot_record_activity(self):
        from integrations.models import BridgeSyncState, CommunityBridgeChannel
        channel = SimpleNamespace(pk=1, slack_workspace_id="T1", slack_channel_id="C1", destination_channel_id="room")
        with patch.object(read_activity.transaction, "atomic", side_effect=nullcontext), patch.object(
            CommunityBridgeChannel.objects, "filter"
        ) as lookup, patch.object(BridgeSyncState.objects, "select_for_update") as state_lock, patch.object(
            CommunityBridgeChannel.objects, "select_for_update"
        ) as channel_lock, patch.object(read_activity, "advance_public_activity") as advance:
            lookup.return_value.exclude.return_value.first.return_value = channel
            state_lock.return_value.filter.return_value.first.return_value = object()
            channel_lock.return_value.filter.return_value.first.return_value = None
            read_activity.record_public_event({"team_id": "T1", "event": {
                "type": "message", "channel_type": "channel", "channel": "C1", "ts": "100.000001"}})
        advance.assert_not_called()

    def test_inbox_takes_activity_state_lock_before_public_ingestion_channel_lock(self):
        payload = {"team_id": "T1", "event": {"type": "message", "channel": "C1"}}
        row = SimpleNamespace(encrypted_payload=json.dumps(payload), lease_expires_at=timezone.now() + timedelta(seconds=60), save=Mock())
        order = []
        with patch.object(inbox, "claim_inbox", return_value=(1, "lease")), patch.object(
            inbox.transaction, "atomic", side_effect=nullcontext
        ), patch.object(inbox.BridgeSyncInbox.objects, "select_for_update") as receipt, patch.object(
            inbox, "expand_authorization_page", return_value=(payload, True)
        ), patch.object(read_activity, "record_public_event", side_effect=lambda _: order.append("activity")), patch(
            "integrations.services.slack_dm_mirror.ingest_slack_dm_event", side_effect=lambda _: order.append("private")
        ), patch("integrations.services.community_bridge.store.ingest_slack_event",
                 side_effect=lambda _: order.append("public")), patch.object(
            priority, "invalidate_event", side_effect=lambda _: order.append("hint")
        ):
            receipt.return_value.filter.return_value.first.return_value = row
            self.assertEqual(inbox.process_inbox_once(), 1)
        self.assertEqual(order, ["activity", "private", "public", "hint"])
        self.assertEqual(row.encrypted_payload, "")

    def test_verified_mapped_bot_event_records_only_timestamp_under_history_lock_order(self):
        from integrations.models import BridgeSyncState, CommunityBridgeChannel
        channel = SimpleNamespace(pk=1, slack_workspace_id="T1", slack_channel_id="C1", destination_channel_id="room")
        state = SimpleNamespace(public_channel_id=1, private_conversation_id=None, workspace_id="T1",
                                source_channel_id="C1", latest_source_activity="", save=Mock())
        order = []
        with patch.object(read_activity.transaction, "atomic", side_effect=nullcontext), patch.object(
            CommunityBridgeChannel.objects, "filter"
        ) as channel_query, patch.object(BridgeSyncState.objects, "select_for_update") as state_lock, patch.object(
            CommunityBridgeChannel.objects, "select_for_update"
        ) as channel_lock:
            channel_query.return_value.exclude.return_value.first.return_value = channel
            state_lock.return_value.filter.return_value.first.side_effect = lambda: (order.append("state") or state)
            channel_lock.return_value.filter.return_value.first.side_effect = lambda: (order.append("channel") or channel)
            read_activity.record_public_event({"team_id": "T1", "event": {"type": "message",
                "channel_type": "channel", "channel": "C1", "subtype": "bot_message",
                "ts": "100.000001", "text": "must not be copied"}})
        self.assertEqual(order, ["state", "channel"])
        self.assertEqual(state.latest_source_activity, "100.000001")
        state.save.assert_called_once_with(update_fields=["latest_source_activity"])
        self.assertNotIn("must not be copied", str(vars(state)))

    def test_public_activity_is_monotonic_and_excludes_control_and_thread_only_rows(self):
        state = SimpleNamespace(public_channel_id=1, private_conversation_id=None,
                                latest_source_activity="100.000001", save=Mock())
        with patch.object(read_activity.time, "time", return_value=1000):
            read_activity.advance_public_activity(state, [
                {"ts": "103.000001", "subtype": "channel_join"},
                {"ts": "104.000001", "thread_ts": "100.000001"},
                {"ts": "102.000001", "subtype": "bot_message", "text": "not retained"},
            ])
            self.assertEqual(state.latest_source_activity, "102.000001")
            read_activity.advance_public_activity(state, [{"ts": "101.000001"}])
        state.save.assert_called_once_with(update_fields=["latest_source_activity"])
        self.assertNotIn("not retained", str(vars(state)))

    def test_unmapped_or_private_callbacks_do_not_create_public_activity(self):
        from integrations.models import CommunityBridgeChannel
        with patch.object(CommunityBridgeChannel.objects, "filter") as lookup:
            for event in ({"type": "message", "channel_type": "group", "ts": "100.000001"},
                          {"type": "message", "channel_type": "im", "ts": "100.000001"},
                          {"type": "reaction_added", "ts": "100.000001"}):
                read_activity.record_public_event({"team_id": "T1", "event": event})
            lookup.assert_not_called()
            lookup.return_value.exclude.return_value.first.return_value = None
            read_activity.record_public_event({"team_id": "T1", "event": {
                "type": "message", "channel_type": "channel", "channel": "C1",
                "ts": "100.000001", "subtype": "bot_message",
            }})
            lookup.assert_called_once_with(slack_workspace_id="T1", slack_channel_id="C1",
                                           destination_platform="buzz", enabled=True)
