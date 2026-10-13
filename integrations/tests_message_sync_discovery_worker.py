"""Both worker entrypoints retain bounded polling with and without work."""
import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from django.test import override_settings

from integrations.services.community_bridge import worker


class DiscoveryWorkerCadenceTests(IsolatedAsyncioTestCase):
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
                    delivery_loop=Mock(),
                    slack_dm_discovery_loop=Mock(),
                    slack_dm_history_loop=Mock(),
                    slack_dm_delivery_loop=Mock(),
                    sync_inbox_loop=Mock(),
                    slack_read_state_loop=Mock(),
                    slack_user_directory_loop=Mock(),
                )
                with patch.object(worker.asyncio, "to_thread", new=AsyncMock()):
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                client.slack_dm_discovery_loop.change_interval.assert_called_once_with(seconds=expected)
                client.slack_dm_discovery_loop.start.assert_called_once()
                client.slack_user_directory_loop.start.assert_called_once()


class DiscoveryHealthTests(IsolatedAsyncioTestCase):
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
