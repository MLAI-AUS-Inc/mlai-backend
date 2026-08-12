import json
from datetime import datetime, timedelta, timezone as datetime_timezone
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
)

CUTOVER = datetime(2026, 8, 10, 10, 26, 32, tzinfo=datetime_timezone.utc)
AVATAR_URL = "https://avatars.slack-edge.com/2026-08-10/alice_192.png"


@override_settings(SLACK_BRIDGE_BOT_TOKEN="xoxb-test")
class BackfillCommunityBridgeSlackAvatarsTests(TestCase):
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

    def _link(self, *, message_id, author_id="U123", text="original"):
        link = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=message_id,
            source_author_id=author_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=f"buzz-{message_id}",
            source_payload={
                "delivery_type": CommunityBridgeDeliveryType.CREATE,
                "source_channel_id": self.channel.slack_channel_id,
                "source_message_id": message_id,
                "source_author_id": author_id,
                "text": text,
                "attachments": [],
                "metadata": {},
            },
        )
        CommunityBridgeMessageLink.objects.filter(id=link.id).update(
            created_at=CUTOVER - timedelta(days=1)
        )
        link.refresh_from_db()
        return link

    def _completed_delivery(self, *, link, delivery_type, text, completed_at):
        delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=delivery_type,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            source_event_key=f"event:{delivery_type}:{link.source_message_id}:{completed_at.timestamp()}",
            source_channel_id=link.source_channel_id,
            source_message_id=link.source_message_id,
            source_parent_message_id=link.source_parent_message_id,
            target_channel_id=link.destination_channel_id,
            payload={
                "delivery_type": delivery_type,
                "source_channel_id": link.source_channel_id,
                "source_message_id": link.source_message_id,
                "source_parent_message_id": link.source_parent_message_id,
                "source_author_id": link.source_author_id,
                "source_author_display_name": "",
                "source_author_avatar_url": "",
                "text": text,
                "attachments": [],
                "metadata": {},
            },
            available_at=completed_at,
            completed_at=completed_at,
        )
        CommunityBridgeDelivery.objects.filter(id=delivery.id).update(
            created_at=completed_at
        )
        delivery.refresh_from_db()
        return delivery

    @staticmethod
    def _profile():
        return {"display_name": "Alice Nguyen", "avatar_url": AVATAR_URL}

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_avatars."
        "SlackBridgeClient.get_user_profile"
    )
    def test_dry_run_is_read_only_and_caches_profiles(self, mock_profile):
        mock_profile.return_value = self._profile()
        self._link(message_id="1710000000.1000")
        self._link(message_id="1710000000.2000")
        stdout = StringIO()

        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            stdout=stdout,
        )

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["scanned"], 2)
        self.assertEqual(report["would_enqueue"], 2)
        self.assertEqual(report["unique_authors"], 1)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        mock_profile.assert_called_once_with("U123")

    def test_apply_requires_explicit_historical_edit_confirmation(self):
        with self.assertRaisesMessage(
            CommandError, "--confirm-historical-edits is required"
        ):
            call_command(
                "backfill_community_bridge_slack_avatars",
                before=CUTOVER.isoformat(),
                apply=True,
                stdout=StringIO(),
            )

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_avatars."
        "SlackBridgeClient.get_user_profile"
    )
    def test_apply_enqueues_idempotent_edit_with_latest_delivered_content(
        self, mock_profile
    ):
        mock_profile.return_value = self._profile()
        link = self._link(message_id="1710000000.3000", text="stale original")
        self._completed_delivery(
            link=link,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            text="stale original",
            completed_at=CUTOVER - timedelta(hours=3),
        )
        self._completed_delivery(
            link=link,
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            text="current edited content",
            completed_at=CUTOVER - timedelta(hours=2),
        )

        first_stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            apply=True,
            confirm_historical_edits=True,
            stdout=first_stdout,
        )

        report = json.loads(first_stdout.getvalue())
        self.assertEqual(report["enqueued"], 1)
        backfills = CommunityBridgeDelivery.objects.filter(
            source_event_key=f"slack-avatar-backfill-v1:{link.id}"
        )
        self.assertEqual(backfills.count(), 1)
        delivery = backfills.get()
        self.assertEqual(delivery.delivery_type, CommunityBridgeDeliveryType.EDIT)
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(delivery.payload["text"], "current edited content")
        self.assertEqual(delivery.payload["source_author_display_name"], "Alice Nguyen")
        self.assertEqual(delivery.payload["source_author_avatar_url"], AVATAR_URL)
        self.assertEqual(delivery.receipt.payload, {})

        second_stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            apply=True,
            confirm_historical_edits=True,
            stdout=second_stdout,
        )
        second_report = json.loads(second_stdout.getvalue())
        self.assertEqual(second_report["enqueued"], 0)
        self.assertEqual(second_report["already_enqueued"], 1)
        self.assertEqual(backfills.count(), 1)

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_avatars."
        "SlackBridgeClient.get_user_profile"
    )
    def test_skips_messages_successfully_edited_after_cutover(self, mock_profile):
        link = self._link(message_id="1710000000.4000")
        self._completed_delivery(
            link=link,
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            text="already enriched",
            completed_at=CUTOVER + timedelta(minutes=1),
        )

        stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            stdout=stdout,
        )

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["scanned"], 0)
        mock_profile.assert_not_called()

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_avatars."
        "SlackBridgeClient.get_user_profile"
    )
    def test_skips_non_message_links_such_as_reactions(self, mock_profile):
        self._link(message_id="reaction:receipt-key")

        stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            stdout=stdout,
        )

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["scanned"], 0)
        mock_profile.assert_not_called()

    @patch(
        "integrations.management.commands.backfill_community_bridge_slack_avatars."
        "SlackBridgeClient.get_user_profile"
    )
    def test_skips_profiles_without_an_approved_avatar(self, mock_profile):
        mock_profile.return_value = {
            "display_name": "Alice Nguyen",
            "avatar_url": "",
        }
        self._link(message_id="1710000000.5000")

        stdout = StringIO()
        call_command(
            "backfill_community_bridge_slack_avatars",
            before=CUTOVER.isoformat(),
            apply=True,
            confirm_historical_edits=True,
            stdout=stdout,
        )

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["skipped_no_avatar"], 1)
        self.assertEqual(report["enqueued"], 0)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)

    def test_rejects_invalid_limits_and_cutoffs(self):
        with self.assertRaisesMessage(CommandError, "valid ISO-8601"):
            call_command(
                "backfill_community_bridge_slack_avatars",
                before="not-a-date",
                stdout=StringIO(),
            )
        with self.assertRaisesMessage(CommandError, "between 1 and 5000"):
            call_command(
                "backfill_community_bridge_slack_avatars",
                before=CUTOVER.isoformat(),
                limit=0,
                stdout=StringIO(),
            )
