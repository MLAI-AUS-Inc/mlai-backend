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


@override_settings(SLACK_BRIDGE_BOT_TOKEN="synthetic-bot", BUZZ_BRIDGE_ADAPTER_URL="https://adapter.invalid",
                   BUZZ_BRIDGE_ADAPTER_TOKEN="synthetic-token", BUZZ_BRIDGE_CALLBACK_SECRET="synthetic-secret")
class RepairCommunityBridgeThreadsTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(audience="community", slack_workspace_id="TMLAI",
            slack_channel_id="CGENERAL", destination_platform="buzz", destination_channel_id="a" * 32)
        self.link = CommunityBridgeMessageLink.objects.create(channel=self.channel, source_platform="slack",
            source_channel_id="CGENERAL", source_message_id="1786660929.427979", destination_platform="buzz",
            destination_channel_id=self.channel.destination_channel_id, destination_message_id="b" * 64,
            source_parent_message_id="1786660000.000001", source_payload={"text": "retained"})

    def test_retired_repair_rejects_every_mode_without_provider_or_data_mutation(self):
        from django.core.management.base import CommandError
        from integrations.models import CommunityBridgeDelivery, CommunityBridgeReceipt
        before = list(CommunityBridgeMessageLink.objects.values())
        with patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client") as slack, \
             patch("integrations.services.community_bridge.buzz.BuzzBridgeClient.deliver") as relay:
            for options in [{"apply": apply, "confirm_republish": apply} for apply in (False, True)]:
                with self.subTest(options=options), self.assertRaisesMessage(CommandError, "durable member-authorized"):
                    call_command("repair_community_bridge_threads", stdout=StringIO(), **options)
            slack.assert_not_called()
            relay.assert_not_called()
        self.assertEqual(list(CommunityBridgeMessageLink.objects.values()), before)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)
