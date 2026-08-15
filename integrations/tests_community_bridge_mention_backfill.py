import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)


@override_settings(SLACK_BRIDGE_BOT_TOKEN="xoxb-test")
class BackfillCommunityBridgeSlackMentionsTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T123",
            slack_channel_id="C123",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="channel-uuid",
            destination_channel_name="general",
            enabled=True,
            sync_edits=True,
        )

    def _mirrored_message(self, *, message_id="1710000000.1000", raw_text=None):
        receipt = CommunityBridgeReceipt.objects.create(
            channel=self.channel,
            platform=CommunityBridgePlatform.SLACK,
            receipt_key=f"event:{message_id}",
            event_type="message",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=message_id,
            status=CommunityBridgeReceiptStatus.ENQUEUED,
            payload=(
                {
                    "event": {
                        "type": "message",
                        "text": raw_text,
                    }
                }
                if raw_text is not None
                else {}
            ),
        )
        payload = {
            "delivery_type": CommunityBridgeDeliveryType.CREATE,
            "source_channel_id": self.channel.slack_channel_id,
            "source_message_id": message_id,
            "source_parent_message_id": "",
            "source_author_id": "U123",
            "source_author_display_name": "Alice",
            "source_author_avatar_url": "",
            "text": "Ask @user in #channel",
            "attachments": [],
            "metadata": {},
        }
        CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            receipt=receipt,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            source_event_key=receipt.receipt_key,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=message_id,
            target_channel_id=self.channel.destination_channel_id,
            payload=payload,
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        return CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=message_id,
            source_author_id="U123",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=f"buzz:{message_id}",
            source_payload=payload,
        )

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_mentions."
        "SlackBridgeClient.resolve_message_text",
        return_value="Ask @Alice Nguyen in #general",
    )
    def test_dry_run_finds_retained_slack_markup_without_writing(self, mock_resolve):
        self._mirrored_message(raw_text="Ask <@U999> in <#C999>")
        stdout = StringIO()

        call_command("backfill_community_bridge_slack_mentions", stdout=stdout)

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["would_enqueue"], 1)
        self.assertEqual(report["enqueued"], 0)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)
        mock_resolve.assert_called_once_with("Ask <@U999> in <#C999>")

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_mentions."
        "SlackBridgeClient.resolve_message_text",
        return_value="Ask @Alice Nguyen in #general",
    )
    def test_apply_enqueues_idempotent_resolved_edit(self, _mock_resolve):
        link = self._mirrored_message(raw_text="Ask <@U999> in <#C999>")

        first_stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_mentions",
            apply=True,
            confirm_historical_edits=True,
            stdout=first_stdout,
        )

        report = json.loads(first_stdout.getvalue())
        self.assertEqual(report["enqueued"], 1)
        delivery = CommunityBridgeDelivery.objects.get(
            source_event_key=f"slack-mention-backfill-v1:{link.id}"
        )
        self.assertEqual(delivery.delivery_type, CommunityBridgeDeliveryType.EDIT)
        self.assertEqual(delivery.payload["text"], "Ask @Alice Nguyen in #general")
        self.assertEqual(
            delivery.payload["metadata"]["backfill_version"],
            "slack-mention-backfill-v1",
        )

        second_stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_mentions",
            apply=True,
            confirm_historical_edits=True,
            stdout=second_stdout,
        )
        second_report = json.loads(second_stdout.getvalue())
        self.assertEqual(second_report["enqueued"], 0)
        self.assertEqual(second_report["already_enqueued"], 1)

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_mentions."
        "SlackBridgeClient.resolve_message_text"
    )
    def test_missing_retained_raw_payload_is_reported_and_skipped(self, mock_resolve):
        self._mirrored_message(raw_text=None)
        stdout = StringIO()

        call_command("backfill_community_bridge_slack_mentions", stdout=stdout)

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["no_retained_slack_markup"], 1)
        self.assertEqual(report["would_enqueue"], 0)
        mock_resolve.assert_not_called()

    def test_apply_requires_explicit_historical_edit_confirmation(self):
        with self.assertRaisesMessage(
            CommandError, "--confirm-historical-edits is required"
        ):
            call_command(
                "backfill_community_bridge_slack_mentions",
                apply=True,
                stdout=StringIO(),
            )
