from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.deletion_slack import SlackCopyErasureError, erase_slack_copy
from community_chat.models import AccountDeletionRequest, CommunityChatDevice
from integrations.models import CommunityBridgeChannel, CommunityBridgeMessageLink, CommunityBridgePlatform


@override_settings(MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="T_TEST", SLACK_BRIDGE_BOT_TOKEN="fixture")
class SlackCopyErasureTests(TestCase):
    def test_only_matching_bot_copy_is_deleted_and_payloads_are_cleared(self):
        member = get_user_model().objects.create_user(email="departing@example.test")
        case = AccountDeletionRequest.objects.create(
            user=member, scope=AccountDeletionRequest.Scope.CHAT, policy_version="test",
        )
        channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T_TEST", slack_channel_id="C_TEST", destination_channel_id="relay-room",
        )
        CommunityChatDevice.objects.create(user=member, public_key="a" * 64, verified_at=timezone.now())
        link = CommunityBridgeMessageLink.objects.create(
            channel=channel, source_platform=CommunityBridgePlatform.BUZZ,
            destination_platform=CommunityBridgePlatform.SLACK, source_channel_id="relay-room",
            source_author_id="a" * 64, source_message_id="relay-event",
            destination_channel_id="C_TEST", destination_message_id="500.000001",
            source_payload={"text": "content"}, destination_payload={"copy": "content"},
        )
        with patch("community_chat.deletion_slack.SlackBridgeClient.get_client") as get_client:
            client = get_client.return_value
            self.assertEqual(erase_slack_copy(request_id=case.pk, link_id=link.pk), "ready")
            get_client.assert_not_called()
            client.auth_test.return_value = {"team_id": "T_OTHER"}
            with self.assertRaises(SlackCopyErasureError):
                erase_slack_copy(request_id=case.pk, link_id=link.pk, apply=True)
            client.chat_delete.assert_not_called()
            client.auth_test.return_value = {"team_id": "T_TEST"}
            client.chat_delete.return_value = {"ok": True}
            self.assertEqual(erase_slack_copy(request_id=case.pk, link_id=link.pk, apply=True), "erased")
            self.assertEqual(erase_slack_copy(request_id=case.pk, link_id=link.pk, apply=True), "already_erased")
            client.chat_delete.assert_called_once_with(channel="C_TEST", ts="500.000001")
        link.refresh_from_db()
        self.assertIsNotNone(link.destination_deleted_at)
        self.assertIsNone(link.source_deleted_at)
        self.assertEqual(link.source_payload, {})
        self.assertEqual(link.destination_payload, {})
