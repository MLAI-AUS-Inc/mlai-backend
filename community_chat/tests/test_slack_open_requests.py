"""Foreground opens never compete with source metadata calls in web requests."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from integrations.services import slack_open_requests as opens
from integrations.services import slack_owner_inventory_api as api
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.scheduler import BudgetDeferred


class SlackOpenRequestsTests(SimpleTestCase):
    def setUp(self):
        self.now = 1900000000.0
        self.connection = SimpleNamespace(sync_cursor={}, save=MagicMock())
        self.grant = SimpleNamespace(
            user=object(), connection=self.connection, save=MagicMock(),
            conversations=MagicMock(), owner_conversation_inventory=MagicMock(),
        )
        self.device = SimpleNamespace(pk=4, public_key="a" * 64, verified_at=None)
        self.authority = SimpleNamespace(grant_id=7)
        self.progress = SimpleNamespace(value={}, membership_fence="room-membership", save=MagicMock())
        self.row = SimpleNamespace(
            slack_conversation_id="DCINDY", kind="im", eligibility="eligible",
            source_archived=False,
        )
        self.grant.owner_conversation_inventory.filter.return_value.first.return_value = self.row
        for target, options in (
            ("integrations.services.slack_open_requests.time.time", {"return_value": self.now}),
            ("integrations.services.slack_owner_inventory.device_epoch", {"return_value": "epoch"}),
            ("integrations.services.slack_owner_inventory.state_for", {"return_value": {}}),
            ("integrations.services.slack_owner_inventory_api.transaction.atomic", {"side_effect": lambda: nullcontext()}),
            ("integrations.services.slack_owner_inventory_api._authorized", {"return_value": (self.grant, self.authority, self.device, {})}),
            ("integrations.services.slack_owner_inventory_api._lock_slack_grant_api_authority", {"return_value": (self.grant, self.connection)}),
            ("integrations.services.slack_owner_inventory_api.CommunityChatDevice.objects", {}),
            ("integrations.services.slack_owner_inventory_api.has_metadata_consent", {"return_value": True}),
            ("integrations.services.slack_owner_inventory_api._grant_history_days", {"return_value": 30}),
            ("integrations.services.slack_discovery_progress.conversation_progress", {"side_effect": lambda *args: nullcontext(self.progress)}),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def enqueue(self):
        return opens.enqueue_open_locked(
            self.grant, self.connection, self.authority, self.device, self.row,
        )

    def request(self):
        return api.request_open(self.grant.user, public_key=self.device.public_key,
                                slack_conversation_id=self.row.slack_conversation_id)

    def test_ready_open_never_calls_slack_even_when_budget_is_busy(self):
        self.enqueue()
        mirror = SimpleNamespace(
            participant_buzz_pubkeys=[self.device.public_key], mlai_channel_id=uuid4(),
        )
        with patch.object(api, "catalog_conversations") as catalog, patch.object(
            api, "ready_for_display", return_value=True,
        ), patch.object(api, "_call_slack_with_grant_authority", side_effect=BudgetDeferred(20)) as provider:
            catalog.return_value.first.return_value = mirror
            status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"state": "ready", "mlai_channel_id": str(mirror.mlai_channel_id)})
        provider.assert_not_called()
        self.assertEqual(self.connection.sync_cursor[opens.KEY], {})

    def test_source_only_request_is_immediate_and_has_no_provider_io(self):
        with patch.object(api, "catalog_conversations") as catalog, patch.object(
            api, "_call_slack_with_grant_authority", side_effect=AssertionError("foreground I/O")), patch.object(
            api, "_validate_open_source", side_effect=AssertionError("foreground validation")):
            catalog.return_value.first.return_value = None
            status, payload = self.request()
        self.assertEqual((status, payload["state"]), (202, "importing"))
        self.assertEqual(payload["retry_after_seconds"], 2)
        self.assertEqual(len(self.connection.sync_cursor[opens.KEY]), 1)

    def test_cached_room_for_other_device_is_not_returned(self):
        mirror = SimpleNamespace(participant_buzz_pubkeys=["b" * 64], mlai_channel_id=uuid4())
        with patch.object(api, "catalog_conversations") as catalog, patch.object(
            api, "ready_for_display", return_value=True,
        ) as readiness:
            catalog.return_value.first.return_value = mirror
            status, payload = self.request()
        self.assertEqual(status, 202)
        self.assertIsNone(payload["mlai_channel_id"])
        readiness.assert_not_called()

    def test_polling_coalesces_and_preserves_provider_backoff_and_lease(self):
        from integrations.services.message_sync.discovery import KEY
        self.connection.sync_cursor[KEY] = {"token": "lease", "expires": self.now + 80, "served": self.now}
        self.enqueue()
        request = next(iter(self.connection.sync_cursor[opens.KEY].values()))
        request["due"] = self.now + 45
        first_id = request["id"]
        self.connection.save.reset_mock()
        payload = self.enqueue()
        self.assertEqual(payload["retry_after_seconds"], 45)
        self.assertEqual(next(iter(self.connection.sync_cursor[opens.KEY].values()))["id"], first_id)
        self.connection.save.assert_not_called()
        self.assertEqual(self.connection.sync_cursor[KEY]["token"], "lease")
        self.assertEqual(self.connection.sync_cursor[KEY]["expires"], self.now + 80)

    def test_worker_error_is_surfaced_without_requeue(self):
        self.enqueue()
        request = next(iter(self.connection.sync_cursor[opens.KEY].values()))
        request.update(state="error", error="inventory_source_changed", status_code=409)
        with self.assertRaises(api.InventoryError) as caught:
            self.enqueue()
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("inventory_source_changed", 409))

    def test_queue_is_bounded_and_expired_intents_are_replaced(self):
        self.connection.sync_cursor[opens.KEY] = {
            str(i): {"until": self.now + 20} for i in range(opens.MAX_REQUESTS)
        }
        with self.assertRaises(api.InventoryError) as caught:
            self.enqueue()
        self.assertEqual(caught.exception.status_code, 429)
        for request in self.connection.sync_cursor[opens.KEY].values():
            request["until"] = self.now - 1
        self.enqueue()
        self.assertEqual(len(self.connection.sync_cursor[opens.KEY]), 1)

    def run_worker(self, *, source_error=None, import_error=None, changed_epoch=False):
        self.enqueue()
        if changed_epoch:
            next(iter(self.connection.sync_cursor[opens.KEY].values()))["epoch"] = "old"
        conversation = SimpleNamespace(pk=7)
        with patch.object(api, "_validate_open_source", return_value=({"id": "DCINDY"}, int(self.now)), side_effect=source_error) as source, patch.object(
            dm, "_discover_conversation", return_value=conversation, side_effect=import_error,
        ) as discover, patch.object(
            dm, "_drain_staged_events_for_conversation",
        ), patch.object(opens, "_prioritize_history") as prioritize, patch.object(opens, "_update") as update:
            self.assertTrue(opens.process_next_open(self.grant, self.authority))
        return source, discover, prioritize, update

    def test_worker_targets_requested_source_with_device_membership_fence(self):
        source, discover, prioritize, update = self.run_worker()
        source.assert_called_once_with(self.grant, self.authority, self.row)
        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["required_owner_public_key"], self.device.public_key)
        self.assertFalse(discover.call_args.kwargs["force_backfill"])
        self.assertFalse(discover.call_args.kwargs["reset_history"])
        prioritize.assert_called_once()
        self.assertEqual(update.call_args.kwargs, {"state": "importing"})

    def test_worker_defers_budget_without_provisioning(self):
        _, discover, prioritize, update = self.run_worker(
            source_error=api.InventoryError("inventory_rate_limited", 429, retry_after_seconds=17),
        )
        discover.assert_not_called()
        prioritize.assert_not_called()
        self.assertEqual(update.call_args.kwargs, {"due": self.now + 17})

    def test_partial_provisioning_obeys_shared_provider_retry_after(self):
        _, _, prioritize, update = self.run_worker(import_error=BudgetDeferred(41))
        prioritize.assert_not_called()
        self.assertEqual(update.call_args.kwargs, {"due": self.now + 41})

    def test_backoff_begins_after_slow_source_work(self):
        self.enqueue()
        with patch.object(opens.time, "time", side_effect=[self.now, self.now, self.now + 8]), patch.object(
            api, "_validate_open_source", side_effect=BudgetDeferred(60),
        ), patch.object(opens, "_update") as update:
            opens.process_next_open(self.grant, self.authority)
        self.assertEqual(update.call_args.kwargs, {"due": self.now + 68})

    def test_nested_provider_deferral_reuses_recent_validated_source(self):
        source, _, _, _ = self.run_worker(import_error=BudgetDeferred(2))
        source.assert_called_once()
        source, discover, _, update = self.run_worker()
        source.assert_not_called()
        discover.assert_called_once()
        self.assertEqual(update.call_args.kwargs, {"state": "importing"})

    def test_source_checkpoint_does_not_persist_provider_message_payloads(self):
        details = {
            "id": "DCINDY", "user": "UCINDY", "name": "Cindy", "is_im": True,
            "is_member": True, "is_open": True,
            "latest": {"text": "private message", "ts": str(self.now)},
            "topic": {"value": "private topic"}, "messages": ["private body"],
        }
        with patch.object(api, "_validate_open_source", return_value=(details, int(self.now))):
            actual, activity = opens._validated_source(self.grant, self.authority, self.row, self.progress)
        self.assertEqual(actual, {
            "id": "DCINDY", "user": "UCINDY", "name": "Cindy", "is_im": True,
            "is_member": True, "is_open": True,
        })
        self.assertEqual(activity, int(self.now))
        self.assertNotIn("private", str(self.progress.value))
        self.progress.save.assert_called_once()

    def test_source_checkpoint_expires_and_never_survives_membership_change(self):
        with patch.object(api, "_validate_open_source", return_value=({"id": "DCINDY"}, int(self.now))) as source:
            opens._validated_source(self.grant, self.authority, self.row, self.progress)
            with patch.object(opens.time, "time", return_value=self.now + opens.SOURCE_VALIDATION_TTL):
                opens._validated_source(self.grant, self.authority, self.row, self.progress)
            self.assertEqual(source.call_count, 2)
            self.progress.membership_fence = "changed-room-membership"
            with patch.object(opens.time, "time", return_value=self.now + opens.SOURCE_VALIDATION_TTL + 1):
                opens._validated_source(self.grant, self.authority, self.row, self.progress)
            self.assertEqual(source.call_count, 3)

    def test_worker_rejects_old_consent_or_device_epoch_before_provider_io(self):
        source, discover, _, update = self.run_worker(changed_epoch=True)
        source.assert_not_called()
        discover.assert_not_called()
        self.assertEqual(update.call_args.kwargs["error"], "slack_authority_changed")

    def test_transport_failures_back_off_and_eventually_report_retryable_error(self):
        with self.assertLogs(opens.logger, level="WARNING"):
            _, discover, _, update = self.run_worker(source_error=RuntimeError("private details"))
        discover.assert_not_called()
        self.assertEqual(update.call_args.kwargs, {"due": self.now + 30, "attempts": 1})
        request = {"id": "failed", "attempts": 2}
        with patch.object(opens, "_update") as update, self.assertLogs(opens.logger, level="WARNING") as logs:
            opens._retry_failure(self.authority, "source", request, "RuntimeError")
        self.assertEqual(update.call_args.kwargs["state"], "error")
        self.assertEqual(update.call_args.kwargs["status_code"], 502)
        self.assertNotIn("private details", "".join(logs.output))

    def test_pending_cooldown_cannot_be_bypassed_by_another_discovery_turn(self):
        self.enqueue()
        request = next(iter(self.connection.sync_cursor[opens.KEY].values()))
        request["due"] = self.now + 18
        # A second tap must not overwrite the first source's resumable metadata.
        self.connection.sync_cursor[opens.KEY]["second"] = {
            **request, "id": "second", "source_id": "DERICA",
            "requested_at": self.now + 1, "due": self.now,
        }
        with patch.object(api, "_validate_open_source") as source, self.assertRaises(BudgetDeferred) as deferred:
            opens.process_next_open(self.grant, self.authority)
        self.assertEqual(deferred.exception.retry_after, 18)
        source.assert_not_called()

    def test_open_queued_during_directory_scan_keeps_discovery_due(self):
        self.assertFalse(opens.has_pending_opens(self.connection.sync_cursor))
        self.enqueue()
        self.assertTrue(opens.has_pending_opens(self.connection.sync_cursor))
        request = next(iter(self.connection.sync_cursor[opens.KEY].values()))
        request["due"] = self.now + 30
        self.assertTrue(opens.has_pending_opens(self.connection.sync_cursor))
        request["state"] = "importing"
        self.assertFalse(opens.has_pending_opens(self.connection.sync_cursor))
        request.update(state="pending", until=self.now - 1)
        self.assertFalse(opens.has_pending_opens(self.connection.sync_cursor))

    def test_provisioned_request_leaves_remaining_history_to_import_worker(self):
        self.enqueue()
        next(iter(self.connection.sync_cursor[opens.KEY].values()))["state"] = "importing"
        with patch.object(api, "_validate_open_source") as source:
            self.assertFalse(opens.process_next_open(self.grant, self.authority))
        source.assert_not_called()
