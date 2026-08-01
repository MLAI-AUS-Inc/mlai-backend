import asyncio
import hashlib
import hmac
import json
import time
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

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
from integrations.services.community_bridge.store import (
    ingest_inbound_event,
    ingest_slack_event,
    resolve_message_link,
)
from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgePermanentError,
)
from integrations.services.community_bridge.worker import CommunityBridgeDiscordClient


class _FakeSentMessage:
    def __init__(self, *, channel_id: str, message_id: str):
        self.channel = SimpleNamespace(id=int(channel_id))
        self.id = int(message_id)


class _FakePartialMessage:
    def __init__(self, *, message_id: str):
        self.message_id = int(message_id)
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self, *, channel_id: str, next_message_id: str = "90001"):
        self.id = int(channel_id)
        self.next_message_id = str(next_message_id)
        self.sent = []
        self.partial_messages = {}

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeSentMessage(channel_id=str(self.id), message_id=self.next_message_id)

    def get_partial_message(self, message_id: int):
        normalized = str(message_id)
        message = self.partial_messages.get(normalized)
        if message is None:
            message = _FakePartialMessage(message_id=normalized)
            self.partial_messages[normalized] = message
        return message


@override_settings(
    SLACK_BRIDGE_SIGNING_SECRET="bridge-secret",
    SLACK_BRIDGE_BOT_USER_ID="UBRIDGEBOT",
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
)
class SlackCommunityBridgeEventViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("community_bridge_slack_events")
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="C-SLACK-1",
            slack_channel_name="community",
            discord_channel_id="222",
            discord_channel_name="community",
            discord_guild_id="111",
        )

    def _sign(self, body: bytes, timestamp: str) -> str:
        base_string = f"v0:{timestamp}:{body.decode('utf-8')}"
        digest = hmac.new(b"bridge-secret", base_string.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"v0={digest}"

    def _post(self, payload: dict, *, timestamp: str = "", signature: str = ""):
        resolved_timestamp = timestamp or str(int(time.time()))
        body = json.dumps(payload).encode("utf-8")
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_SLACK_REQUEST_TIMESTAMP=resolved_timestamp,
            HTTP_X_SLACK_SIGNATURE=signature or self._sign(body, resolved_timestamp),
        )

    def test_url_verification_returns_challenge(self):
        response = self._post({"type": "url_verification", "challenge": "abc123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["challenge"], "abc123")

    def test_invalid_signature_is_rejected(self):
        response = self._post(
            {"type": "url_verification", "challenge": "abc123"},
            signature="v0=bad",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"], "invalid_signature")

    def test_message_event_enqueues_delivery_and_sanitizes_content(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvBridge1",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "U12345",
                    "ts": "1710000000.1000",
                    "text": "Hello <@U999> <http://example.com|Example>",
                    "files": [
                        {
                            "title": "guide.pdf",
                            "permalink": "https://files.example.com/guide.pdf",
                        }
                    ],
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "enqueued")

        receipt = CommunityBridgeReceipt.objects.get(receipt_key="EvBridge1")
        delivery = CommunityBridgeDelivery.objects.get(receipt=receipt)

        self.assertEqual(receipt.status, CommunityBridgeReceiptStatus.ENQUEUED)
        self.assertEqual(delivery.target_platform, CommunityBridgePlatform.DISCORD)
        self.assertEqual(delivery.delivery_type, CommunityBridgeDeliveryType.CREATE)
        self.assertEqual(delivery.payload["text"], "Hello @user Example (http://example.com)")
        self.assertEqual(
            delivery.payload["attachments"],
            [{"title": "guide.pdf", "url": "https://files.example.com/guide.pdf"}],
        )

    def test_duplicate_event_id_does_not_enqueue_twice(self):
        payload = {
            "type": "event_callback",
            "event_id": "EvBridgeDuplicate",
            "event": {
                "type": "message",
                "channel_type": "channel",
                "channel": self.channel.slack_channel_id,
                "user": "U12345",
                "ts": "1710000000.2000",
                "text": "Duplicate me",
            },
        }
        first = self._post(payload)
        second = self._post(payload)
        self.assertEqual(first.data["status"], "enqueued")
        self.assertEqual(second.data["status"], "duplicate")
        self.assertEqual(CommunityBridgeReceipt.objects.filter(receipt_key="EvBridgeDuplicate").count(), 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_bridge_bot_messages_are_ignored(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvBridgeIgnored",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "UBRIDGEBOT",
                    "ts": "1710000000.3000",
                    "text": "Loop prevention",
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ignored")
        receipt = CommunityBridgeReceipt.objects.get(receipt_key="EvBridgeIgnored")
        self.assertEqual(receipt.status, CommunityBridgeReceiptStatus.IGNORED)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_bridge_bot_delete_and_slack_connect_events_are_ignored(self):
        bot_delete = self._post(
            {
                "type": "event_callback",
                "event_id": "EvBridgeBotDelete",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "deleted_ts": "1710000000.4000",
                    "previous_message": {
                        "ts": "1710000000.4000",
                        "user": "UBRIDGEBOT",
                        "bot_id": "BBRIDGE",
                    },
                },
            }
        )
        shared_message = self._post(
            {
                "type": "event_callback",
                "event_id": "EvBridgeShared",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "U12345",
                    "ts": "1710000000.5000",
                    "text": "external message",
                    "is_ext_shared_channel": True,
                },
            }
        )

        self.assertEqual(bot_delete.data["status"], "ignored")
        self.assertEqual(shared_message.data["status"], "ignored")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)


class CommunityBridgeSetupCommandTests(TestCase):
    def test_command_creates_mapping_from_discord_url(self):
        out = StringIO()

        call_command(
            "upsert_community_bridge_channel",
            slack_channel_id="C-SLACK-PILOT",
            slack_channel_name="community-pilot",
            discord_url="https://discord.com/channels/1492063515987410957/1492063517191180340",
            discord_channel_name="welcome-and-rules",
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        channel = CommunityBridgeChannel.objects.get(slack_channel_id="C-SLACK-PILOT")

        self.assertEqual(payload["status"], "created")
        self.assertEqual(channel.discord_guild_id, "1492063515987410957")
        self.assertEqual(channel.discord_channel_id, "1492063517191180340")
        self.assertEqual(channel.discord_channel_name, "welcome-and-rules")
        self.assertTrue(channel.enabled)
        self.assertTrue(channel.sync_edits)
        self.assertTrue(channel.sync_deletes)
        self.assertTrue(channel.sync_replies)

    def test_command_creates_generic_buzz_mapping(self):
        out = StringIO()

        call_command(
            "upsert_community_bridge_channel",
            slack_channel_id="C-MLAI-CHAT",
            slack_channel_name="community",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="nostr-channel-event-id",
            destination_channel_name="community",
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        channel = CommunityBridgeChannel.objects.get(slack_channel_id="C-MLAI-CHAT")
        self.assertEqual(payload["destination_platform"], CommunityBridgePlatform.BUZZ)
        self.assertEqual(channel.destination_workspace_id, "chat.mlai.au")
        self.assertEqual(channel.destination_channel_id, "nostr-channel-event-id")
        self.assertEqual(channel.discord_channel_id, "")

    def test_command_updates_existing_mapping(self):
        CommunityBridgeChannel.objects.create(
            slack_channel_id="C-SLACK-PILOT",
            slack_channel_name="old-name",
            discord_guild_id="1492063515987410957",
            discord_channel_id="1492063517191180340",
            discord_channel_name="old-discord-name",
            enabled=False,
            sync_edits=False,
            sync_deletes=False,
            sync_replies=False,
        )
        out = StringIO()

        call_command(
            "upsert_community_bridge_channel",
            slack_channel_id="C-SLACK-PILOT",
            slack_channel_name="community-pilot",
            discord_guild_id="1492063515987410957",
            discord_channel_id="1492063517191180340",
            discord_channel_name="welcome-and-rules",
            no_sync_replies=True,
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        channel = CommunityBridgeChannel.objects.get(slack_channel_id="C-SLACK-PILOT")

        self.assertEqual(payload["status"], "updated")
        self.assertEqual(channel.slack_channel_name, "community-pilot")
        self.assertEqual(channel.discord_channel_name, "welcome-and-rules")
        self.assertTrue(channel.enabled)
        self.assertTrue(channel.sync_edits)
        self.assertTrue(channel.sync_deletes)
        self.assertFalse(channel.sync_replies)

    def test_command_rejects_conflicting_discord_channel_mapping(self):
        CommunityBridgeChannel.objects.create(
            slack_channel_id="C-SLACK-ONE",
            discord_guild_id="1492063515987410957",
            discord_channel_id="1492063517191180340",
        )

        with self.assertRaisesMessage(
            CommandError,
            "Discord channel 1492063517191180340 is already mapped to Slack channel C-SLACK-ONE.",
        ):
            call_command(
                "upsert_community_bridge_channel",
                slack_channel_id="C-SLACK-TWO",
                discord_url="https://discord.com/channels/1492063515987410957/1492063517191180340",
            )


class GenericCommunityBridgeRoutingTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="C-MLAI-CHAT",
            slack_channel_name="community",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="nostr-channel-event-id",
            destination_channel_name="community",
        )

    def test_slack_event_routes_to_configured_buzz_destination(self):
        result = ingest_slack_event(
            {
                "event_id": "EvToBuzz",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "U123",
                    "ts": "1710000000.0001",
                    "text": "Hello MLAI Chat",
                },
            }
        )

        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(result["target_platform"], CommunityBridgePlatform.BUZZ)
        self.assertEqual(delivery.target_channel_id, "nostr-channel-event-id")
        self.assertEqual(delivery.payload["text"], "Hello MLAI Chat")

    def test_buzz_event_routes_back_to_slack(self):
        result = ingest_inbound_event(
            source_platform=CommunityBridgePlatform.BUZZ,
            receipt_key="nostr-event-id",
            source_channel_id=self.channel.destination_channel_id,
            event_type="message_create",
            normalized_event={
                "delivery_type": CommunityBridgeDeliveryType.CREATE,
                "source_message_id": "nostr-event-id",
                "source_parent_message_id": "",
                "source_author_id": "a" * 64,
                "source_author_display_name": "MLAI member",
                "text": "Hello Slack",
                "attachments": [],
            },
            raw_payload={"event_id": "nostr-event-id"},
        )

        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(result["target_platform"], CommunityBridgePlatform.SLACK)
        self.assertEqual(delivery.target_channel_id, "C-MLAI-CHAT")

    def test_invalid_attachment_scheme_fails_closed(self):
        result = ingest_inbound_event(
            source_platform=CommunityBridgePlatform.BUZZ,
            receipt_key="nostr-invalid-attachment",
            source_channel_id=self.channel.destination_channel_id,
            event_type="message_create",
            normalized_event={
                "delivery_type": CommunityBridgeDeliveryType.CREATE,
                "source_message_id": "nostr-invalid-attachment",
                "text": "unsafe attachment",
                "attachments": [{"title": "secret", "url": "file:///tmp/secret"}],
            },
            raw_payload={},
        )

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)


@override_settings(BUZZ_BRIDGE_CALLBACK_SECRET="b" * 40, BUZZ_BRIDGE_CALLBACK_MAX_AGE_SECONDS=300)
class BuzzCommunityBridgeEventViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("community_bridge_buzz_events")
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="C-MLAI-CHAT",
            slack_channel_name="community",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            destination_channel_name="community",
        )

    def _post(self, payload: dict, *, timestamp: str = "", signature: str = ""):
        resolved_timestamp = timestamp or str(int(time.time()))
        body = json.dumps(payload).encode("utf-8")
        digest = hmac.new(
            b"b" * 40,
            resolved_timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_MLAI_BRIDGE_TIMESTAMP=resolved_timestamp,
            HTTP_X_MLAI_BRIDGE_SIGNATURE=signature or f"v1={digest}",
        )

    def _message_payload(self):
        return {
            "receipt_key": "message_create:" + "a" * 64,
            "source_channel_id": self.channel.destination_channel_id,
            "event_type": "message_create",
            "normalized_event": {
                "delivery_type": CommunityBridgeDeliveryType.CREATE,
                "source_message_id": "a" * 64,
                "source_parent_message_id": "",
                "source_author_id": "b" * 64,
                "source_author_display_name": "MLAI member",
                "text": "Hello from MLAI Chat",
                "attachments": [],
            },
            "raw_payload": {
                "event_id": "a" * 64,
                "kind": 9,
                "created_at": int(time.time()),
                "pubkey": "b" * 64,
                "tags": [["h", self.channel.destination_channel_id]],
            },
        }

    def test_signed_event_enqueues_slack_delivery(self):
        response = self._post(self._message_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "enqueued")
        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(delivery.source_platform, CommunityBridgePlatform.BUZZ)
        self.assertEqual(delivery.target_platform, CommunityBridgePlatform.SLACK)
        self.assertEqual(delivery.target_channel_id, self.channel.slack_channel_id)

    def test_callback_receipt_is_idempotent(self):
        first = self._post(self._message_payload())
        second = self._post(self._message_payload())

        self.assertEqual(first.data["status"], "enqueued")
        self.assertEqual(second.data["status"], "duplicate")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_invalid_or_stale_signature_is_rejected(self):
        invalid = self._post(self._message_payload(), signature="v1=bad")
        stale_timestamp = str(int(time.time()) - 301)
        stale = self._post(self._message_payload(), timestamp=stale_timestamp)

        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(stale.status_code, 403)
        self.assertEqual(CommunityBridgeReceipt.objects.count(), 0)


@override_settings(
    BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
    BUZZ_BRIDGE_ADAPTER_TOKEN="a" * 40,
    BUZZ_BRIDGE_CALLBACK_SECRET="b" * 40,
    BUZZ_BRIDGE_ADAPTER_TIMEOUT_SECONDS=12,
)
class BuzzBridgeClientTests(TestCase):
    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_delivery_uses_private_authenticated_contract(self, mock_post):
        mock_post.return_value = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "channel_id": "922c3b22-8002-4c3c-a37b-ce406a5e606e",
                "message_id": "a" * 64,
                "parent_message_id": "",
            },
        )

        result = BuzzBridgeClient.deliver(
            delivery_id="42",
            created_at=1785568000,
            operation="create",
            channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            text="Alice (Slack)\nHello",
        )

        self.assertEqual(result["message_id"], "a" * 64)
        call = mock_post.call_args
        self.assertEqual(call.args[0], "http://buzz-bridge-adapter:8090/v1/deliveries")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + "a" * 40)
        self.assertEqual(call.kwargs["json"]["delivery_id"], "42")
        self.assertEqual(call.kwargs["json"]["created_at"], 1785568000)
        self.assertEqual(call.kwargs["timeout"], 12)

    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_authentication_rejection_is_permanent(self, mock_post):
        mock_post.return_value = SimpleNamespace(ok=False, status_code=401)

        with self.assertRaises(BuzzBridgePermanentError):
            BuzzBridgeClient.deliver(
                delivery_id="42",
                created_at=1785568000,
                operation="create",
                channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
                text="hello",
            )


@override_settings(
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
    BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
    BUZZ_BRIDGE_ADAPTER_TOKEN="a" * 40,
    BUZZ_BRIDGE_CALLBACK_SECRET="b" * 40,
)
class BuzzCommunityBridgeWorkerTests(TransactionTestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="C-MLAI-CHAT",
            slack_channel_name="community",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            destination_channel_name="community",
        )
        self.client = CommunityBridgeDiscordClient()

    def tearDown(self):
        asyncio.run(self.client.close())

    def _delivery(self, *, delivery_type: str, message_id: str, parent_id: str = ""):
        return CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=delivery_type,
            source_event_key=f"event-{delivery_type}",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=message_id,
            source_parent_message_id=parent_id,
            target_channel_id=self.channel.destination_channel_id,
            payload={
                "source_author_id": "U123",
                "source_author_display_name": "Alice",
                "text": "Hello MLAI Chat",
                "attachments": [{"title": "guide", "url": "https://files.example/guide"}],
            },
            available_at=timezone.now(),
        )

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_create_preserves_reply_and_creates_message_link(self, mock_deliver):
        parent_event_id = "1" * 64
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.1000",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=parent_event_id,
        )
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            message_id="1710000000.2000",
            parent_id="1710000000.1000",
        )
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "2" * 64,
            "parent_message_id": parent_event_id,
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        kwargs = mock_deliver.call_args.kwargs
        self.assertEqual(kwargs["parent_message_id"], parent_event_id)
        self.assertIn("Alice (Slack)", kwargs["text"])
        self.assertIn("https://files.example/guide", kwargs["text"])
        self.assertGreater(kwargs["created_at"], 0)
        link = resolve_message_link(
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2000",
            destination_platform=CommunityBridgePlatform.BUZZ,
        )
        self.assertEqual(link["destination_message_id"], "2" * 64)

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_edit_targets_linked_buzz_event(self, mock_deliver):
        original_event_id = "3" * 64
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.3000",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=original_event_id,
        )
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            message_id="1710000000.3000",
        )
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "4" * 64,
            "parent_message_id": "",
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(mock_deliver.call_args.kwargs["target_message_id"], original_event_id)
        self.assertEqual(mock_deliver.call_args.kwargs["operation"], CommunityBridgeDeliveryType.EDIT)

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_delete_targets_link_and_marks_it_deleted(self, mock_deliver):
        original_event_id = "5" * 64
        link = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.4000",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=original_event_id,
        )
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.DELETE,
            message_id="1710000000.4000",
        )
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "6" * 64,
            "parent_message_id": "",
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(mock_deliver.call_args.kwargs["text"], "")
        self.assertEqual(mock_deliver.call_args.kwargs["target_message_id"], original_event_id)
        self.assertIsNotNone(link.destination_deleted_at)


class CommunityBridgePayloadRetentionTests(TestCase):
    def _receipt(self, *, key: str, age_days: int, payload: dict):
        receipt = CommunityBridgeReceipt.objects.create(
            platform=CommunityBridgePlatform.SLACK,
            receipt_key=key,
            payload=payload,
        )
        CommunityBridgeReceipt.objects.filter(id=receipt.id).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
        receipt.refresh_from_db()
        return receipt

    def test_purge_clears_only_expired_raw_payloads(self):
        expired = self._receipt(key="expired", age_days=31, payload={"event": {"text": "secret"}})
        current = self._receipt(key="current", age_days=29, payload={"event": {"text": "keep"}})

        out = StringIO()
        call_command("purge_community_bridge_payloads", days=30, batch_size=1, stdout=out)

        expired.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(expired.payload, {})
        self.assertEqual(current.payload, {"event": {"text": "keep"}})
        self.assertIn("Cleared 1 community bridge payload", out.getvalue())

    def test_dry_run_does_not_clear_payloads(self):
        expired = self._receipt(key="dry-run", age_days=31, payload={"token": "sensitive"})

        call_command("purge_community_bridge_payloads", days=30, dry_run=True, stdout=StringIO())

        expired.refresh_from_db()
        self.assertEqual(expired.payload, {"token": "sensitive"})


@override_settings(
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
    SLACK_BRIDGE_SIGNING_SECRET="bridge-secret",
)
class CommunityBridgeWorkerTests(TransactionTestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="C-SLACK-1",
            slack_channel_name="community",
            discord_channel_id="222",
            discord_channel_name="community",
            discord_guild_id="111",
        )
        self.client = CommunityBridgeDiscordClient()

    def tearDown(self):
        asyncio.run(self.client.close())

    def test_on_message_enqueues_slack_delivery(self):
        message = SimpleNamespace(
            id=901,
            channel=SimpleNamespace(id=int(self.channel.discord_channel_id)),
            guild=SimpleNamespace(id=111),
            author=SimpleNamespace(id=42, display_name="Dana", name="Dana", bot=False),
            edited_at=None,
            content="Hi <@123456>",
            attachments=[],
            reference=None,
            type=discord.MessageType.default,
        )

        asyncio.run(self.client.on_message(message))

        delivery = CommunityBridgeDelivery.objects.get()
        receipt = CommunityBridgeReceipt.objects.get()
        self.assertEqual(receipt.status, CommunityBridgeReceiptStatus.ENQUEUED)
        self.assertEqual(delivery.target_platform, CommunityBridgePlatform.SLACK)
        self.assertEqual(delivery.payload["text"], "Hi @user")
        self.assertEqual(delivery.payload["source_author_display_name"], "Dana")

    @patch("integrations.services.community_bridge.worker.SlackBridgeClient.get_user_display_name", return_value="Alice")
    def test_process_pending_deliveries_creates_discord_reply_and_link(self, _mock_user_name):
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.1000",
            destination_platform=CommunityBridgePlatform.DISCORD,
            destination_channel_id=self.channel.discord_channel_id,
            destination_message_id="555",
            destination_parent_message_id="",
            source_payload={"text": "parent"},
            destination_payload={},
        )
        delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.DISCORD,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            source_event_key="EvReply1",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2000",
            source_parent_message_id="1710000000.1000",
            target_channel_id=self.channel.discord_channel_id,
            payload={
                "source_author_id": "U123",
                "source_author_display_name": "",
                "text": "Hello from Slack",
                "attachments": [{"title": "notes.txt", "url": "https://files.example.com/notes.txt"}],
            },
            available_at=timezone.now(),
        )
        fake_channel = _FakeChannel(channel_id=self.channel.discord_channel_id, next_message_id="777")

        with patch.object(self.client, "_get_channel_or_fetch", AsyncMock(return_value=fake_channel)):
            asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(len(fake_channel.sent), 1)
        sent_payload = fake_channel.sent[0]
        self.assertEqual(sent_payload["reference"].message_id, 555)
        self.assertIn("Alice (Slack)", sent_payload["content"])
        self.assertIn("https://files.example.com/notes.txt", sent_payload["content"])

        link = resolve_message_link(
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2000",
            destination_platform=CommunityBridgePlatform.DISCORD,
        )
        self.assertIsNotNone(link)
        self.assertEqual(link["destination_message_id"], "777")

    @patch("integrations.services.community_bridge.worker.SlackBridgeClient.update_message")
    def test_process_pending_deliveries_updates_slack_message(self, mock_update_message):
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.DISCORD,
            source_channel_id=self.channel.discord_channel_id,
            source_message_id="333",
            destination_platform=CommunityBridgePlatform.SLACK,
            destination_channel_id=self.channel.slack_channel_id,
            destination_message_id="1710000000.4444",
            destination_parent_message_id="",
            source_payload={"text": "before"},
            destination_payload={},
        )
        delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.SLACK,
            source_platform=CommunityBridgePlatform.DISCORD,
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            source_event_key="discord-edit-1",
            source_channel_id=self.channel.discord_channel_id,
            source_message_id="333",
            target_channel_id=self.channel.slack_channel_id,
            payload={
                "source_author_id": "88",
                "source_author_display_name": "Bob",
                "text": "Updated from Discord",
                "attachments": [],
            },
            available_at=timezone.now(),
        )

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        mock_update_message.assert_called_once()
        kwargs = mock_update_message.call_args.kwargs
        self.assertEqual(kwargs["channel_id"], self.channel.slack_channel_id)
        self.assertEqual(kwargs["message_id"], "1710000000.4444")
        self.assertIn("Bob (Discord)", kwargs["text"])

    def test_process_pending_deliveries_deletes_discord_message_and_marks_link_deleted(self):
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.5555",
            destination_platform=CommunityBridgePlatform.DISCORD,
            destination_channel_id=self.channel.discord_channel_id,
            destination_message_id="666",
            destination_parent_message_id="",
            source_payload={"text": "before"},
            destination_payload={},
        )
        delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.DISCORD,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.DELETE,
            source_event_key="EvDelete1",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.5555",
            target_channel_id=self.channel.discord_channel_id,
            payload={
                "source_author_id": "U123",
                "source_author_display_name": "",
                "text": "",
                "attachments": [],
            },
            available_at=timezone.now(),
        )
        fake_channel = _FakeChannel(channel_id=self.channel.discord_channel_id)

        with patch.object(self.client, "_get_channel_or_fetch", AsyncMock(return_value=fake_channel)):
            asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertTrue(fake_channel.partial_messages["666"].deleted)

        link = CommunityBridgeMessageLink.objects.get(source_message_id="1710000000.5555")
        self.assertIsNotNone(link.source_deleted_at)
        self.assertIsNotNone(link.destination_deleted_at)
