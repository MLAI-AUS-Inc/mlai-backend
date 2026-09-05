import json
from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.buzz import BuzzBridgeError

PARENT_EVENT = "11" * 32
FLATTENED_EVENT = "22" * 32
REPAIRED_EVENT = "33" * 32
DELETE_EVENT = "44" * 32


@override_settings(
    BUZZ_BRIDGE_ADAPTER_URL="https://adapter.example/",
    BUZZ_BRIDGE_ADAPTER_TOKEN="adapter-token",
    BUZZ_BRIDGE_CALLBACK_SECRET="callback-secret",
)
class RepairCommunityBridgeThreadsTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T123",
            slack_channel_id="C123",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="9a1657ac-f7aa-5db0-b632-d8bbeb6dfb50",
            destination_channel_name="general",
            enabled=True,
            sync_deletes=True,
            sync_replies=True,
        )
        self.parent = self._link(
            source_message_id="1710000000.100000",
            destination_message_id=PARENT_EVENT,
        )
        self.reply = self._link(
            source_message_id="1710000001.100000",
            source_parent_message_id=self.parent.source_message_id,
            destination_message_id=FLATTENED_EVENT,
        )

    def _link(
        self,
        *,
        source_message_id,
        destination_message_id,
        source_parent_message_id="",
    ):
        return CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=source_message_id,
            source_parent_message_id=source_parent_message_id,
            source_author_id="U123",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=destination_message_id,
            source_payload={
                "source_author_id": "U123",
                "source_author_display_name": "Alice Nguyen",
                "source_author_avatar_url": "",
                "text": "A threaded reply",
                "attachments": [],
                "metadata": {"slack_message_id": source_message_id},
            },
        )

    @patch(
        "integrations.management.commands.repair_community_bridge_threads."
        "BuzzBridgeClient.deliver"
    )
    def test_dry_run_reports_safe_and_manual_cases_without_writes(self, mock_deliver):
        source_deleted = self._link(
            source_message_id="1710000002.100000",
            destination_message_id="55" * 32,
        )
        CommunityBridgeMessageLink.objects.filter(id=source_deleted.id).update(
            source_deleted_at=timezone.now()
        )
        destination_deleted = self._link(
            source_message_id="1710000003.100000",
            destination_message_id="66" * 32,
        )
        CommunityBridgeMessageLink.objects.filter(id=destination_deleted.id).update(
            destination_deleted_at=timezone.now()
        )
        stdout = StringIO()

        call_command("repair_community_bridge_threads", stdout=stdout)

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["would_repair_thread_parents"], 1)
        self.assertEqual(report["would_repair_deleted_destinations"], 1)
        self.assertEqual(report["manual_review_destination_only_deletes"], 1)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)
        mock_deliver.assert_not_called()

    def test_apply_requires_explicit_confirmation(self):
        with self.assertRaisesMessage(CommandError, "--confirm-republish is required"):
            call_command(
                "repair_community_bridge_threads",
                apply=True,
                stdout=StringIO(),
            )

    @patch(
        "integrations.management.commands.repair_community_bridge_threads."
        "BuzzBridgeClient.deliver"
    )
    def test_apply_republishes_reply_then_deletes_flattened_event(self, mock_deliver):
        mock_deliver.side_effect = [
            {
                "channel_id": self.channel.destination_channel_id,
                "message_id": REPAIRED_EVENT,
                "parent_message_id": PARENT_EVENT,
            },
            {
                "channel_id": self.channel.destination_channel_id,
                "message_id": DELETE_EVENT,
                "parent_message_id": "",
            },
        ]
        stdout = StringIO()

        call_command(
            "repair_community_bridge_threads",
            apply=True,
            confirm_republish=True,
            stdout=stdout,
        )

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["repaired_thread_parents"], 1)
        self.reply.refresh_from_db()
        self.assertEqual(self.reply.destination_message_id, REPAIRED_EVENT)
        self.assertEqual(self.reply.destination_parent_message_id, PARENT_EVENT)
        self.assertEqual(mock_deliver.call_count, 2)
        create_call, delete_call = mock_deliver.call_args_list
        self.assertEqual(create_call.kwargs["operation"], "create")
        self.assertEqual(create_call.kwargs["parent_message_id"], PARENT_EVENT)
        self.assertEqual(delete_call.kwargs["operation"], "delete")
        self.assertEqual(delete_call.kwargs["target_message_id"], FLATTENED_EVENT)

        receipt = CommunityBridgeReceipt.objects.get(
            receipt_key=f"slack-buzz-thread-repair-v1:parent:{self.reply.id}"
        )
        self.assertEqual(receipt.status, CommunityBridgeReceiptStatus.ACCEPTED)
        self.assertIsNotNone(receipt.processed_at)

        mock_deliver.reset_mock()
        call_command(
            "repair_community_bridge_threads",
            apply=True,
            confirm_republish=True,
            stdout=StringIO(),
        )
        mock_deliver.assert_not_called()

    @patch(
        "integrations.management.commands.repair_community_bridge_threads."
        "BuzzBridgeClient.deliver"
    )
    def test_failed_repair_is_retryable_with_stable_event_identity(self, mock_deliver):
        mock_deliver.side_effect = BuzzBridgeError("relay unavailable")
        call_command(
            "repair_community_bridge_threads",
            apply=True,
            confirm_republish=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        first_call = mock_deliver.call_args
        self.reply.refresh_from_db()
        self.assertEqual(self.reply.destination_message_id, FLATTENED_EVENT)
        receipt = CommunityBridgeReceipt.objects.get(
            receipt_key=f"slack-buzz-thread-repair-v1:parent:{self.reply.id}"
        )
        self.assertEqual(receipt.status, CommunityBridgeReceiptStatus.FAILED)

        mock_deliver.reset_mock()
        mock_deliver.side_effect = [
            {
                "channel_id": self.channel.destination_channel_id,
                "message_id": REPAIRED_EVENT,
                "parent_message_id": PARENT_EVENT,
            },
            {
                "channel_id": self.channel.destination_channel_id,
                "message_id": DELETE_EVENT,
                "parent_message_id": "",
            },
        ]
        call_command(
            "repair_community_bridge_threads",
            apply=True,
            confirm_republish=True,
            stdout=StringIO(),
        )
        self.assertEqual(
            mock_deliver.call_args_list[0].kwargs["delivery_id"],
            first_call.kwargs["delivery_id"],
        )
        self.assertEqual(
            mock_deliver.call_args_list[0].kwargs["created_at"],
            first_call.kwargs["created_at"],
        )

    @patch(
        "integrations.management.commands.repair_community_bridge_threads."
        "BuzzBridgeClient.deliver"
    )
    def test_repairs_source_deleted_destination_state(self, mock_deliver):
        CommunityBridgeMessageLink.objects.filter(id=self.parent.id).update(
            source_deleted_at=timezone.now()
        )
        CommunityBridgeMessageLink.objects.filter(id=self.reply.id).update(
            destination_parent_message_id=PARENT_EVENT
        )
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": DELETE_EVENT,
            "parent_message_id": "",
        }

        call_command(
            "repair_community_bridge_threads",
            apply=True,
            confirm_republish=True,
            stdout=StringIO(),
        )

        self.parent.refresh_from_db()
        self.assertIsNotNone(self.parent.destination_deleted_at)
        self.assertIn(
            call(
                delivery_id=f"slack-buzz-thread-repair-v1:{self.parent.id}:delete",
                created_at=mock_deliver.call_args.kwargs["created_at"],
                operation="delete",
                channel_id=self.channel.destination_channel_id,
                text="",
                target_message_id=PARENT_EVENT,
                source_workspace_id="T123",
                source_channel_id="C123",
                source_message_id=self.parent.source_message_id,
                source_author_id="U123",
                source_author_display_name="Alice Nguyen",
                source_author_avatar_url="",
                linked_pubkey="",
                linked_profile_id="",
            ),
            mock_deliver.call_args_list,
        )
