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
        ):
            await worker.CommunityBridgeDiscordClient.slack_dm_discovery_loop.coro(SimpleNamespace())
        self.assertEqual(events, ["revoke", "claim"])

    async def test_discord_loop_uses_same_cadence_without_duplicate_start(self):
        for enabled, expected in ((True, 1.0), (False, 5.0)):
            with self.subTest(enabled=enabled), override_settings(MESSAGE_SYNC_ENABLED=enabled):
                client = SimpleNamespace(
                    _delivery_loop_started=False,
                    _slack_dm_maintenance_started=False,
                    delivery_loop=Mock(),
                    slack_dm_discovery_loop=Mock(),
                    slack_dm_history_loop=Mock(),
                    slack_dm_delivery_loop=Mock(),
                    sync_inbox_loop=Mock(),
                    slack_read_state_loop=Mock(),
                )
                with patch.object(worker.asyncio, "to_thread", new=AsyncMock()):
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                    await worker.CommunityBridgeDiscordClient.setup_hook(client)
                client.slack_dm_discovery_loop.change_interval.assert_called_once_with(seconds=expected)
                client.slack_dm_discovery_loop.start.assert_called_once()
