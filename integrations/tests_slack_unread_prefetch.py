"""Database-free unread prefetch quota, authority and source-selection fences."""
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from integrations.services import slack_dm_mirror as dm, slack_open_requests as opens
from integrations.services import slack_owner_inventory as inventory
from integrations.services import slack_owner_inventory_api as api
from integrations.services.message_sync.scheduler import BudgetDeferred


@override_settings(MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED=True, MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR=6)
class UnreadPrefetchTests(SimpleTestCase):
    def setUp(self):
        self.now = 1900000000.0
        self.connection = SimpleNamespace(sync_cursor={}, save=MagicMock())
        self.grant = SimpleNamespace(pk=1, connection=self.connection, history_days=30, conversations=MagicMock(), owner_conversation_inventory=MagicMock())
        self.authority = SimpleNamespace(grant_id=1)
        self.row = SimpleNamespace(slack_conversation_id="DUNREAD", kind="im", eligibility="eligible", source_is_open=True, source_archived=False, source_activity_ts=str(self.now - 60))
        self.target = SimpleNamespace(slack_id=self.row.slack_conversation_id)
        self.mirror = SimpleNamespace(pk=10)

    def run_prefetch(self, *, error=None, snapshot=None, cursor=None, details=None, history_days=30):
        if cursor is not None:
            self.connection.sync_cursor["slack_unread_prefetch_v1"] = cursor
        with ExitStack() as stack:
            stack.enter_context(patch.object(opens.time, "time", return_value=self.now))
            stack.enter_context(patch.object(inventory, "enabled", return_value=True))
            stack.enter_context(patch.object(inventory, "has_metadata_consent", return_value=True))
            stack.enter_context(patch.object(inventory, "source_read_targets", return_value=[self.target]))
            stack.enter_context(patch.object(dm, "private_channels_enabled", return_value=True))
            stack.enter_context(patch.object(dm, "_grant_history_days", return_value=history_days))
            stack.enter_context(patch.object(dm, "_lock_slack_grant_api_authority", return_value=(self.grant, self.connection)))
            stack.enter_context(patch.object(opens.transaction, "atomic", side_effect=lambda: nullcontext()))
            stack.enter_context(patch("integrations.services.slack_discovery_progress.conversation_progress", side_effect=lambda *_: nullcontext(SimpleNamespace())) )
            rows = self.grant.owner_conversation_inventory.filter.return_value
            rows.exclude.return_value = rows
            rows.filter.return_value = rows
            rows.order_by.return_value = rows
            rows.__getitem__.return_value = [self.row]
            stack.enter_context(patch("integrations.services.slack_chat_read_state._cache_key", return_value="synthetic-cache"))
            stack.enter_context(patch("django.core.cache.cache.get_many", return_value={"synthetic-cache": snapshot if snapshot is not None else {"available": True, "is_unread": True}}))
            validated = stack.enter_context(patch.object(opens, "_validated_source", return_value=(details or {"id": "DUNREAD"}, int(self.now - 60)), side_effect=error))
            discover = stack.enter_context(patch.object(dm, "_discover_conversation", return_value=self.mirror))
            drain = stack.enter_context(patch.object(dm, "_drain_staged_events_for_conversation"))
            result = opens.prefetch_unread_source(self.grant, self.authority, profile_cache={}, cycle_started_at=datetime.fromtimestamp(self.now, tz=timezone.utc))
        return result, validated, discover, drain

    def test_disabled_and_missing_metadata_consent_do_no_io(self):
        for flag, consent in ((False, True), (True, False)):
            with self.subTest(flag=flag, consent=consent), override_settings(MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED=flag), patch.object(inventory, "enabled", return_value=True), patch.object(inventory, "has_metadata_consent", return_value=consent):
                self.assertFalse(opens.prefetch_unread_source(self.grant, self.authority, profile_cache={}, cycle_started_at=None))
        self.grant.owner_conversation_inventory.filter.assert_not_called()

    def test_unread_prefetch_has_all_device_fence_and_no_foreground_marker(self):
        result, validated, discover, drain = self.run_prefetch()
        self.assertTrue(result)
        validated.assert_called_once()
        self.assertNotIn("required_owner_public_key", discover.call_args.kwargs)
        self.assertEqual(discover.call_args.kwargs["recent_activity"], True)
        self.assertFalse(discover.call_args.kwargs["force_backfill"])
        drain.assert_called_once_with(self.authority, self.mirror.pk)
        self.assertNotIn("foreground-refresh", str(self.connection.sync_cursor))
        self.assertEqual(len(self.connection.sync_cursor["slack_unread_prefetch_v1"]["hourly"]), 1)

    def test_unknown_or_read_snapshot_never_creates_room(self):
        for snapshot in ({}, {"available": False, "is_unread": True}, {"available": True, "is_unread": False}):
            result, validated, discover, _ = self.run_prefetch(snapshot=snapshot)
            self.assertFalse(result)
            validated.assert_not_called()
            discover.assert_not_called()

    def test_hourly_cap_and_source_cooldown_stop_before_slack(self):
        for cursor in ({"hourly": [{"id": str(i), "at": self.now - 1} for i in range(6)]}, {"sources": {self.row.slack_conversation_id: self.now + 100}}):
            result, validated, _, _ = self.run_prefetch(cursor=cursor)
            self.assertFalse(result)
            validated.assert_not_called()

    def test_inventory_errors_cool_down_without_consuming_creation_cap(self):
        for status, delay in ((403, 3600), (409, 3600), (502, 300)):
            result, _, discover, _ = self.run_prefetch(error=api.InventoryError("synthetic", status), cursor={})
            self.assertTrue(result)
            discover.assert_not_called()
            state = self.connection.sync_cursor["slack_unread_prefetch_v1"]
            self.assertEqual(state["sources"][self.row.slack_conversation_id], self.now + delay)
            self.assertEqual(state["hourly"], [])

    def test_budget_deferral_propagates_and_unused_reservation_is_returned(self):
        with self.assertRaises(BudgetDeferred):
            self.run_prefetch(error=BudgetDeferred(30))
        self.assertEqual(self.connection.sync_cursor["slack_unread_prefetch_v1"]["hourly"], [])

    def test_out_of_window_archived_and_closed_sources_are_skipped(self):
        for field, value in (("source_activity_ts", str(self.now - 31 * 86400)), ("source_archived", True), ("source_is_open", False), ("eligibility", "unsupported")):
            previous = getattr(self.row, field)
            setattr(self.row, field, value)
            result, validated, _, _ = self.run_prefetch(cursor={})
            self.assertFalse(result)
            validated.assert_not_called()
            setattr(self.row, field, previous)


    def test_wrapped_provider_admission_deferral_does_not_cool_source_for_an_hour(self):
        deferred = BudgetDeferred(20, before_request_method="conversations.info")
        error = api.InventoryError("inventory_rate_limited", 429, retry_after_seconds=20)
        error.__cause__ = deferred
        with self.assertRaises(BudgetDeferred) as caught:
            self.run_prefetch(error=error)
        self.assertIs(caught.exception, deferred)
        state = self.connection.sync_cursor["slack_unread_prefetch_v1"]
        self.assertEqual(state, {"hourly": [], "sources": {}})

    def test_mirrored_and_archived_sources_are_excluded_before_candidate_limit(self):
        self.run_prefetch()
        rows = self.grant.owner_conversation_inventory.filter.return_value
        self.assertIn({"slack_conversation_id__in": self.grant.conversations.values.return_value},
                      [call.kwargs for call in rows.exclude.call_args_list])
        self.assertIn({"source_archived": True}, [call.kwargs for call in rows.exclude.call_args_list])
        rows.__getitem__.assert_called_with(slice(None, 50, None))


    def test_fresh_archival_respects_bounded_and_all_history_consent(self):
        details = {"id": "DUNREAD", "is_archived": True}
        result, validated, discover, _ = self.run_prefetch(details=details)
        self.assertTrue(result)
        validated.assert_called_once()
        discover.assert_not_called()
        state = self.connection.sync_cursor["slack_unread_prefetch_v1"]
        self.assertEqual(state["hourly"], [])
        self.assertEqual(state["sources"][self.row.slack_conversation_id], self.now + 3600)
        result, _, discover, _ = self.run_prefetch(details=details, history_days=0, cursor={})
        self.assertTrue(result)
        discover.assert_called_once()
