import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
)


@override_settings(
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
    BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
    BUZZ_BRIDGE_ADAPTER_TOKEN="a" * 40,
    BUZZ_BRIDGE_CALLBACK_SECRET="b" * 40,
)
class CommunityBridgeThreadReconciliationTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI",
            slack_channel_id="CGENERAL",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            destination_channel_name="general",
        )
        self.root_id = "1786660929.427979"
        self.reply_id = "1786666478.295369"
        self.root_event_id = "a" * 64
        self.orphan_event_id = "b" * 64
        self.history = [
            {
                "ts": self.root_id,
                "user": "UROOT",
                "text": "Root",
                "reply_count": 1,
            }
        ]
        self.thread = [
            {"ts": self.root_id, "user": "UROOT", "text": "Root"},
            {
                "ts": self.reply_id,
                "thread_ts": self.root_id,
                "user": "UREPLY",
                "text": "Reply",
            },
        ]
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=self.root_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=self.root_event_id,
        )

    def _run(self, *, lookup_matches, apply=False):
        output = StringIO()
        with patch(
            "integrations.management.commands.reconcile_community_bridge_slack_threads."
            "SlackBridgeClient.get_channel_history",
            return_value=self.history,
        ), patch(
            "integrations.management.commands.reconcile_community_bridge_slack_threads."
            "SlackBridgeClient.get_thread_messages",
            return_value=self.thread,
        ), patch(
            "integrations.management.commands.reconcile_community_bridge_slack_threads."
            "BuzzBridgeClient.lookup_messages",
            return_value=lookup_matches,
        ), patch(
            "integrations.management.commands.reconcile_community_bridge_slack_threads."
            "Command._wait",
        ):
            call_command(
                "reconcile_community_bridge_slack_threads",
                slack_channel_id=[self.channel.slack_channel_id],
                max_roots=10,
                apply=apply,
                confirm_historical_repair=apply,
                wait_seconds=1 if apply else 120,
                stdout=output,
            )
        return json.loads(output.getvalue())

    def test_dry_run_detects_orphan_without_mutating_links(self):
        result = self._run(
            lookup_matches=[
                {
                    "source_message_id": self.root_id,
                    "destination_message_id": self.root_event_id,
                    "parent_message_id": "",
                    "broadcast": False,
                    "created_at": 1786660929,
                },
                {
                    "source_message_id": self.reply_id,
                    "destination_message_id": self.orphan_event_id,
                    "parent_message_id": "",
                    "broadcast": False,
                    "created_at": 1786666478,
                },
            ]
        )

        totals = result["totals"]
        self.assertEqual(totals["roots_scanned"], 1)
        self.assertEqual(totals["messages_scanned"], 2)
        self.assertEqual(totals["orphan_replies"], 1)
        self.assertEqual(totals["mismatches"], 1)
        self.assertEqual(CommunityBridgeMessageLink.objects.count(), 1)
        self.assertEqual(result["resume"]["latest"], "1786660929.427978")

    def test_dry_run_identifies_missing_database_link_for_valid_reply(self):
        result = self._run(
            lookup_matches=[
                {
                    "source_message_id": self.root_id,
                    "destination_message_id": self.root_event_id,
                    "parent_message_id": "",
                    "broadcast": False,
                    "created_at": 1786660929,
                },
                {
                    "source_message_id": self.reply_id,
                    "destination_message_id": "c" * 64,
                    "parent_message_id": self.root_event_id,
                    "broadcast": False,
                    "created_at": 1786666478,
                },
            ]
        )

        totals = result["totals"]
        self.assertEqual(totals["links_restored"], 1)
        self.assertEqual(totals["mismatches"], 1)
        self.assertEqual(CommunityBridgeMessageLink.objects.count(), 1)

    def test_dry_run_accepts_explicit_thread_broadcast(self):
        self.thread[1]["subtype"] = "thread_broadcast"
        result = self._run(
            lookup_matches=[
                {
                    "source_message_id": self.root_id,
                    "destination_message_id": self.root_event_id,
                    "parent_message_id": "",
                    "broadcast": False,
                    "created_at": 1786660929,
                },
                {
                    "source_message_id": self.reply_id,
                    "destination_message_id": "c" * 64,
                    "parent_message_id": self.root_event_id,
                    "broadcast": True,
                    "created_at": 1786666478,
                },
            ]
        )
        self.assertEqual(result["totals"]["wrong_broadcast"], 0)

    def test_dry_run_treats_reply_as_orphan_when_root_is_missing(self):
        CommunityBridgeMessageLink.objects.all().delete()

        result = self._run(
            lookup_matches=[
                {
                    "source_message_id": self.reply_id,
                    "destination_message_id": self.orphan_event_id,
                    "parent_message_id": "",
                    "broadcast": False,
                    "created_at": 1786666478,
                }
            ]
        )

        self.assertEqual(result["totals"]["missing_events"], 1)
        self.assertEqual(result["totals"]["orphan_replies"], 1)
        self.assertEqual(result["totals"]["mismatches"], 2)

    def test_apply_restores_valid_link_and_tombstones_duplicate(self):
        valid_reply_event_id = "c" * 64
        lookup_matches = [
            {
                "source_message_id": self.root_id,
                "destination_message_id": self.root_event_id,
                "parent_message_id": "",
                "broadcast": False,
                "created_at": 1786660929,
            },
            {
                "source_message_id": self.reply_id,
                "destination_message_id": valid_reply_event_id,
                "parent_message_id": self.root_event_id,
                "broadcast": False,
                "created_at": 1786666478,
            },
            {
                "source_message_id": self.reply_id,
                "destination_message_id": self.orphan_event_id,
                "parent_message_id": "",
                "broadcast": False,
                "created_at": 1786666478,
            },
        ]
        result = self._run(
            apply=True,
            lookup_matches=lookup_matches,
        )

        restored = CommunityBridgeMessageLink.objects.get(
            source_message_id=self.reply_id
        )
        self.assertEqual(restored.destination_message_id, valid_reply_event_id)
        self.assertEqual(
            restored.destination_parent_message_id, self.root_event_id
        )
        deletion = CommunityBridgeDelivery.objects.get(
            delivery_type=CommunityBridgeDeliveryType.DELETE,
            source_message_id=self.reply_id,
        )
        self.assertEqual(
            deletion.payload["metadata"]["destination_message_id_override"],
            self.orphan_event_id,
        )
        self.assertEqual(result["totals"]["duplicate_events"], 1)
        self.assertEqual(result["totals"]["links_restored"], 1)

        delivery_count = CommunityBridgeDelivery.objects.count()
        self._run(apply=True, lookup_matches=lookup_matches)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), delivery_count)

    def test_apply_requires_one_explicit_channel(self):
        with self.assertRaisesMessage(
            CommandError, "Apply mode requires exactly one --slack-channel-id"
        ):
            call_command(
                "reconcile_community_bridge_slack_threads",
                apply=True,
                confirm_historical_repair=True,
                wait_seconds=1,
            )
