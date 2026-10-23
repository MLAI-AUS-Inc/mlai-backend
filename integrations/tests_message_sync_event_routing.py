"""Event routing safety across real owner leases and content-free diagnostics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
import json
import threading
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import BridgeApiBudget, ExternalServiceConnection, SlackDmMirrorConversation, SlackDmMirrorDelivery, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync import discovery, event_routing, staged_routing
from integrations.services.message_sync.scheduler import LeaseLost


def pending(channel="GROOM", key="opaque-generation", *, at=None):
    return {"key": key, "channel_id": channel, "ciphertext": "sealed-only-copy",
            "kind": "mpim", "staged_at": (at or timezone.now()).isoformat()}


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True,
                   MESSAGE_SYNC_TARGETED_STAGED_ROUTING=False)
class EventRoutingDiagnosticsTests(SimpleTestCase):
    def test_pending_denominator_comes_from_ciphertext_not_optional_wake_map(self):
        cursor = {dm.PENDING_EVENT_CHECKPOINT_KEY: [
            pending(key="private-key-one", at=datetime.fromtimestamp(90, dt_timezone.utc)),
            pending(key="private-key-two", at=datetime.fromtimestamp(95, dt_timezone.utc)),
            pending("GOTHER", at=datetime.fromtimestamp(98, dt_timezone.utc)),
        ]}
        result = event_routing.summarize([cursor], now=100)
        self.assertEqual(result["pending_targets"], 2)
        self.assertEqual(result["pending_events"], 3)
        self.assertEqual(result["oldest_pending_age_seconds"], 10)
        encoded = json.dumps(result)
        for secret in ("GROOM", "GOTHER", "private-key", "sealed-only-copy"):
            self.assertNotIn(secret, encoded)

    def test_delivered_sample_is_idempotent_and_only_emits_content_free_after_commit(self):
        delivery = SimpleNamespace(status="completed", completed_at=datetime.fromtimestamp(120, dt_timezone.utc),
            metadata={event_routing.DELIVERY_KEY: {"parked_at": 100.0,
                "key": "private-key-must-not-be-logged", "path": "event"}}, save=Mock())
        callbacks = []
        with patch.object(event_routing.transaction, "on_commit", side_effect=lambda fn, **_: callbacks.append(fn)), patch.object(
            event_routing.telemetry, "record_latency",
        ) as metric:
            self.assertTrue(event_routing.record_delivery_completed_locked(delivery, adapter_confirmed=True))
            self.assertFalse(event_routing.record_delivery_completed_locked(delivery))
            metric.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            with self.assertLogs(event_routing.logger) as logs:
                callbacks[0]()
        self.assertEqual([call.args for call in metric.call_args_list], [
            ("staged_delivered", 20000), ("staged_delivered_event", 20000),
        ])
        self.assertNotIn("private-key", " ".join(logs.output))
        self.assertEqual(delivery.metadata[event_routing.DELIVERY_KEY]["latency_ms"], 20000)

    def test_completed_rows_without_matching_trusted_ack_cannot_mint_samples(self):
        for ack in (None, 119.0):
            with self.subTest(ack=ack):
                metadata = {"destination_message_id": "e" * 64, event_routing.DELIVERY_KEY: {
                    "parked_at": 100.0, "key": "server-key", "path": "event"}}
                if ack is not None:
                    metadata["event_adapter_confirmed_at"] = ack
                row = SimpleNamespace(status="completed", operation="create",
                    completed_at=datetime.fromtimestamp(120, dt_timezone.utc), metadata=metadata, save=Mock())
                with patch.object(event_routing.transaction, "on_commit") as commit:
                    self.assertFalse(event_routing.record_delivery_completed_locked(row))
                row.save.assert_not_called()
                commit.assert_not_called()

    def test_pending_failed_dead_and_undeliverable_rows_never_mint_delivery_samples(self):
        with patch.object(event_routing.transaction, "on_commit") as commit:
            for state in ("pending", "processing", "failed", "dead"):
                with self.subTest(state=state):
                    delivery = SimpleNamespace(status=state, completed_at=None, metadata={
                        event_routing.DELIVERY_KEY: {"parked_at": 100, "key": "opaque", "path": "event"},
                    }, save=Mock())
                    self.assertFalse(event_routing.record_delivery_completed_locked(delivery))
                    delivery.save.assert_not_called()
            for flag in ("dependency_superseded", "history_outside_window", "undeliverable",
                         "mutation_superseded", "history_recovery_superseded"):
                with self.subTest(noop_flag=flag):
                    delivery.status = "completed"
                    delivery.completed_at = datetime.fromtimestamp(120, dt_timezone.utc)
                    delivery.metadata = {flag: True, "event_adapter_confirmed_at": 120,
                        "destination_message_id": "d" * 64,
                        event_routing.DELIVERY_KEY: {"parked_at": 100, "key": "opaque", "path": "event"}}
                    self.assertFalse(event_routing.record_delivery_completed_locked(delivery))
        commit.assert_not_called()

    @override_settings(MESSAGE_SYNC_INBOX_MENTIONS=False)
    def test_actual_history_refresh_and_revival_preserve_server_timing_and_reject_forgery(self):
        conversation = SimpleNamespace(participant_hash="audience")
        original = {"parked_at": 100.0, "key": "original-server-key", "path": "event"}
        for state in ("pending", "failed", "dead"):
            with self.subTest(state=state):
                row = SimpleNamespace(status=state, metadata={"participant_hash": "audience", "event_adapter_confirmed_at": 110,
                    event_routing.DELIVERY_KEY: dict(original)}, save=Mock())
                incoming = {"participant_hash": "audience", "backfill": True,
                    "event_adapter_confirmed_at": 1,
                    event_routing.DELIVERY_KEY: {"parked_at": 1, "key": "forged", "path": "discovery"}}
                with patch.object(dm.SlackDmMirrorDelivery, "objects") as rows:
                    rows.get_or_create.return_value = row, False
                    result = dm._upsert_history_delivery(conversation,
                        source_message_id="1900000000.000001", author_id="UOTHER", operation="create",
                        text="synthetic history refresh", metadata=incoming,
                        held_until=datetime.fromtimestamp(130, dt_timezone.utc))
                self.assertIs(result, row)
                self.assertEqual(row.metadata[event_routing.DELIVERY_KEY], original)
                self.assertEqual(row.metadata["event_adapter_confirmed_at"], 110)
                self.assertTrue(row.metadata["backfill"])
                self.assertEqual(row.status, "pending")
                row.status, row.completed_at = "completed", datetime.fromtimestamp(140, dt_timezone.utc)
                callbacks = []
                with patch.object(event_routing.transaction, "on_commit", side_effect=lambda fn, **_: callbacks.append(fn)), patch.object(
                    event_routing.telemetry, "record_latency",
                ) as metric:
                    self.assertTrue(event_routing.record_delivery_completed_locked(row, adapter_confirmed=True))
                    for callback in callbacks:
                        callback()
                self.assertEqual(row.metadata[event_routing.DELIVERY_KEY]["latency_ms"], 40000)
                self.assertEqual(row.metadata["event_adapter_confirmed_at"], 140)
                self.assertIn(("staged_delivered_event", 40000), [call.args for call in metric.call_args_list])

    def test_delivery_summary_keeps_adapter_pending_terminal_noops_and_paths_separate(self):
        def marker(path="event", **extra):
            return {"path": path, "parked_at": 50, **extra}

        rows = [
            (marker(completed_at=99, latency_ms=14999), "completed"),
            (marker(completed_at=99, latency_ms=15000), "completed"),
            (marker(completed_at=99, latency_ms=15001), "completed"),
            (marker(completed_at=-300, latency_ms=1), "completed"),  # Old success cannot dilute this window.
            (marker(completed_at=101, latency_ms=1), "completed"),  # Future timestamp is not observed proof.
            (marker("targeted", completed_at=99, latency_ms=1), "completed"),
            (marker(), "pending"), (marker(), "processing"),
            (marker(), "failed"), (marker(), "dead"), (marker(), "completed"),
            ({"path": "unknown", "private": "must-not-leak"}, "pending"),
        ]
        result = event_routing.summarize_deliveries(rows, now=100, window_seconds=300)
        event = result["by_path"]["event"]
        self.assertEqual(event, {"confirmed": 3, "under_15s": 1, "at_least_15s": 2,
                                "pending": 2, "terminal": 2, "noop_completed": 1,
                                "oldest_pending_age_seconds": 50})
        self.assertEqual(result["by_path"]["targeted"]["confirmed"], 1)
        self.assertFalse(result["truncated"])
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_unknown_completion_timing_cannot_be_counted_as_a_fast_delivery(self):
        rows = [
            ({"path": "event", "parked_at": "unknown"}, "processing"),
            ({"path": "event", "completed_at": "unknown", "latency_ms": 1}, "completed"),
            ({"path": "event", "completed_at": 99, "latency_ms": float("nan")}, "completed"),
        ]
        event = event_routing.summarize_deliveries(rows, now=100)["by_path"]["event"]
        self.assertEqual(event["confirmed"], 0)
        self.assertEqual(event["under_15s"], 0)
        self.assertEqual((event["pending"], event["noop_completed"]), (1, 2))

    def test_delivery_summary_is_bounded_and_explicitly_reports_truncation(self):
        visited = []

        def rows():
            for i in range(20):
                visited.append(i)
                yield {"path": "event", "parked_at": 90}, "pending"

        result = event_routing.summarize_deliveries(rows(), now=100, limit=2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["by_path"]["event"]["pending"], 2)
        self.assertEqual(visited, [0, 1, 2])  # One lookahead establishes the incomplete denominator.

    def test_completed_only_sample_cannot_fabricate_zero_pending_or_terminal_counts(self):
        result = event_routing.summarize_deliveries([
            ({"path": "event", "completed_at": 99, "latency_ms": 10}, "completed"),
        ], now=100, completed_only=True)
        self.assertEqual(result["sample_scope"], "completed_confirmations_in_window")
        for path in result["by_path"].values():
            for field in ("pending", "terminal", "noop_completed", "oldest_pending_age_seconds"):
                self.assertNotIn(field, path)

    def test_cleanup_cannot_erase_a_new_generation_with_ciphertext_still_parked(self):
        cursor = {dm.PENDING_EVENT_CHECKPOINT_KEY: [pending(key="new-generation")],
            event_routing.KEY: {"targets": {"GROOM": {"generation": "new-generation", "due": 900,
                "first": 90, "attempts": 2}}, "counts": {"deferred": 1}},
            staged_routing.KEY: {"channels": {"GROOM": {"due": 900, "attempts": 2}}},
            discovery.KEY: {"token": "active-owner", "due": 900, "expires": 800, "served": 50}}
        original = json.loads(json.dumps(cursor))
        event_routing.prune_drained_targets(cursor)
        self.assertEqual(cursor[dm.PENDING_EVENT_CHECKPOINT_KEY], original[dm.PENDING_EVENT_CHECKPOINT_KEY])
        self.assertEqual(cursor[event_routing.KEY]["targets"], original[event_routing.KEY]["targets"])
        self.assertEqual(cursor[event_routing.KEY]["counts"], original[event_routing.KEY]["counts"])
        self.assertEqual(cursor[event_routing.KEY]["next_due"], 900)
        self.assertEqual(cursor[staged_routing.KEY], original[staged_routing.KEY])
        self.assertEqual(cursor[discovery.KEY], original[discovery.KEY])


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True,
                   MESSAGE_SYNC_TARGETED_STAGED_ROUTING=False)
class EventRoutingLeaseTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def normalized(self, source="1900000000.000001"):
        return {"source_message_id": source, "source_author_id": "UOTHER", "operation": "create",
                "slack_target_ts": source, "client_msg_id": "", "text": "private synthetic body",
                "metadata": {"participant_hash": self.conversation.participant_hash}}

    def stage_for_existing(self, normalized, born):
        with patch.object(dm.timezone, "now", return_value=born):
            self.assertEqual(dm._stage_unknown_slack_event("TIOAUTH", self.conversation.slack_conversation_id,
                normalized, authorized_user_ids={"UOWNER"}), 1)
        return self.cursor()[dm.PENDING_EVENT_CHECKPOINT_KEY][0]

    def stage(self, source="1900000000.000001"):
        return dm._stage_unknown_slack_event("TIOAUTH", "GROOM", {
            "source_message_id": source, "operation": "create", "text": "private synthetic body",
        }, authorized_user_ids={"UOWNER"}, kind_hint="mpim")

    def cursor(self):
        self.connection.refresh_from_db()
        return self.connection.sync_cursor

    def test_stage_and_target_roll_back_together_when_grant_save_fails(self):
        before = self.cursor()
        with patch.object(SlackDmMirrorGrant, "save", side_effect=RuntimeError("synthetic save failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic save failure"):
                self.stage()
        self.assertEqual(self.cursor(), before)
        self.assertNotIn(event_routing.KEY, self.cursor())
        self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())

    def test_status_separates_recent_confirmations_from_all_outstanding_delivery_work(self):
        now = timezone.now()
        samples = [
            ("completed", now.timestamp() - 1, 16000, 30),
            ("completed", now.timestamp() - 3600, 1, 3601),
            ("completed", None, None, 20),
            ("pending", None, None, 100),
            ("failed", None, None, 90),
            ("dead", None, None, 80),
        ]
        for index, (state, confirmed, latency, age) in enumerate(samples):
            marker = {"path": "event", "parked_at": now.timestamp() - age, "key": "private-key-not-exported"}
            if confirmed is not None:
                marker.update(completed_at=confirmed, latency_ms=latency)
            SlackDmMirrorDelivery.objects.create(conversation=self.conversation,
                source_platform="slack", source_message_id=f"status-fixture-{index}",
                operation="create", status=state, encrypted_text="", available_at=now,
                completed_at=now if state == "completed" else None,
                metadata={event_routing.DELIVERY_KEY: marker})
        output = StringIO()
        with patch("integrations.management.commands.message_sync_status.timezone.now", return_value=now):
            call_command("message_sync_status", window_minutes=1, stdout=output)
        payload = output.getvalue()
        result = json.loads(payload)["event_routing"]["delivery"]
        self.assertEqual(result["by_path"]["event"]["confirmed"], 1)
        self.assertEqual(result["by_path"]["event"]["at_least_15s"], 1)
        self.assertNotIn("pending", result["by_path"]["event"])
        self.assertEqual(result["outstanding_by_status"], [
            {"status": "dead", "count": 1}, {"status": "failed", "count": 1}, {"status": "pending", "count": 1},
        ])
        self.assertEqual(result["oldest_outstanding_age_seconds"], 100)
        self.assertEqual(result["noop_completed"], 1)
        self.assertNotIn("private-key-not-exported", payload)

    def test_drain_uses_original_server_timing_and_retry_cannot_replace_it(self):
        normalized = {"source_message_id": "1900000000.000003", "source_author_id": "UOTHER",
            "operation": "create", "slack_target_ts": "1900000000.000003", "client_msg_id": "",
            "text": "private synthetic body", "metadata": {event_routing.DELIVERY_KEY: {
                "parked_at": 0, "key": "forged-provider-key", "path": "event", "completed_at": 1,
            }}}
        born = timezone.now()

        def stage():
            return dm._stage_unknown_slack_event("TIOAUTH", self.conversation.slack_conversation_id,
                normalized, authorized_user_ids={"UOWNER"})

        with patch.object(dm.timezone, "now", return_value=born):
            self.assertEqual(stage(), 1)
        [parked] = self.cursor()[dm.PENDING_EVENT_CHECKPOINT_KEY]
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 1)
        row = SlackDmMirrorDelivery.objects.get(conversation=self.conversation, source_message_id=normalized["source_message_id"])
        original = row.metadata[event_routing.DELIVERY_KEY]
        self.assertEqual(original, {"parked_at": born.timestamp(), "key": parked["key"], "path": "event"})
        self.assertNotIn("forged-provider-key", json.dumps(row.metadata))
        row.status, row.last_error = "failed", "synthetic retry"
        row.save(update_fields=["status", "last_error"])
        with patch.object(dm.timezone, "now", return_value=born + timedelta(seconds=60)):
            self.assertEqual(stage(), 1)
        self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 1)
        row.refresh_from_db()
        self.assertEqual(row.metadata[event_routing.DELIVERY_KEY], original)
        self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())

    def test_staged_observation_attaches_to_pending_or_processing_history_delivery_without_resend(self):
        for index, state in enumerate(("pending", "processing")):
            with self.subTest(state=state):
                normalized = self.normalized(f"1900000000.{100 + index:06d}")
                born = timezone.now() - timedelta(seconds=20)
                parked = self.stage_for_existing(normalized, born)
                row = SlackDmMirrorDelivery.objects.create(conversation=self.conversation,
                    source_platform="slack", source_message_id=normalized["source_message_id"],
                    source_author_id="UOTHER", operation="create", status=state,
                    encrypted_text="already queued by history", available_at=timezone.now(),
                    metadata={"backfill": True, "participant_hash": self.conversation.participant_hash})
                authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
                with patch.object(dm.BuzzBridgeClient, "deliver_private") as send, patch.object(
                    event_routing.telemetry, "record_latency",
                ) as metric:
                    self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 0)
                send.assert_not_called()
                self.assertFalse(any(call.args[0].startswith("staged_delivered") for call in metric.call_args_list))
                row.refresh_from_db()
                self.assertEqual((row.status, row.encrypted_text), (state, "already queued by history"))
                self.assertEqual(row.metadata[event_routing.DELIVERY_KEY], {
                    "parked_at": born.timestamp(), "key": parked["key"], "path": "event"})
                self.assertEqual(SlackDmMirrorDelivery.objects.filter(conversation=self.conversation,
                    source_message_id=normalized["source_message_id"]).count(), 1)
                self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())

    def test_ack_before_staged_drain_measures_original_parked_latency_once_without_resend(self):
        normalized = self.normalized("1900000000.000103")
        born = timezone.now() - timedelta(seconds=30)
        parked = self.stage_for_existing(normalized, born)
        row = SlackDmMirrorDelivery.objects.create(conversation=self.conversation,
            source_platform="slack", source_message_id=normalized["source_message_id"],
            source_author_id="UOTHER", operation="create", status="processing",
            encrypted_text="already queued by history", available_at=timezone.now(),
            metadata={"participant_hash": self.conversation.participant_hash})
        ack = born + timedelta(seconds=20)
        with patch.object(dm, "_history_delivery_author_pubkey", return_value=self.owner_key), patch.object(
            dm, "_assert_private_delivery_authorized_locked",
        ), patch.object(dm.BuzzBridgeClient, "deliver_private", return_value={"message_id": "e" * 64}) as send, patch.object(
            event_routing.telemetry, "record_latency",
        ) as metric:
            with patch.object(dm.timezone, "now", return_value=ack), transaction.atomic():
                dm._deliver_to_mlai(row)
            send.assert_called_once()
            self.assertFalse(any(call.args[0].startswith("staged_delivered") for call in metric.call_args_list))
            row.refresh_from_db()
            self.assertEqual(row.metadata["event_adapter_confirmed_at"], ack.timestamp())
            authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
            self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 0)
            self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 0)
            send.assert_called_once()  # No second delivery to collect a timing observation.
        row.refresh_from_db()
        marker = row.metadata[event_routing.DELIVERY_KEY]
        self.assertEqual(marker["parked_at"], born.timestamp())
        self.assertEqual(marker["key"], parked["key"])
        self.assertEqual(marker["completed_at"], ack.timestamp())
        self.assertEqual(marker["latency_ms"], 20000)
        self.assertEqual(sum(call.args == ("staged_delivered_event", 20000) for call in metric.call_args_list), 1)
        self.assertEqual(SlackDmMirrorDelivery.objects.filter(conversation=self.conversation,
            source_message_id=normalized["source_message_id"]).count(), 1)
        self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())

    def test_legacy_completed_duplicate_without_trusted_ack_stays_unmeasured(self):
        normalized = self.normalized("1900000000.000104")
        born = timezone.now() - timedelta(seconds=30)
        self.stage_for_existing(normalized, born)
        row = SlackDmMirrorDelivery.objects.create(conversation=self.conversation,
            source_platform="slack", source_message_id=normalized["source_message_id"],
            source_author_id="UOTHER", operation="create", status="completed", encrypted_text="",
            available_at=timezone.now(), completed_at=born + timedelta(seconds=20),
            metadata={"participant_hash": self.conversation.participant_hash, "destination_message_id": "e" * 64})
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        with patch.object(dm.BuzzBridgeClient, "deliver_private") as send, patch.object(
            event_routing.telemetry, "record_latency",
        ) as metric:
            self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="event"), 0)
        send.assert_not_called()
        self.assertFalse(any(call.args[0].startswith("staged_delivered") for call in metric.call_args_list))
        row.refresh_from_db()
        self.assertEqual(row.metadata[event_routing.DELIVERY_KEY]["parked_at"], born.timestamp())
        self.assertNotIn("completed_at", row.metadata[event_routing.DELIVERY_KEY])
        self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())

    def test_new_history_delivery_rejects_incoming_timing_and_ack_metadata(self):
        with transaction.atomic():
            row = dm._upsert_history_delivery(self.conversation,
                source_message_id="1900000000.000105", author_id="UOTHER", operation="create",
                text="synthetic source history", held_until=timezone.now(), metadata={
                    "backfill": True, "participant_hash": self.conversation.participant_hash,
                    "event_adapter_confirmed_at": 1,
                    event_routing.DELIVERY_KEY: {"parked_at": 1, "key": "forged", "path": "event"},
                })
        row.refresh_from_db()
        self.assertNotIn(event_routing.DELIVERY_KEY, row.metadata)
        self.assertNotIn("event_adapter_confirmed_at", row.metadata)
        self.assertTrue(row.metadata["backfill"])

    def test_ordinary_drain_then_fresh_callback_resets_only_drained_source_schedule(self):
        born = timezone.now() - timedelta(seconds=30)
        normalized = self.normalized("1900000000.000106")
        self.stage_for_existing(normalized, born)
        source_id = self.conversation.slack_conversation_id
        cursor = self.cursor()
        cursor[event_routing.KEY]["targets"][source_id].update(due=born.timestamp() + 600, attempts=9)
        cursor[staged_routing.KEY] = {"channels": {source_id: {"due": born.timestamp() + 600, "attempts": 9}}}
        owner_lease = {"token": "unchanged-owner-lease", "expires": born.timestamp() + 600,
                       "due": born.timestamp() + 600, "served": 10}
        cursor[discovery.KEY] = dict(owner_lease)
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=["sync_cursor"])
        provider_deadline = timezone.now() + timedelta(seconds=600)
        budget = BridgeApiBudget.objects.create(app_id="SYNTHETIC", workspace_id="TIOAUTH",
            method="conversations.info", cooldown_until=provider_deadline, next_admitted_at=provider_deadline)
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.assertEqual(dm._drain_staged_events_for_conversation(authority, self.conversation.pk, path="discovery"), 1)
        self.assertNotIn(dm.PENDING_EVENT_CHECKPOINT_KEY, self.cursor())
        fresh = born + timedelta(seconds=60)
        self.stage_for_existing(self.normalized("1900000000.000107"), fresh)
        cursor = self.cursor()
        target = cursor[event_routing.KEY]["targets"][source_id]
        self.assertEqual(target["first"], fresh.timestamp())
        self.assertLessEqual(target["due"], fresh.timestamp())
        self.assertEqual(target["attempts"], 0)
        self.assertNotIn(source_id, (cursor.get(staged_routing.KEY) or {}).get("channels", {}))
        self.assertEqual(cursor[discovery.KEY], owner_lease)
        budget.refresh_from_db()
        self.assertEqual((budget.cooldown_until, budget.next_admitted_at), (provider_deadline, provider_deadline))

    def test_new_events_coalesce_one_target_and_preserve_its_original_deadline(self):
        self.assertEqual(self.stage(), 1)
        first = self.cursor()[dm.PENDING_EVENT_CHECKPOINT_KEY][0]
        self.connection.sync_cursor[event_routing.KEY]["targets"]["GROOM"]["due"] = timezone.now().timestamp() + 120
        self.connection.save(update_fields=["sync_cursor"])
        original = self.cursor()[event_routing.KEY]["targets"]["GROOM"].copy()
        self.assertEqual(self.stage(), 0)
        self.assertEqual(self.cursor()[event_routing.KEY]["targets"]["GROOM"], original)
        self.assertEqual(self.stage("1900000001.000002"), 1)
        cursor = self.cursor()
        target = cursor[event_routing.KEY]["targets"]["GROOM"]
        self.assertEqual(len(cursor[event_routing.KEY]["targets"]), 1)
        self.assertEqual(len(cursor[dm.PENDING_EVENT_CHECKPOINT_KEY]), 2)
        self.assertEqual(cursor[dm.PENDING_EVENT_CHECKPOINT_KEY][0], first)
        self.assertEqual((target["due"], target["first"]), (original["due"], original["first"]))
        self.assertNotEqual(target["generation"], original["generation"])
        self.assertNotIn("private synthetic body", json.dumps(cursor))
        self.assertNotIn("ciphertext", json.dumps(cursor[event_routing.KEY]))

    def test_recent_directory_sweep_does_not_delay_event_claim_and_old_flag_is_not_required(self):
        self.stage()
        self.grant.last_discovery_at = timezone.now()
        self.grant.save(update_fields=["last_discovery_at"])
        self.assertIsNone(discovery.claim_discovery(900))
        lease = event_routing.claim_event_routing(900)
        self.assertIsNotNone(lease)
        self.assertEqual((lease.grant_id, lease.event_channel_id), (self.grant.pk, "GROOM"))
        self.assertIsNone(discovery.claim_discovery(900))
        discovery.finish_discovery(lease)

    def test_restart_recovers_ciphertext_without_a_second_queue_or_wake_marker(self):
        self.stage()
        cursor = self.cursor()
        cursor.pop(event_routing.KEY)
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=["sync_cursor"])
        lease = event_routing.claim_event_routing(900)
        self.assertEqual(lease.event_generation, cursor[dm.PENDING_EVENT_CHECKPOINT_KEY][0]["key"])
        self.assertEqual(lease.event_channel_id, "GROOM")
        discovery.finish_discovery(lease)

    def test_owner_and_source_cooldowns_are_not_reset_by_a_new_event(self):
        self.stage()
        now = timezone.now().timestamp()
        cursor = self.cursor()
        cursor[discovery.KEY] = {"due": now + 120}
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=["sync_cursor"])
        self.stage("1900000001.000002")
        self.assertIsNone(event_routing.claim_event_routing(900))
        cursor = self.cursor()
        cursor[discovery.KEY]["due"] = 0
        cursor[staged_routing.KEY] = {"channels": {"GROOM": {"due": now + 120, "attempts": 2}}}
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=["sync_cursor"])
        self.assertIsNone(event_routing.claim_event_routing(900))

    def test_disabled_event_lane_leaves_ciphertext_for_existing_sweep(self):
        self.stage()
        original = self.cursor()
        for durable, enabled in ((False, True), (False, False)):
            with self.subTest(durable=durable, enabled=enabled), override_settings(
                MESSAGE_SYNC_ENABLED=durable, MESSAGE_SYNC_EVENT_ROUTING=enabled,
            ):
                self.assertIsNone(event_routing.claim_event_routing(900))
                self.assertEqual(self.cursor(), original)

    def test_changed_scope_consent_oauth_or_connection_fences_event_before_provider(self):
        class RollbackFixture(Exception):
            pass

        for change in ("scope", "consent", "oauth", "disconnect", "revoke"):
            with self.subTest(change=change):
                try:
                    with transaction.atomic():
                        self.stage()
                        lease = event_routing.claim_event_routing(900)
                        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
                        if change == "scope":
                            self.connection.scopes = []
                            self.connection.save(update_fields=["scopes"])
                        elif change == "consent":
                            self.grant.consented_at += timedelta(seconds=1)
                            self.grant.save(update_fields=["consented_at"])
                        elif change == "oauth":
                            self.connection.access_token = "synthetic-replacement"
                            self.connection.save(update_fields=["access_token"])
                        elif change == "disconnect":
                            self.connection.status = "disconnected"
                            self.connection.save(update_fields=["status"])
                        else:
                            self.grant.status = "revoked"
                            self.grant.save(update_fields=["status"])
                        with discovery.discovery_context(lease), patch.object(dm, "WebClient") as provider, patch.object(
                            staged_routing, "_route_channel",
                        ) as route:
                            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                                event_routing.route_event_claim(self.grant, authority, lease)
                            provider.assert_not_called()
                            route.assert_not_called()
                        raise RollbackFixture
                except RollbackFixture:
                    pass
                self.grant.refresh_from_db()
                self.connection.refresh_from_db()

    def test_device_revocation_keeps_full_first_event_provisioning_fence(self):
        self.stage()
        lease = event_routing.claim_event_routing(900)
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        CommunityChatDevice.objects.filter(user=self.user).update(status=DeviceBindingStatus.REVOKED, revoked_at=timezone.now())
        profiles = {"UOWNER": {"display_name": "Owner"}, "UOTHER": {"display_name": "Other"}}

        def source(_authority, method, **kwargs):
            if method == "conversations_info":
                return {"ok": True, "channel": {"id": "GROOM", "is_mpim": True, "is_private": True,
                    "is_member": True, "is_open": True, "latest": {"ts": "1900000000.000001"}}}
            if method == "conversations_members":
                return {"ok": True, "members": ["UOWNER", "UOTHER"], "response_metadata": {"next_cursor": ""}}
            self.fail(f"Unexpected provider method: {method}")

        with discovery.discovery_context(lease), patch.object(dm, "_call_slack_with_grant_authority", side_effect=source), patch.object(
            dm, "_stored_profile_cache", return_value=profiles,
        ), patch.object(dm, "_preload_slack_profiles"), patch.object(dm, "BuzzBridgeClient") as adapter:
            with self.assertRaises(dm.SlackDmMirrorError):
                event_routing.route_event_claim(self.grant, authority, lease)
        self.assertEqual(adapter.mock_calls, [])
        self.assertFalse(SlackDmMirrorConversation.objects.filter(
            grant=self.grant, slack_conversation_id="GROOM", mlai_channel_id__isnull=False,
        ).exists())
        self.assertEqual(len(self.cursor()[dm.PENDING_EVENT_CHECKPOINT_KEY]), 1)
        discovery.finish_discovery(lease)

    def test_hot_owner_does_not_starve_another_owner_or_workspace(self):
        self.stage()
        peers = []
        for index, workspace in ((701, "TIOAUTH"), (702, "TQUIET")):
            user = get_user_model().objects.create_user(email=f"event-owner-{index}@example.invalid")
            peer_connection = ExternalServiceConnection.objects.create(user=user, provider="slack", access_token="synthetic")
            peer = SlackDmMirrorGrant.objects.create(user=user, connection=peer_connection,
                slack_workspace_id=workspace, slack_user_id=f"U{index}", consented_at=timezone.now())
            peer_connection.sync_cursor = {dm.PENDING_EVENT_CHECKPOINT_KEY: [pending(f"G{index}")]}
            peer_connection.save(update_fields=["sync_cursor"])
            peers.append(peer)
        seen = []
        for _ in range(8):
            lease = event_routing.claim_event_routing(900)
            self.assertIsNotNone(lease)
            seen.append(lease.grant_id)
            discovery.finish_discovery(lease)
            # Keep every source hot, without resetting its fairness position.
            for row in ExternalServiceConnection.objects.all():
                cursor = row.sync_cursor or {}
                if discovery.KEY in cursor:
                    cursor[discovery.KEY]["due"] = 0
                    row.sync_cursor = cursor
                    row.save(update_fields=["sync_cursor"])
        self.assertEqual(seen[:4], [self.grant.pk, peers[1].pk, peers[0].pk, peers[1].pk])
        self.assertEqual(set(seen), {self.grant.pk, *(peer.pk for peer in peers)})

    @skipUnless(connection.vendor == "postgresql", "Real owner locking requires PostgreSQL")
    def test_event_and_sweep_compete_for_the_same_owner_lease(self):
        self.stage()
        ready = threading.Barrier(2)

        def claim(event):
            try:
                ready.wait(timeout=5)
                return event_routing.claim_event_routing(900) if event else discovery.claim_discovery(900)
            finally:
                connections.close_all()  # Healthy thread-local sessions must not survive teardown.

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, [True, False]))
        leases = [lease for lease in outcomes if lease is not None]
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].grant_id, self.grant.pk)
        discovery.finish_discovery(leases[0])

    def test_expired_replaced_event_worker_cannot_probe_route_or_finish(self):
        self.stage()
        old = event_routing.claim_event_routing(900)
        cursor = self.cursor()
        cursor[discovery.KEY]["expires"] = 0
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=["sync_cursor"])
        current = event_routing.claim_event_routing(900)
        self.assertNotEqual(old.token, current.token)
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        with discovery.discovery_context(old), patch.object(dm, "WebClient") as client, patch.object(
            staged_routing, "_route_channel",
        ) as route:
            with self.assertRaises(LeaseLost):
                event_routing.route_event_claim(self.grant, authority, old)
            client.assert_not_called()
            route.assert_not_called()
        with self.assertRaises(LeaseLost):
            discovery.finish_discovery(old)
        discovery.finish_discovery(current)

    def test_new_generation_arriving_during_old_route_survives_completion(self):
        self.stage()
        lease = event_routing.claim_event_routing(900)
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)

        def new_arrival(*_args, **_kwargs):
            cursor = self.cursor()
            cursor.pop(dm.PENDING_EVENT_CHECKPOINT_KEY)
            self.connection.sync_cursor = cursor
            self.connection.save(update_fields=["sync_cursor"])
            self.stage("1900000001.000002")
            return "routed"

        with discovery.discovery_context(lease), patch.object(staged_routing, "_route_channel", side_effect=new_arrival):
            event_routing.route_event_claim(self.grant, authority, lease)
        cursor = self.cursor()
        [retained] = cursor[dm.PENDING_EVENT_CHECKPOINT_KEY]
        self.assertNotEqual(retained["key"], lease.event_generation)
        self.assertEqual(cursor[event_routing.KEY]["targets"]["GROOM"]["generation"], retained["key"])
        discovery.finish_discovery(lease)
        future = timezone.now() + timedelta(seconds=2)
        with patch.object(discovery.timezone, "now", return_value=future):
            next_lease = event_routing.claim_event_routing(900)
        self.assertEqual(next_lease.event_generation, retained["key"])
        discovery.finish_discovery(next_lease)
