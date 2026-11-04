"""Database-free outbox retry policy and worker error regressions."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from integrations.services.community_bridge import buzz, store, worker
from integrations.services.message_sync.delivery import delivery_context
from integrations.services.message_sync.scheduler import LeaseLost


@override_settings(MESSAGE_SYNC_ENABLED=True)
class ParentDependencyBackoffTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.row = SimpleNamespace(
            id=1, pk=1, dependency_first_seen_at=None, dependency_attempts=0,
            attempts=1, lease_token=None, lease_expires_at=None, save=Mock(),
            source_platform="slack", source_channel_id="source", target_platform="buzz", delivery_type="create",
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

    def test_flag_off_does_not_classify_and_first_park_logs_once(self):
        with patch('integrations.services.community_bridge.dependencies.classify_missing_dependency') as classify:
            with self.assertLogs(store.logger, 'INFO') as logs:
                self.park()
            self.assertIn('community_bridge_waiting_for_parent', logs.output[0])
            with self.assertNoLogs(store.logger, 'INFO'):
                self.park()
            classify.assert_not_called()

    @override_settings(COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED=True)
    def test_flagged_verdicts_preserve_lease_and_attempt_bookkeeping(self):
        from integrations.services.community_bridge.dependencies import DependencyVerdict
        self.row.delivery_type = 'create'
        for action, status in [('noop', 'completed'), ('dead_letter', 'dead'), ('park', 'waiting_parent'), ('wait', 'waiting_parent')]:
            with self.subTest(action=action), patch('integrations.services.community_bridge.dependencies.classify_missing_dependency', return_value=DependencyVerdict(action, 'dependency_dead', 'slack_ts')), patch.object(store, '_wake_waiting_child_deliveries') as wake:
                self.row.attempts = 3
                self.row.dependency_attempts = 0
                self.park()
                self.assertEqual(self.row.status, status)
                self.assertEqual(self.row.attempts, 2)
                self.assertEqual(self.row.dependency_attempts, 1)
                if action in {'noop', 'dead_letter'}:
                    wake.assert_called_once_with(self.row)
                if action == 'park':
                    self.assertEqual(self.row.available_at, self.now + timedelta(days=7))

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
            patch.object(store.transaction, "on_commit") as on_commit,
            patch.object(store.CommunityBridgeDelivery.objects, "select_for_update") as query,
            patch.object(store.CommunityBridgeMessageLink.objects, "update_or_create"),
            patch.object(store.CommunityBridgeMessageLink.objects, "select_for_update") as link_query,
            patch.object(store.CommunityBridgeDelivery.objects, "filter") as children,
            patch.object(store.timezone, "now", return_value=self.now),
        ):
            query.return_value.select_related.return_value.get.return_value = parent
            link_query.return_value.filter.return_value.first.return_value = None
            candidates = children.return_value.filter.return_value
            candidates.select_for_update.return_value.order_by.return_value.values_list.return_value = [3]
            store.complete_create_delivery(
                delivery_id=2, destination_message_id="destination-parent",
                destination_channel_id="destination",
            )
        on_commit.assert_called_once()
        callback = on_commit.call_args.args[0]
        self.assertEqual(callback.args, (2,))
        self.assertEqual(callback.func.__name__, "record_public_delivery")
        self.assertTrue(on_commit.call_args.kwargs["robust"])
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


@override_settings(BUZZ_BRIDGE_ADAPTER_URL="http://adapter.internal:8080/", BUZZ_BRIDGE_ADAPTER_TOKEN="token")
class AdapterErrorDetailTests(SimpleTestCase):
    def deliver(self):
        return buzz.BuzzBridgeClient.deliver(delivery_id="fixture", created_at=1, operation="reaction_add", channel_id="11111111-1111-4111-8111-111111111111", text="🧨")

    def response(self, status, body):
        return Mock(status_code=status, ok=status < 400, json=Mock(return_value=body))

    def test_deliver_preserves_prefix_and_records_permanent_reason(self):
        detail = "invalid_delivery/reaction_add_is_not_in_the_approved_bridge_set"
        with patch.object(buzz.requests, "post", return_value=self.response(400, {"error": "invalid_delivery", "reason": "reaction_add_is_not_in_the_approved_bridge_set"})):
            with self.assertRaises(buzz.BuzzBridgePermanentError) as caught:
                self.deliver()
        self.assertEqual(str(caught.exception), f"MLAI Chat adapter rejected delivery with HTTP 400 [{detail}]")
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(caught.exception.adapter_error, detail)

    def test_untrusted_or_invalid_body_keeps_exact_legacy_message(self):
        for body in [{"error": "channel is not mapped"}, {"error": "x <b>"}, [], None, {"error": "a" * 65}]:
            with self.subTest(body=body), patch.object(buzz.requests, "post", return_value=self.response(400, body)):
                with self.assertRaises(buzz.BuzzBridgePermanentError) as caught:
                    self.deliver()
                self.assertEqual(str(caught.exception), "MLAI Chat adapter rejected delivery with HTTP 400")
                self.assertEqual(caught.exception.adapter_error, "")
        response = self.response(400, {})
        response.json.side_effect = ValueError("invalid JSON")
        with patch.object(buzz.requests, "post", return_value=response):
            with self.assertRaises(buzz.BuzzBridgePermanentError) as caught:
                self.deliver()
        self.assertEqual(str(caught.exception), "MLAI Chat adapter rejected delivery with HTTP 400")
        self.assertEqual(caught.exception.adapter_error, "")

    def test_request_errors_keep_status_classification_and_batch_fallback(self):
        for status, code, error_type, message in [
            (422, "relay_rejected", buzz.BuzzBridgePermanentError, "MLAI Chat adapter rejected request with HTTP 422 [relay_rejected]"),
            (502, "upstream_unavailable", buzz.BuzzBridgeError, "MLAI Chat adapter returned HTTP 502 [upstream_unavailable]"),
            (404, "not_found", buzz.BuzzBridgePermanentError, "MLAI Chat adapter rejected request with HTTP 404 [not_found]"),
        ]:
            with self.subTest(status=status), patch.object(buzz.requests, "post", return_value=self.response(status, {"error": code})):
                with self.assertRaises(error_type) as caught:
                    buzz.BuzzBridgeClient._post_adapter("v1/fixture", {})
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(caught.exception.adapter_error, code)
                self.assertEqual(caught.exception.permanent, status != 502)

    def test_delete_error_preserves_suffix_and_prefix(self):
        with patch.object(buzz.requests, "delete", return_value=self.response(409, {"error": "conflict"})):
            with self.assertRaises(buzz.BuzzBridgePermanentError) as caught:
                buzz.BuzzBridgeClient._delete_adapter("v1/fixture")
        self.assertEqual(str(caught.exception), "MLAI Chat adapter rejected unregister request with HTTP 409 [conflict]")
        self.assertEqual(caught.exception.adapter_error, "conflict")

    def test_lookup_error_preserves_suffix_and_prefix(self):
        with patch.object(buzz.requests, "post", return_value=self.response(400, {"error": "invalid_delivery"})):
            with self.assertRaises(buzz.BuzzBridgePermanentError) as caught:
                buzz.BuzzBridgeClient.lookup_messages(channel_id="11111111-1111-4111-8111-111111111111", source_workspace_id="T1", source_channel_id="C1", source_message_ids=["1.000001"])
        self.assertEqual(str(caught.exception), "MLAI Chat adapter rejected lookup with HTTP 400 [invalid_delivery]")
        self.assertEqual(caught.exception.adapter_error, "invalid_delivery")

    def test_retryable_delivery_delete_and_lookup_errors_retain_codes(self):
        cases = [("post", self.deliver, "MLAI Chat adapter returned HTTP 503"),
                 ("delete", lambda: buzz.BuzzBridgeClient._delete_adapter("v1/fixture"), "MLAI Chat adapter unregister returned HTTP 503"),
                 ("post", lambda: buzz.BuzzBridgeClient.lookup_messages(channel_id="11111111-1111-4111-8111-111111111111", source_workspace_id="T1", source_channel_id="C1", source_message_ids=["1.000001"]), "MLAI Chat adapter lookup returned HTTP 503")]
        for method, call, prefix in cases:
            with self.subTest(prefix=prefix), patch.object(buzz.requests, method, return_value=self.response(503, {"error": "inbox_disabled"})):
                with self.assertRaises(buzz.BuzzBridgeError) as caught:
                    call()
                self.assertFalse(caught.exception.permanent)
                self.assertEqual(caught.exception.adapter_error, "inbox_disabled")
                self.assertEqual(str(caught.exception), prefix + " [inbox_disabled]")


class AdapterErrorWorkerTests(IsolatedAsyncioTestCase):
    async def test_worker_logs_content_free_error_and_keeps_permanent_policy(self):
        instance = SimpleNamespace(_process_delivery=AsyncMock(side_effect=buzz.BuzzBridgePermanentError("x", adapter_error="invalid_delivery")))
        with patch.object(worker, "message_sync_enabled", return_value=False), patch.object(worker, "mark_delivery_retry") as retry, patch.object(worker.logger, "exception") as log:
            await worker.CommunityBridgeDiscordClient._process_claimed_public_delivery(instance, {"id": 1, "target_platform": "buzz"})
        self.assertIn("invalid_delivery", log.call_args.args)
        self.assertTrue(retry.call_args.kwargs["permanent"])


class DependencyEvidenceTests(SimpleTestCase):
    def test_ignored_evidence_uses_subtype_timestamp_without_echoing_content(self):
        for subtype, fields, expected in (
            ('channel_join', {'ts': '123.4', 'user': 'U1'}, ('123.4', 'subtype:channel_join')),
            ('bot_message', {'ts': '123.5'}, ('123.5', 'userless_bot')),
            ('message_changed', {'ts': 'event-ts', 'message': {'ts': '123.6', 'user': 'UBRIDGE'}}, ('123.6', 'bridge_echo')),
            ('message_deleted', {'ts': 'event-ts', 'deleted_ts': '123.7', 'previous_message': {'user': 'UBRIDGE'}}, ('123.7', 'bridge_echo')),
        ):
            with self.subTest(subtype=subtype), override_settings(SLACK_BRIDGE_BOT_USER_ID='UBRIDGE'):
                self.assertEqual(store._slack_ignored_message_evidence({'event': {'type': 'message', 'channel_type': 'channel', 'subtype': subtype, **fields}}), expected)

    def test_noncovered_platform_always_waits_without_queries(self):
        from integrations.services.community_bridge.dependencies import classify_missing_dependency
        row = SimpleNamespace(source_platform='slack', target_platform='discord')
        now = timezone.now()
        verdict = classify_missing_dependency(row, '123.4', first_seen=now - timedelta(days=60), now=now)
        self.assertEqual((verdict.action, verdict.code), ('wait', 'platform_not_covered'))

    def test_command_verdict_does_not_spend_worker_attempts(self):
        from integrations.services.community_bridge.dependencies import DependencyVerdict, apply_dependency_verdict
        row = SimpleNamespace(pk=1, attempts=7, dependency_attempts=8, delivery_type='reaction_remove',
                              source_platform='slack', target_platform='buzz', save=Mock())
        now = timezone.now()
        with patch.object(store, '_wake_waiting_child_deliveries') as wake:
            apply_dependency_verdict(row, DependencyVerdict('noop', 'dependency_dead', 'reaction_object'),
                dependency_id='reaction:abc', first_seen=now, now=now, claimed=False)
        self.assertEqual((row.attempts, row.dependency_attempts), (7, 8))
        self.assertEqual(row.dependency_first_seen_at, now)
        self.assertIn('dependency_first_seen_at', row.save.call_args.kwargs['update_fields'])
        self.assertEqual(row.status, 'completed')
        self.assertEqual(row.last_error, 'dependency_noop:dependency_dead:reaction:abc')
        wake.assert_called_once_with(row)
