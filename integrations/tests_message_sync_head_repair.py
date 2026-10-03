"""Database/network-free coverage of incremental repair and paced event wakeups."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.message_sync import head_repair as repair, history, private_history
from integrations.services.message_sync.scheduler import validate_checkpoint


@override_settings(MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=True)
class HeadRepairPolicyTests(SimpleTestCase):
    now = 1_800_000_000
    scope = "1:7:workspace:source:room:audience"

    def state(self, **policy):
        return SimpleNamespace(head_cursor=policy, save=Mock(), jobs=Mock(), status="current")

    def checkpoint(self, state, *, at=None, checkpoint=None, days=7, scope=None):
        now = self.now if at is None else at
        return repair.prepare_head(state, checkpoint or {}, now=now,
                                   consent_floor=now - days * 86400, scope=scope or self.scope)

    def completed(self, state, checkpoint, *, at=None, messages=(), limited=False):
        checkpoint = repair.observe_head(checkpoint, messages)
        return repair.finish_head(state, {**checkpoint, "source_limited": limited}, complete=True,
                                  now=self.now if at is None else at, scope=self.scope)

    def test_first_scan_keeps_daily_head_and_then_uses_overlap_after_complete(self):
        state = self.state()
        first = self.checkpoint(state)
        self.assertEqual(first["oldest"], f"{self.now - 86400}.000000")
        self.assertEqual(self.completed(state, first), 120)
        next_page = self.checkpoint(state, at=self.now + 120)
        self.assertEqual(next_page["oldest"], f"{self.now - 300}.000000")
        self.assertEqual(next_page["phase"], "head_delta")
        validate_checkpoint(repair.observe_head(next_page, []))

    def test_quiet_overlap_does_not_keep_room_hot_and_backoff_is_bounded(self):
        state = self.state()
        delays = []
        for step in range(8):
            now = self.now + step * 60
            checkpoint = self.checkpoint(state, at=now)
            delays.append(self.completed(state, checkpoint, at=now, messages=[
                {"ts": f"{self.now - 3600}.000001", "text": "never retained"},
            ]))
        self.assertEqual(delays, [120, 240, 480, 900, 900, 900, 900, 900])
        self.assertNotIn("never retained", str(state.head_cursor))

    @override_settings(MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=False)
    def test_disabled_quiet_backoff_keeps_minute_repairs_and_incremental_progress(self):
        state = self.state()
        for step in range(8):
            now = self.now + step * 60
            checkpoint = self.checkpoint(state, at=now)
            if step:
                self.assertEqual(checkpoint["oldest"], f"{now - 60 - repair.OVERLAP_SECONDS}.000000")
            self.assertEqual(self.completed(state, checkpoint, at=now), 60)
            self.assertEqual(state.head_cursor["completed_upper"], f"{now}.999999")

    def test_new_message_resets_quiet_streak_but_preserves_incremental_boundary(self):
        state = self.state(version=1, scope=self.scope, quiet_runs=5,
                           completed_upper=f"{self.now - 30}.999999", deep_checked_at=self.now - 60)
        checkpoint = self.checkpoint(state)
        self.assertEqual(self.completed(state, checkpoint, messages=[{"ts": f"{self.now - 1}.000001"}]), 60)
        self.assertEqual(state.head_cursor["quiet_runs"], 0)
        self.assertEqual(checkpoint["oldest"], f"{self.now - 330}.000000")

    def test_outage_gap_longer_than_day_is_not_truncated(self):
        state = self.state(version=1, scope=self.scope, completed_upper=f"{self.now - 3 * 86400}.999999")
        checkpoint = self.checkpoint(state)
        self.assertEqual(checkpoint["oldest"], f"{self.now - 3 * 86400 - 300}.000000")
        self.assertEqual(checkpoint["phase"], "head_deep")
        state.head_cursor["completed_upper"] = f"{self.now - 10 * 86400}.999999"
        self.assertEqual(self.checkpoint(state)["oldest"], f"{self.now - 7 * 86400}.000000")

    def test_periodic_deep_pass_repairs_older_changes_without_resetting_watermark(self):
        state = self.state(version=1, scope=self.scope, completed_upper=f"{self.now - 60}.999999",
                           deep_checked_at=self.now - repair.DEEP_SCAN_SECONDS)
        checkpoint = self.checkpoint(state)
        self.assertEqual(checkpoint["oldest"], f"{self.now - 86400}.000000")
        self.assertEqual(self.completed(state, checkpoint, messages=[
            {"ts": f"{self.now - 3600}.000001", "edited": {"ts": f"{self.now - 1}.000001"}},
        ]), 60)
        self.assertEqual(state.head_cursor["deep_checked_at"], self.now)

    def test_partial_limited_and_invalid_future_completion_never_advance_watermark(self):
        for complete, limited, future in ((False, False, False), (True, True, False), (True, False, True)):
            with self.subTest(complete=complete, limited=limited, future=future):
                state = self.state(version=1, scope=self.scope, completed_upper=f"{self.now - 60}.999999")
                original = deepcopy(state.head_cursor)
                checkpoint = self.checkpoint(state)
                if future:
                    checkpoint["upper_bound"] = str(self.now + 20)
                checkpoint["source_limited"] = limited
                self.assertEqual(repair.finish_head(state, checkpoint, complete=complete,
                                                    now=self.now, scope=self.scope), 60)
                self.assertEqual(state.head_cursor, original)
                state.save.assert_not_called()

    def test_pagination_keeps_bounds_and_activity_across_worker_restart(self):
        state = self.state()
        first = self.checkpoint(state)
        pending = repair.observe_head({**first, "cursor": "page2"}, [{"ts": f"{self.now - 1}.000001"}])
        resumed = self.checkpoint(state, at=self.now + 120, checkpoint=pending)
        self.assertEqual(resumed, pending)
        self.assertEqual(self.completed(state, resumed, at=self.now + 120), 60)
        self.assertEqual(state.head_cursor["completed_upper"], first["upper_bound"])

    def test_scope_change_restarts_pages_and_does_not_reuse_previous_audience_watermark(self):
        state = self.state(version=1, scope="old-room", completed_upper=f"{self.now - 60}.999999")
        old_page = {"authority_generation": "old-room", "cursor": "old-page",
                    "oldest": f"{self.now - 86400}.000000", "upper_bound": f"{self.now}.999999"}
        checkpoint = self.checkpoint(state, checkpoint=old_page)
        self.assertNotIn("cursor", checkpoint)
        self.assertEqual(checkpoint["oldest"], f"{self.now - 86400}.000000")
        self.assertEqual(checkpoint["authority_generation"], self.scope)

    def test_narrower_consent_drops_out_of_window_pagination(self):
        state = self.state()
        checkpoint = self.checkpoint(state, checkpoint={
            "cursor": "old-page", "upper_bound": f"{self.now}.999999",
            "oldest": f"{self.now - 30 * 86400}.000000",
        }, days=7)
        self.assertNotIn("cursor", checkpoint)
        self.assertGreaterEqual(int(checkpoint["oldest"].split(".")[0]), self.now - 7 * 86400)

    def test_event_arriving_during_scan_keeps_next_repair_hot(self):
        state = self.state()
        checkpoint = self.checkpoint(state)
        state.head_cursor["wake_requested_at"] = self.now + 1.5
        self.assertEqual(self.completed(state, checkpoint, at=self.now + 2), 60)
        self.assertEqual(state.head_cursor["wake_requested_at"], self.now + 1.5)
        self.assertEqual(state.head_cursor["completed_upper"], checkpoint["upper_bound"])

    def test_same_second_event_after_request_start_prevents_quiet_backoff(self):
        state = self.state(version=1, scope=self.scope, quiet_runs=5,
                           completed_upper=f"{self.now - 900}.999999", deep_checked_at=self.now - 900)
        checkpoint = self.checkpoint(state, at=self.now + .1)
        # No message was present in the response taken at .1; its callback
        # arrives at .7, which is still less than the query's .999999 bound.
        repair.wake_head_locked(state, now=self.now + .7)
        self.assertEqual(self.completed(state, checkpoint, at=self.now + .8), 60)
        self.assertEqual(state.head_cursor["quiet_runs"], 0)

    def test_coalesced_event_during_scan_is_not_lost_after_earlier_wake(self):
        state = self.state(version=1, scope=self.scope, quiet_runs=5,
                           completed_upper=f"{self.now - 900}.999999", deep_checked_at=self.now - 900,
                           wake_requested_at=self.now - 10)
        checkpoint = self.checkpoint(state, at=self.now + .1)
        repair.wake_head_locked(state, now=self.now + .7)
        state.save.assert_not_called()  # Scheduling remains coalesced.
        self.assertEqual(self.completed(state, checkpoint, at=self.now + .8), 60)
        later = self.checkpoint(state, at=self.now + 61)
        self.assertEqual(self.completed(state, later, at=self.now + 61), 120)

    def test_malformed_activity_never_corrupts_checkpoint_or_pacing(self):
        state = self.state(version=1, scope=self.scope, quiet_runs="bad", completed_upper="NaN")
        checkpoint = self.checkpoint(state)
        result = repair.observe_head(checkpoint, [{"ts": "NaN", "latest_reply": "Infinity", "edited": []}])
        validate_checkpoint(result)
        self.assertEqual(self.completed(state, result), 120)

    def test_malformed_upper_bound_is_rejected_with_a_bounded_machine_error(self):
        for value in (None, "NaN", "Infinity", "bad"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^invalid_history_page$"):
                self.checkpoint(self.state(), checkpoint={"upper_bound": value})

    def test_wake_is_coalesced_and_preserves_retry_lease_cursor_and_fairness(self):
        state = self.state(version=1, scope=self.scope, quiet_runs=5,
                           completed_upper=f"{self.now - 10}.000000")
        repair.wake_head_locked(state, now=self.now)
        state.jobs.select_for_update.assert_called_once_with(skip_locked=True)
        query = state.jobs.select_for_update.return_value
        kwargs = query.filter.call_args.kwargs
        self.assertEqual(kwargs["backoff_seconds"], 0)
        self.assertEqual(kwargs["last_error_code"], "")
        self.assertEqual(kwargs["due_at__gt"].timestamp(), self.now + 50)
        job = query.filter.return_value.filter.return_value.order_by.return_value.first.return_value
        self.assertEqual(job.due_at.timestamp(), self.now + 50)
        job.save.assert_called_once_with(update_fields=["due_at"])
        self.assertEqual(state.head_cursor["quiet_runs"], 5)
        repair.wake_head_locked(state, now=self.now + 5)
        self.assertEqual(state.save.call_count, 1)

    def test_busy_head_job_is_skipped_after_state_lock_without_losing_wake_hint(self):
        state = self.state(version=1, scope=self.scope, quiet_runs=5)
        query = state.jobs.select_for_update.return_value
        query.filter.return_value.filter.return_value.order_by.return_value.first.return_value = None
        repair.wake_head_locked(state, now=self.now)
        state.jobs.select_for_update.assert_called_once_with(skip_locked=True)
        self.assertEqual(state.head_cursor["wake_requested_at"], self.now)
        self.assertEqual(state.head_cursor["quiet_runs"], 5)
        state.save.assert_called_once_with(update_fields=["head_cursor"])
        state.status = "revoked"
        repair.wake_head_locked(state, now=self.now + 60)
        self.assertEqual(state.save.call_count, 1)

    def test_private_wake_filters_exact_owner_workspace_sources_and_live_state(self):
        from integrations.models import BridgeSyncState
        authority = SimpleNamespace(workspace_id="T1", grant_id=12, scopes={"im:read"})
        targets = [SimpleNamespace(slack_id="D1", read_scope="im:read"),
                   SimpleNamespace(slack_id="Csecret", read_scope="groups:read")]
        with patch.object(BridgeSyncState.objects, "select_for_update") as query, patch.object(repair, "wake_head_locked") as wake:
            query.return_value.filter.return_value.exclude.return_value.order_by.return_value = ["state"]
            repair.wake_private_targets(authority, targets)
        self.assertEqual(query.return_value.filter.call_args.kwargs, {
            "workspace_id": "T1", "source_channel_id__in": {"D1"},
            "private_conversation__grant_id": 12, "private_conversation__status": "live",
        })
        wake.assert_called_once_with("state")

    def test_public_wake_waits_for_owner_transaction_and_keeps_exact_mapping(self):
        from integrations.models import BridgeSyncState
        authority = SimpleNamespace(workspace_id="T1", scopes={"channels:read"})
        targets = [SimpleNamespace(slack_id="C1", channel_id="room", kind="public_channel", read_scope="channels:read")]
        callbacks = []
        with patch.object(repair.transaction, "on_commit", side_effect=lambda fn, **kw: callbacks.append(fn)), patch.object(
            BridgeSyncState.objects, "filter"
        ) as versions, patch.object(
            BridgeSyncState.objects, "select_for_update"
        ) as query, patch.object(repair, "wake_head_locked") as wake, patch.object(repair.transaction, "atomic", side_effect=nullcontext):
            versions.return_value.values_list.return_value = [(1, 7)]
            repair.defer_public_target_wake(authority, targets)
            query.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            query.return_value.filter.return_value.exclude.return_value.order_by.return_value = ["state"]
            callbacks[0]()
        criteria = query.return_value.filter.call_args
        query.assert_called_once_with(skip_locked=True, of=("self",))
        self.assertEqual(criteria.kwargs, {"workspace_id": "T1", "public_channel__enabled": True,
                                          "public_channel__destination_platform": "buzz"})
        self.assertIn("('source_channel_id', 'C1')", str(criteria.args[0]))
        self.assertIn("('public_channel__destination_channel_id', 'room')", str(criteria.args[0]))
        self.assertIn("('authority_generation', 7)", str(criteria.args[1]))
        wake.assert_called_once_with("state")

    def test_locked_public_mapping_is_skipped_without_touching_or_waiting_for_it(self):
        from integrations.models import BridgeSyncState
        authority = SimpleNamespace(workspace_id="T1", scopes={"channels:read"})
        targets = [SimpleNamespace(slack_id="C1", channel_id="room", kind="public_channel", read_scope="channels:read")]
        with patch.object(repair.transaction, "on_commit") as queue, patch.object(
            BridgeSyncState.objects, "filter"
        ) as versions, patch.object(repair.transaction, "atomic", side_effect=nullcontext), patch.object(
            BridgeSyncState.objects, "select_for_update"
        ) as query, patch.object(repair, "wake_head_locked") as wake:
            versions.return_value.values_list.return_value = [(1, 7)]
            query.return_value.filter.return_value.exclude.return_value.order_by.return_value = []
            repair.defer_public_target_wake(authority, targets)
            queue.call_args.args[0]()
        query.assert_called_once_with(skip_locked=True, of=("self",))
        wake.assert_not_called()

    def test_failed_deferred_public_wake_cannot_fail_the_completed_read(self):
        from integrations.models import BridgeSyncState
        authority = SimpleNamespace(workspace_id="T1", scopes={"channels:read"})
        targets = [SimpleNamespace(slack_id="C1", channel_id="room", kind="public_channel", read_scope="channels:read")]
        with patch.object(repair.transaction, "on_commit") as queue, patch.object(
            BridgeSyncState.objects, "filter"
        ) as versions, patch.object(repair.transaction, "atomic", side_effect=nullcontext), patch.object(
            BridgeSyncState.objects, "select_for_update", side_effect=RuntimeError("unavailable")
        ), patch.object(repair.logger, "warning") as log:
            versions.return_value.values_list.return_value = [(1, 7)]
            repair.defer_public_target_wake(authority, targets)
            self.assertTrue(queue.call_args.kwargs["robust"])
            queue.call_args.args[0]()
        log.assert_called_once_with("message_sync_public_head_wake_failed")

    def test_private_or_unscoped_targets_do_not_enqueue_public_wake(self):
        authority = SimpleNamespace(workspace_id="T1", scopes={"im:read"})
        targets = [SimpleNamespace(slack_id="C1", channel_id="room", kind="public_channel", read_scope="channels:read"),
                   SimpleNamespace(slack_id="D1", channel_id="private", kind="im", read_scope="im:read")]
        with patch.object(repair.transaction, "on_commit") as queue:
            repair.defer_public_target_wake(authority, targets)
        queue.assert_not_called()

    def test_failed_public_mapping_lookup_does_not_break_owner_read(self):
        from integrations.models import BridgeSyncState
        authority = SimpleNamespace(workspace_id="T1", scopes={"channels:read"})
        targets = [SimpleNamespace(slack_id="C1", channel_id="room", kind="public_channel", read_scope="channels:read")]
        with patch.object(repair.transaction, "on_commit") as queue, patch.object(
            repair.transaction, "atomic", side_effect=nullcontext
        ), patch.object(BridgeSyncState.objects, "filter", side_effect=RuntimeError("unavailable")), patch.object(
            repair.logger, "warning"
        ) as log:
            repair.defer_public_target_wake(authority, targets)
        queue.assert_not_called()
        log.assert_called_once_with("message_sync_public_head_wake_failed")


@override_settings(MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=True)
class HeadRepairPageIntegrationTests(SimpleTestCase):
    now = 1_800_000_000

    def test_public_head_persists_only_completed_upper_and_reuses_it_next_pass(self):
        channel = SimpleNamespace(pk=1, slack_workspace_id="T1", slack_channel_id="C1",
                                  destination_platform="buzz", destination_workspace_id="relay", destination_channel_id="room",
                                  enabled=True)
        state = SimpleNamespace(public_channel=channel, public_channel_id=1, private_conversation_id=None,
                                authority_generation=1, head_cursor={}, verified_ranges={}, latest_source_activity="", save=Mock())
        lease = SimpleNamespace(kind="head", checkpoint={})
        client = Mock()
        client.conversations_history.return_value = {"ok": True, "messages": []}
        with patch.object(history.time, "time", return_value=self.now), patch.object(history.transaction, "atomic", side_effect=nullcontext), patch.object(
            history, "locked_job", return_value=(state, None)
        ), patch.object(history.CommunityBridgeChannel.objects, "select_for_update") as channel_lock, patch.object(
            history.SlackBridgeClient, "get_client", return_value=client
        ), patch.object(history, "finish_job") as finish:
            channel_lock.return_value.get.return_value = channel
            history.public_page(lease, state)
            self.assertEqual(state.head_cursor["completed_upper"], f"{self.now}.999999")
            self.assertEqual(finish.call_args.kwargs["delay_seconds"], 120)
            history.public_page(lease, state)
        self.assertEqual(client.conversations_history.call_args.kwargs["oldest"], f"{self.now - 300}.000000")

    def test_private_head_keeps_authority_fences_and_incremental_watermark(self):
        self._exercise_private_head()

    def test_private_head_scope_change_replaces_page_and_scan_epoch(self):
        self._exercise_private_head(checkpoint={
            "authority_generation": "1:7:T1:D1:old-room:old-audience", "scan_id": "old-scan",
            "cursor": "old-page", "oldest": f"{self.now - 60}.000000", "upper_bound": f"{self.now}.999999",
        })

    def test_private_head_reduced_consent_replaces_legacy_page_and_scan_epoch(self):
        self._exercise_private_head(checkpoint={
            "scan_id": "old-scan", "cursor": "old-page", "oldest": f"{self.now - 30 * 86400}.000000",
            "upper_bound": f"{self.now}.999999",
        })

    def _exercise_private_head(self, checkpoint=None):
        from integrations.services import slack_dm_mirror as dm
        grant = SimpleNamespace(pk=2)
        conversation = SimpleNamespace(pk=1, grant=grant, grant_id=2, slack_workspace_id="T1", slack_conversation_id="D1",
                                       mlai_channel_id="room", participant_hash="audience")
        state = SimpleNamespace(private_conversation=conversation, authority_generation=1, head_cursor={},
                                verified_ranges={}, save=Mock(), refresh_from_db=Mock())
        lease = SimpleNamespace(kind="head", checkpoint=checkpoint or {})
        with patch.object(private_history.time, "time", return_value=self.now), patch.object(
            private_history.transaction, "atomic", side_effect=nullcontext
        ), patch.object(dm, "_capture_slack_grant_api_authority", return_value="authority"), patch.object(
            dm, "conversation_kind", return_value="im"
        ), patch.object(dm, "_history_required_scopes", return_value={"im:history"}), patch.object(
            dm, "_grant_history_days", return_value=7
        ), patch.object(dm, "_locked_history_write_context", return_value=(conversation, grant)) as authority_lock, patch.object(
            private_history, "locked_job", return_value=(state, None)
        ), patch.object(private_history, "read_boundary", side_effect=lambda *args, **kw: SimpleNamespace(epoch=kw["epoch"])) as boundary, patch.object(
            dm, "_call_slack_with_grant_authority", return_value={"ok": True, "messages": []}
        ) as source, patch("integrations.services.message_sync.publication.record_publication_locked"), patch.object(
            private_history, "finish_job"
        ) as finish:
            private_history.private_page(lease, state)
            self.assertEqual(state.head_cursor["completed_upper"], f"{self.now}.999999")
            if checkpoint:
                self.assertNotIn("cursor", source.call_args.kwargs)
                self.assertNotEqual(boundary.call_args_list[0].kwargs["epoch"], "old-scan")
            lease.checkpoint = {}
            private_history.private_page(lease, state)
        self.assertEqual(source.call_args.kwargs["oldest"], f"{self.now - 300}.000000")
        self.assertEqual(authority_lock.call_count, 4)
        self.assertTrue(finish.call_args.kwargs["complete"])
