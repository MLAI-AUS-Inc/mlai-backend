"""Network-free worker regressions; these tests do not construct a database."""

import asyncio
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from integrations.services.community_bridge.worker import (
    CommunityBridgeDiscordClient,
    ParentMappingPending,
)

WORKER = "integrations.services.community_bridge.worker"


class MessageSyncWorkerUnitTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = CommunityBridgeDiscordClient()
        self.addAsyncCleanup(self.client.close)
        self.delivery = {
            "id": 42, "created_at": 1700000000, "delivery_type": "create",
            "source_platform": "slack", "source_channel_id": "C123",
            "source_message_id": "1700000000.000001", "payload": {},
        }

    async def test_checkpointed_retry_does_not_resolve_changed_profiles(self):
        envelope = {
            "delivery_id": "42", "created_at": 1700000000,
            "operation": "create", "channel_id": "room", "text": "original",
        }
        self.delivery["payload"]["_buzz_envelope_v1"] = envelope
        with (
            patch(f"{WORKER}.verified_identity_for_slack") as profile,
            patch(f"{WORKER}.BuzzBridgeClient.deliver", return_value={
                "message_id": "a" * 64, "channel_id": "room",
            }) as send,
            patch(f"{WORKER}.complete_create_delivery") as complete,
        ):
            await self.client._deliver_to_buzz(self.delivery)
        profile.assert_not_called()
        send.assert_called_once_with(**envelope)
        self.assertEqual(complete.call_args.kwargs["destination_message_id"], "a" * 64)

    async def test_uncertain_acceptance_retries_exactly_the_first_checkpoint(self):
        checkpoint = {}
        order = []

        def freeze(*, delivery_id, envelope):
            order.append("freeze")
            return checkpoint.setdefault(delivery_id, deepcopy(envelope))

        sent = []

        def send(**envelope):
            order.append("send")
            sent.append(deepcopy(envelope))
            if len(sent) == 1:
                raise TimeoutError("acknowledgement lost after acceptance")
            return {"message_id": "a" * 64, "channel_id": "room"}

        with (
            patch(f"{WORKER}.freeze_buzz_delivery", side_effect=freeze),
            patch(f"{WORKER}.BuzzBridgeClient.deliver", side_effect=send),
            patch(f"{WORKER}.complete_create_delivery") as complete,
        ):
            with self.assertRaises(TimeoutError):
                await self.client._freeze_and_send_buzz_delivery(
                    self.delivery, delivery_id="42", text="original", created_at=100,
                )
            complete.assert_not_called()
            await self.client._freeze_and_send_buzz_delivery(
                self.delivery, delivery_id="42", text="changed profile", created_at=200,
            )
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(order, ["freeze", "send", "freeze", "send"])

    async def test_delete_before_create_parks_instead_of_completing(self):
        self.delivery["delivery_type"] = "delete"
        with (
            patch(f"{WORKER}.verified_identity_for_slack", return_value=None),
            patch(f"{WORKER}.resolve_message_link", return_value=None),
            patch(f"{WORKER}.complete_delivery") as complete,
            patch(f"{WORKER}.BuzzBridgeClient.deliver") as send,
        ):
            with self.assertRaises(ParentMappingPending) as parked:
                await self.client._deliver_to_buzz(self.delivery)
        self.assertEqual(parked.exception.parent_message_id, self.delivery["source_message_id"])
        complete.assert_not_called()
        send.assert_not_called()

    async def test_disabled_sync_preserves_sequential_legacy_one_shot(self):
        order = []

        async def private(_limit):
            order.append("private_start")
            await asyncio.sleep(0)
            order.append("private_done")

        async def public(_limit):
            order.append("public")

        with (
            patch(f"{WORKER}.message_sync_enabled", return_value=False),
            patch.object(self.client, "process_private_deliveries_once", side_effect=private),
            patch.object(self.client, "process_public_deliveries_once", side_effect=public),
        ):
            await self.client.process_pending_deliveries_once()
        self.assertEqual(order, ["private_start", "private_done", "public"])

    async def test_slow_private_lane_cannot_hold_up_public_delivery(self):
        release_private = asyncio.Event()
        public_finished = asyncio.Event()

        async def private(_limit):
            await release_private.wait()

        async def public(_limit):
            public_finished.set()

        with (
            patch(f"{WORKER}.message_sync_enabled", return_value=True),
            patch.object(self.client, "process_private_deliveries_once", side_effect=private),
            patch.object(self.client, "process_public_deliveries_once", side_effect=public),
        ):
            task = asyncio.create_task(self.client.process_pending_deliveries_once())
            try:
                await asyncio.wait_for(public_finished.wait(), timeout=1)
                self.assertFalse(task.done())
            finally:
                release_private.set()
                await task


    async def test_history_uses_four_bounded_lanes_without_reseeding_each_page(self):
        import threading
        import time
        lock = threading.Lock()
        active = peak = 0
        def page(*, seed):
            nonlocal active, peak
            self.assertFalse(seed)
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return 1
        with (
            patch(f"{WORKER}.message_sync_enabled", return_value=True),
            patch(f"{WORKER}.seed_states") as seeder,
            patch(f"{WORKER}.process_history_once", side_effect=page) as run,
            patch(f"{WORKER}.heartbeat"),
        ):
            await self.client.process_sync_history_once()
            await self.client.process_sync_history_once()
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(run.call_count, 8)
        seeder.assert_called_once()
