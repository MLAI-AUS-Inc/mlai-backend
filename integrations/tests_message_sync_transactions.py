"""Run only with approval for the disposable database migration baseline."""

from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery, CommunityBridgeReceipt
from integrations.services.community_bridge.store import (
    complete_create_delivery, freeze_buzz_delivery, ingest_inbound_event,
    mark_delivery_waiting_for_parent,
)


class MessageSyncTransactionTests(TransactionTestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T123", slack_channel_id="C123",
            destination_platform="buzz", destination_workspace_id="test.invalid",
            destination_channel_id="11111111-1111-4111-8111-111111111111",
        )

    def ingest(self, key="create", operation="create"):
        return ingest_inbound_event(
            source_platform="slack", receipt_key=key, source_channel_id="C123",
            event_type="message", raw_payload={}, normalized_event={
                "delivery_type": operation, "source_channel_id": "C123",
                "source_message_id": "1700000000.000001", "source_author_id": "U123",
                "source_author_display_name": "Synthetic fixture", "text": "fixture",
                "attachments": [],
            },
        )

    def test_receipt_and_outbox_commit_together_and_callback_retry_recovers(self):
        with patch.object(CommunityBridgeDelivery.objects, "create", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.ingest()
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.assertEqual(self.ingest()["status"], "enqueued")
        self.assertEqual(self.ingest()["status"], "duplicate")
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_first_frozen_envelope_survives_a_worker_restart(self):
        delivery_id = self.ingest()["delivery_id"]
        first = {"delivery_id": str(delivery_id), "text": "first", "created_at": 100}
        freeze_buzz_delivery(delivery_id=delivery_id, envelope=first)
        after_restart = freeze_buzz_delivery(
            delivery_id=delivery_id, envelope={**first, "text": "changed", "created_at": 200},
        )
        self.assertEqual(after_restart, first)
        self.assertEqual(CommunityBridgeDelivery.objects.get(id=delivery_id).payload["_buzz_envelope_v1"], first)

    def test_retry_repairs_an_accepted_orphan_from_the_previous_worker(self):
        CommunityBridgeReceipt.objects.create(
            channel=self.channel, platform="slack", receipt_key="create",
            event_type="message", source_channel_id="C123", status="accepted",
        )
        self.assertEqual(self.ingest()["status"], "enqueued")
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_committing_create_wakes_a_parked_edit(self):
        parent = self.ingest()["delivery_id"]
        edit = self.ingest("edit", "edit")["delivery_id"]
        mark_delivery_waiting_for_parent(delivery_id=edit, parent_message_id="1700000000.000001")
        self.assertEqual(CommunityBridgeDelivery.objects.get(id=edit).status, "waiting_parent")
        complete_create_delivery(
            delivery_id=parent, destination_message_id="a" * 64,
            destination_channel_id=self.channel.destination_channel_id,
        )
        self.assertEqual(CommunityBridgeDelivery.objects.get(id=edit).status, "pending")




class MessageSyncHealthTests(TestCase):
    def test_enabled_health_rejects_missing_or_stale_lanes(self):
        from io import StringIO
        from datetime import timedelta
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings
        from django.utils import timezone
        from integrations.models import BridgeWorkerHeartbeat
        with override_settings(MESSAGE_SYNC_ENABLED=True):
            with self.assertRaises(CommandError):
                call_command('message_sync_status', check=True, stdout=StringIO())
            for lane in ('inbox', 'history', 'public_delivery', 'private_delivery'):
                BridgeWorkerHeartbeat.objects.create(worker_id='synthetic-worker', lane=lane)
            call_command('message_sync_status', check=True, stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command('message_sync_status', check=True, local_worker=True, stdout=StringIO())
            BridgeWorkerHeartbeat.objects.filter(lane='inbox').update(heartbeat_at=timezone.now()-timedelta(minutes=4))
            with self.assertRaises(CommandError):
                call_command('message_sync_status', check=True, stdout=StringIO())
        with override_settings(MESSAGE_SYNC_ENABLED=False):
            call_command('message_sync_status', check=True, stdout=StringIO())
