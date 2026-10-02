"""Database-free outbox retry policy and worker error regressions."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services.community_bridge import store, worker
from integrations.services.message_sync.delivery import delivery_context
from integrations.services.message_sync.scheduler import LeaseLost


@override_settings(MESSAGE_SYNC_ENABLED=True)
class ParentDependencyBackoffTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.row = SimpleNamespace(
            id=1, pk=1, dependency_first_seen_at=None, dependency_attempts=0,
            attempts=1, lease_token=None, lease_expires_at=None, save=Mock(),
            source_platform="slack", source_channel_id="source", target_platform="buzz",
        )
        parent_lookup = patch.object(store, "resolve_mapped_message", return_value=None)
        self.parent_lookup = parent_lookup.start()
        self.addCleanup(parent_lookup.stop)

    def park(self):
        with patch.object(store.CommunityBridgeDelivery.objects, "select_for_update") as query:
            query.return_value.filter.return_value.first.return_value = self.row
            with patch.object(store.timezone, "now", return_value=self.now):
                # Exercise the transaction body with a mock row; no DB is opened.
                store.mark_delivery_waiting_for_parent.__wrapped__(
                    delivery_id=1, parent_message_id="parent",
                )

    def test_retry_schedule_survives_reclaims_without_spending_provider_attempts(self):
        first_seen = self.now
        for delay in (10, 30, 120, 300, 900, 900):
            self.row.attempts = 3  # Two actual provider failures plus this claim.
            self.park()
            self.assertEqual(self.row.status, "waiting_parent")
            self.assertEqual(self.row.available_at, self.now + timedelta(seconds=delay))
            self.assertEqual(self.row.attempts, 2)
            self.assertEqual(self.row.dependency_first_seen_at, first_seen)
            self.assertIsNone(self.row.lease_token)
            self.assertIsNone(self.row.lease_expires_at)
            self.now = self.row.available_at
        self.assertIn("available_at", self.row.save.call_args.kwargs["update_fields"])

    def test_long_archive_dependency_remains_durable_and_capped(self):
        self.row.dependency_first_seen_at = self.now - timedelta(days=30)
        self.row.dependency_attempts = 32_767
        self.park()
        self.assertEqual(self.row.status, "waiting_parent")
        self.assertEqual(self.row.dependency_attempts, 32_767)
        self.assertEqual(self.row.available_at, self.now + timedelta(minutes=15))

    @override_settings(MESSAGE_SYNC_ENABLED=False, COMMUNITY_BRIDGE_PARENT_DEPENDENCY_MAX_ATTEMPTS=2)
    def test_legacy_dependency_attempt_limit_still_dead_letters(self):
        self.row.dependency_attempts = 1
        self.park()
        self.assertEqual(self.row.status, "dead")
        self.assertEqual(self.row.last_error, "parent_mapping_timeout:parent")

    def test_expired_worker_cannot_park_a_newer_claim(self):
        self.row.lease_token = "current-owner"
        self.row.lease_expires_at = self.now + timedelta(minutes=1)
        with self.assertRaises(LeaseLost):
            self.park()
        self.parent_lookup.assert_not_called()
        self.row.save.assert_not_called()

    def test_parent_committed_before_parking_makes_child_immediately_retryable(self):
        self.parent_lookup.return_value = {"destination_message_id": "destination-parent"}
        self.row.attempts = 3
        self.row.dependency_attempts = 5
        self.row.dependency_first_seen_at = self.now - timedelta(hours=1)
        self.row.status = "processing"
        self.row.locked_at = self.now
        self.row.lease_token = "current-owner"
        self.row.lease_expires_at = self.now + timedelta(minutes=1)
        self.row.last_error = "parent_mapping_pending:parent"
        with delivery_context({"id": 1, "lease_token": "current-owner"}):
            self.park()
        self.parent_lookup.assert_called_once_with(
            source_platform="slack", source_channel_id="source",
            source_message_id="parent", destination_platform="buzz",
        )
        self.assertEqual(self.row.status, "pending")
        self.assertEqual(self.row.available_at, self.now)
        self.assertEqual(self.row.attempts, 2)
        self.assertEqual(self.row.dependency_attempts, 5)
        self.assertEqual(self.row.dependency_first_seen_at, self.now - timedelta(hours=1))
        self.assertIsNone(self.row.locked_at)
        self.assertIsNone(self.row.lease_token)
        self.assertIsNone(self.row.lease_expires_at)
        self.assertEqual(self.row.last_error, "")
        self.assertNotIn("dependency_attempts", self.row.save.call_args.kwargs["update_fields"])

    def test_parent_mapping_without_destination_still_parks(self):
        self.parent_lookup.return_value = {"destination_message_id": "  "}
        self.park()
        self.assertEqual(self.row.status, "waiting_parent")
        self.assertEqual(self.row.available_at, self.now + timedelta(seconds=10))

    def test_parent_completion_wakes_children_before_their_timed_retry(self):
        parent = SimpleNamespace(
            id=2, pk=2, channel="mapped-channel", source_platform="slack",
            source_channel_id="source", source_message_id="parent",
            source_parent_message_id="", target_platform="buzz", payload={},
            lease_token=None, save=Mock(),
        )
        with (
            patch.object(store.transaction, "atomic", return_value=nullcontext()),
            patch.object(store.CommunityBridgeDelivery.objects, "select_for_update") as query,
            patch.object(store.CommunityBridgeMessageLink.objects, "update_or_create"),
            patch.object(store.CommunityBridgeDelivery.objects, "filter") as children,
            patch.object(store.timezone, "now", return_value=self.now),
        ):
            query.return_value.select_related.return_value.get.return_value = parent
            candidates = children.return_value.filter.return_value
            candidates.select_for_update.return_value.order_by.return_value.values_list.return_value = [3]
            store.complete_create_delivery(
                delivery_id=2, destination_message_id="destination-parent",
                destination_channel_id="destination",
            )
        self.assertEqual(parent.status, "completed")
        self.assertEqual(children.call_args_list[0].kwargs["channel"], "mapped-channel")
        self.assertEqual(children.call_args_list[0].kwargs["status__in"], ["processing", "waiting_parent"])
        self.assertEqual(children.call_args.kwargs["pk__in"], [3])
        self.assertEqual(children.call_args.kwargs["status"], "waiting_parent")
        wake = children.return_value.update.call_args.kwargs
        self.assertEqual(wake["status"], "pending")
        self.assertEqual(wake["available_at"], self.now)

    def test_wake_waits_for_parking_children_without_changing_active_claims(self):
        parent = SimpleNamespace(
            channel="mapped-channel", source_platform="slack", source_channel_id="source",
            source_message_id="parent", target_platform="buzz",
        )
        parking = SimpleNamespace(status="processing", lease_token="parking-worker")
        active = SimpleNamespace(status="processing", lease_token="active-worker")
        rows = {3: parking, 4: active}
        candidates = Mock()
        wake = Mock()

        def locked_rows():
            # Model lock acquisition waiting until the child's park commits.
            parking.status = "waiting_parent"
            parking.lease_token = None
            yield 3
            yield 4

        def filtered_children(**kwargs):
            if "channel" in kwargs:
                self.assertEqual(kwargs["status__in"], ["processing", "waiting_parent"])
                return Mock(filter=Mock(return_value=candidates))
            self.assertEqual(parking.status, "waiting_parent")
            self.assertEqual(kwargs, {"pk__in": [3, 4], "status": "waiting_parent"})
            return wake

        def wake_parked(**kwargs):
            updated = 0
            for row in rows.values():
                if row.status == "waiting_parent":
                    row.status = kwargs["status"]
                    updated += 1
            return updated

        candidates.select_for_update.return_value.order_by.return_value.values_list.return_value = locked_rows()
        wake.update.side_effect = wake_parked
        with patch.object(store.CommunityBridgeDelivery.objects, "filter", side_effect=filtered_children):
            self.assertEqual(store._wake_waiting_child_deliveries(parent), 1)
        candidates.select_for_update.assert_called_once_with(of=("self",))
        self.assertEqual(parking.status, "pending")
        self.assertEqual(active.status, "processing")
        self.assertEqual(active.lease_token, "active-worker")
        self.assertNotIn("lease_token", wake.update.call_args.kwargs)


class UnsupportedDeliveryTests(IsolatedAsyncioTestCase):
    async def test_unsupported_reaction_is_permanent_before_provider_io(self):
        with self.assertRaises(worker.UnsupportedDeliveryError) as caught:
            await worker.CommunityBridgeDiscordClient._deliver_reaction_to_slack(
                None, {"delivery_type": "reaction_add"}, {"text": "not an emoji"},
            )
        self.assertTrue(caught.exception.permanent)

    async def test_unknown_adapter_is_permanent(self):
        with self.assertRaises(worker.UnsupportedDeliveryError):
            await worker.CommunityBridgeDiscordClient._process_delivery(
                None, {"target_platform": "unsupported"},
            )

    async def test_worker_dead_letters_unsupported_but_retries_transient_failures(self):
        for error, permanent in (
            (worker.UnsupportedDeliveryError("unsupported"), True),
            (TimeoutError("temporary transport failure"), False),
        ):
            instance = SimpleNamespace(_process_delivery=AsyncMock(side_effect=error))
            with (
                patch.object(worker, "message_sync_enabled", return_value=False),
                patch.object(worker, "mark_delivery_retry") as retry,
                patch.object(worker.logger, "exception"),
            ):
                await worker.CommunityBridgeDiscordClient._process_claimed_public_delivery(
                    instance, {"id": 1, "target_platform": "slack"},
                )
            self.assertEqual(retry.call_args.kwargs["permanent"], permanent)
