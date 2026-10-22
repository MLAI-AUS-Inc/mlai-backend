"""Both worker entrypoints retain bounded polling with and without work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from django.test import override_settings

from integrations.services.community_bridge import worker


class DiscoveryWorkerCadenceTests(IsolatedAsyncioTestCase):
    async def test_event_slots_do_not_wait_for_a_slow_owner_and_still_reduce_authority_first(self):
        from integrations.services.message_sync import execution
        from integrations.services.message_sync.discovery import DiscoveryOutcome

        slow_started, other_entered, release, all_finished = (threading.Event() for _ in range(4))
        lock = threading.Lock()
        sequence, calls, finished = [], [0], [0]

        def revoke():
            with lock:
                sequence.append((threading.get_ident(), "revoke"))

        def discover():
            with lock:
                sequence.append((threading.get_ident(), "claim"))
                calls[0] += 1
                first = calls[0] == 1
            if first:
                slow_started.set()
            else:
                other_entered.set()
            try:
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release the synthetic owner turns")
            finally:
                with lock:
                    finished[0] += 1
                    if finished[0] == calls[0]:
                        all_finished.set()
            return DiscoveryOutcome(completed=1)

        with override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True), patch(
            "community_chat.account_bans.process_account_ban_revocations", side_effect=revoke,
        ), patch.object(worker, "discover_grants_if_due", side_effect=discover), patch.object(
            worker, "_record_discovery_heartbeat",
        ), patch.object(execution, "close_old_connections"):
            task = asyncio.create_task(worker._run_discovery_loop())
            try:
                self.assertTrue(await asyncio.to_thread(slow_started.wait, 2))
                self.assertTrue(await asyncio.to_thread(other_entered.wait, 2))
                await asyncio.sleep(0.05)  # Any excess worker slot would enter the blocked operation.
                self.assertEqual(calls[0], 2)
                self.assertFalse(all_finished.is_set())
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(all_finished.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        by_thread = {}
        for identity, step in sequence:
            by_thread.setdefault(identity, []).append(step)
        self.assertEqual(len(by_thread), 2)
        self.assertTrue(all(steps[:2] == ["revoke", "claim"] for steps in by_thread.values()))

    async def test_discord_event_mode_uses_the_same_bounded_runner(self):
        with override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True), patch.object(
            worker, "_run_discovery_loop", new=AsyncMock(),
        ) as run, patch.object(worker.asyncio, "to_thread", new=AsyncMock()) as legacy:
            await worker.CommunityBridgeDiscordClient.slack_dm_discovery_loop.coro(SimpleNamespace())
        run.assert_awaited_once()
        legacy.assert_not_awaited()

    async def test_headless_loop_always_sleeps_even_when_no_grant_is_due(self):
        for enabled, expected in ((True, 1.0), (False, 5.0)):
            with self.subTest(enabled=enabled), override_settings(MESSAGE_SYNC_ENABLED=enabled):
                events = []

                async def dispatch(fn):
                    fn()

                async def sleep(seconds):
                    events.append(seconds)
                    if len(events) == 9:
                        raise asyncio.CancelledError

                with (
                    patch.object(worker.asyncio, "to_thread", side_effect=dispatch),
                    patch.object(worker.asyncio, "sleep", side_effect=sleep),
                    patch("community_chat.account_bans.process_account_ban_revocations", side_effect=lambda: events.append("revoke")),
                    patch.object(worker, "discover_grants_if_due", side_effect=lambda: events.append("claim")),
                    patch.object(worker, "_record_discovery_heartbeat"),
                    self.assertRaises(asyncio.CancelledError),
                ):
                    await worker._run_discovery_loop()
                self.assertEqual(events, ["revoke", "claim", expected] * 3)

    async def test_discord_maintenance_retries_bans_before_discovery(self):
        events = []

        async def dispatch(fn):
            fn()

        with (
            patch.object(worker.asyncio, "to_thread", side_effect=dispatch),
            patch("community_chat.account_bans.process_account_ban_revocations", side_effect=lambda: events.append("revoke")),
            patch.object(worker, "discover_grants_if_due", side_effect=lambda: events.append("claim")),
                    patch.object(worker, "_record_discovery_heartbeat"),
        ):
            await worker.CommunityBridgeDiscordClient.slack_dm_discovery_loop.coro(SimpleNamespace())
        self.assertEqual(events, ["revoke", "claim"])

    async def test_discord_loop_uses_same_cadence_without_duplicate_start(self):
        for enabled, expected in ((True, 1.0), (False, 5.0)):
            with self.subTest(enabled=enabled), override_settings(MESSAGE_SYNC_ENABLED=enabled):
                client = SimpleNamespace(
                    _delivery_loop_started=False,
                    _slack_dm_maintenance_started=False,
                    _slack_user_directory_started=False,
                    _member_channel_maintenance_started=False,
                    delivery_loop=Mock(),
                    slack_dm_discovery_loop=Mock(),
                    slack_dm_history_loop=Mock(),
                    slack_dm_delivery_loop=Mock(),
                    sync_inbox_loop=Mock(),
                    slack_read_state_loop=Mock(),
                    slack_user_directory_loop=Mock(),
                    member_channel_maintenance_loop=Mock(),
                )
                with patch.object(worker.asyncio, "to_thread", new=AsyncMock()):
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                client.slack_dm_discovery_loop.change_interval.assert_called_once_with(seconds=expected)
                client.slack_dm_discovery_loop.start.assert_called_once()
                client.slack_user_directory_loop.start.assert_called_once()
                client.member_channel_maintenance_loop.start.assert_called_once()


class DiscoveryHealthTests(IsolatedAsyncioTestCase):
    async def test_blocked_heartbeat_preserves_sibling_counts_and_restores_a_failed_snapshot(self):
        from integrations.services.message_sync.discovery import DiscoveryOutcome

        for failed_write in (False, True):
            with self.subTest(failed_write=failed_write):
                entered, release = threading.Event(), threading.Event()
                clock = [10.0]

                def write(*_args, **_kwargs):
                    if beat.call_count == 1:
                        entered.set()
                        if not release.wait(timeout=5):
                            raise AssertionError("test did not release the heartbeat writer")
                        if failed_write:
                            raise RuntimeError("synthetic heartbeat write failure")

                with override_settings(MESSAGE_SYNC_ENABLED=True), patch.multiple(
                    worker, _discovery_heartbeat_at=0.0, _discovery_completed=0,
                    _discovery_failed=0, _discovery_error="", _discovery_heartbeat_inflight=False,
                    _discovery_heartbeat_lock=threading.Lock(),
                ), patch.object(worker, "time", SimpleNamespace(monotonic=lambda: clock[0])), patch.object(
                    worker, "heartbeat", side_effect=write,
                ) as beat, ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(worker._record_discovery_heartbeat,
                        DiscoveryOutcome(completed=1, failed=1, error_code="FirstError"))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        clock[0] = 20.0  # Even beyond the interval, no overlapping writer is permitted.
                        worker._record_discovery_heartbeat(
                            DiscoveryOutcome(completed=2, failed=3, error_code="SiblingError"))
                        self.assertEqual(beat.call_count, 1)
                        self.assertEqual((worker._discovery_completed, worker._discovery_failed), (2, 3))
                    finally:
                        release.set()
                    if failed_write:
                        with self.assertRaisesRegex(RuntimeError, "synthetic heartbeat write failure"):
                            first.result(timeout=2)
                        self.assertEqual((worker._discovery_completed, worker._discovery_failed), (3, 4))
                        self.assertEqual(worker._discovery_error, "SiblingError")
                    else:
                        first.result(timeout=2)
                        self.assertEqual((worker._discovery_completed, worker._discovery_failed), (2, 3))
                    self.assertFalse(worker._discovery_heartbeat_inflight)
                    worker._record_discovery_heartbeat(None)
                    expected = (3, 4) if failed_write else (2, 3)
                    self.assertEqual((beat.call_args.kwargs["completed"], beat.call_args.kwargs["failed"]), expected)
                    self.assertEqual(beat.call_args.kwargs["error_code"], "SiblingError")
                    self.assertEqual((worker._discovery_completed, worker._discovery_failed), (0, 0))

    async def test_hot_event_lane_reserves_every_fourth_turn_for_ordinary_discovery(self):
        from itertools import count
        from integrations.services import slack_dm_mirror as dm
        from integrations.services.message_sync import discovery, event_routing

        event = discovery.DiscoveryLease(7, 8, 9, "event-lease", event_channel_id="GROOM", event_generation="opaque")
        ordinary = discovery.DiscoveryLease(10, 11, 12, "ordinary-lease")
        grant = SimpleNamespace(connection=SimpleNamespace(sync_cursor={}))
        order = []
        with override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True,
                               MESSAGE_SYNC_TARGETED_STAGED_ROUTING=False), patch.object(
            discovery, "_turns", count(),
        ), patch.object(event_routing, "claim_event_routing", return_value=event) as fast, patch.object(
            discovery, "claim_discovery", return_value=ordinary,
        ) as normal, patch.object(discovery.SlackDmMirrorGrant, "objects") as grants, patch.object(
            dm, "ensure_owner_identity", return_value=(None, False, None),
        ), patch.object(dm, "_capture_slack_grant_api_authority"), patch.object(
            event_routing, "route_event_claim", side_effect=lambda *_, **__: order.append("event"),
        ), patch.object(dm, "discover_conversations", side_effect=lambda *_: order.append("ordinary")), patch.object(
            discovery, "finish_discovery",
        ):
            grants.select_related.return_value.get.return_value = grant
            for _ in range(8):
                self.assertTrue(discovery.discover_once(300))
        self.assertEqual(order, ["event", "event", "event", "ordinary"] * 2)
        self.assertEqual((fast.call_count, normal.call_count), (6, 2))
        self.assertTrue(all(not call.kwargs["prefer_opens"] for call in normal.call_args_list))

    async def test_heartbeat_coalesces_turns_and_budget_deferral_is_not_failure(self):
        from integrations.services.message_sync.discovery import DiscoveryOutcome
        with override_settings(MESSAGE_SYNC_ENABLED=True), patch.multiple(worker, _discovery_heartbeat_at=0.0, _discovery_completed=0, _discovery_failed=0, _discovery_error=""), patch.object(worker.time, "monotonic", side_effect=[10, 11, 12, 15]), patch.object(worker, "heartbeat") as beat:
            worker._record_discovery_heartbeat(DiscoveryOutcome(completed=1))
            worker._record_discovery_heartbeat(DiscoveryOutcome(error_code="BudgetDeferred"))
            worker._record_discovery_heartbeat(DiscoveryOutcome(failed=1, error_code="TimeoutError"))
            worker._record_discovery_heartbeat(None)
        self.assertEqual(beat.call_count, 2)
        self.assertEqual(beat.call_args.kwargs, {"completed": 0, "failed": 1, "error_code": "TimeoutError"})

    async def test_fourth_discovery_turn_reserves_normal_fairness(self):
        from itertools import count
        from integrations.services.message_sync import discovery
        with override_settings(MESSAGE_SYNC_OPEN_PRIORITY_ENABLED=True), patch.object(discovery, "_turns", count()), patch.object(discovery, "claim_discovery", return_value=None) as claim:
            for _ in range(8):
                self.assertIsNone(discovery.discover_once(300))
        self.assertEqual([call.kwargs["prefer_opens"] for call in claim.call_args_list], [True, True, True, False] * 2)
