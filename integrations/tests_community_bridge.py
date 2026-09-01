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
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.store import (
    ingest_inbound_event,
    ingest_slack_event,
    resolve_mapped_message,
    resolve_message_link,
)
from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgePermanentError,
)
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.worker import CommunityBridgeDiscordClient
from integrations.services.community_bridge.identity import (
    verified_identity_for_buzz,
    verified_identity_for_slack,
)


User = get_user_model()


class SlackBridgeClientProfileTests(TestCase):
    def setUp(self):
        SlackBridgeClient._profile_cache.clear()
        SlackBridgeClient._channel_name_cache.clear()

    def tearDown(self):
        SlackBridgeClient._profile_cache.clear()
        SlackBridgeClient._channel_name_cache.clear()

    @patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client")
    def test_profile_resolves_display_name_and_approved_avatar_once(self, mock_get_client):
        mock_get_client.return_value.users_info.return_value = {
            "ok": True,
            "user": {
                "profile": {
                    "display_name": "Alice Nguyen",
                    "real_name": "Alice N.",
                    "image_192": "https://avatars.slack-edge.com/2026-08-10/alice_192.png",
                }
            },
        }

        expected = {
            "display_name": "Alice Nguyen",
            "avatar_url": "https://avatars.slack-edge.com/2026-08-10/alice_192.png",
        }
        self.assertEqual(SlackBridgeClient.get_user_profile("U123"), expected)
        self.assertEqual(SlackBridgeClient.get_user_profile("U123"), expected)
        mock_get_client.return_value.users_info.assert_called_once_with(user="U123")

    @patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client")
    def test_profile_rejects_unapproved_avatar_host(self, mock_get_client):
        mock_get_client.return_value.users_info.return_value = {
            "ok": True,
            "user": {
                "profile": {
                    "display_name": "Alice",
                    "image_192": "https://evil.example/alice.png",
                }
            },
        }

        self.assertEqual(
            SlackBridgeClient.get_user_profile("U123"),
            {"display_name": "Alice", "avatar_url": ""},
        )

    @patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client")
    def test_message_text_resolves_user_and_unlabelled_channel_once(self, mock_get_client):
        mock_get_client.return_value.users_info.return_value = {
            "ok": True,
            "user": {"profile": {"display_name": "Alice Nguyen"}},
        }
        mock_get_client.return_value.conversations_info.return_value = {
            "ok": True,
            "channel": {"name": "general"},
        }

        raw_text = (
            "Ask <@U123> in <#C123> or <#C456|announcements>; "
            "notify <!channel>."
        )
        expected = "Ask @Alice Nguyen in #general or #announcements; notify @channel."

        self.assertEqual(SlackBridgeClient.resolve_message_text(raw_text), expected)
        self.assertEqual(SlackBridgeClient.resolve_message_text(raw_text), expected)
        mock_get_client.return_value.users_info.assert_called_once_with(user="U123")
        mock_get_client.return_value.conversations_info.assert_called_once_with(channel="C123")

    @patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client")
    def test_message_text_uses_safe_fallback_when_slack_lookup_fails(self, mock_get_client):
        mock_get_client.return_value.users_info.return_value = {"ok": False}
        mock_get_client.return_value.conversations_info.return_value = {"ok": False}

        self.assertEqual(
            SlackBridgeClient.resolve_message_text("Ask <@U404> in <#C404>"),
            "Ask @user in #channel",
        )

    @patch("integrations.services.community_bridge.slack.SlackBridgeClient.get_client")
    def test_messages_from_chat_enable_native_slack_link_unfurls(self, mock_get_client):
        mock_get_client.return_value.chat_postMessage.return_value = {
            "channel": "C123",
            "ts": "123.456",
        }

        SlackBridgeClient.post_message(
            channel_id="C123",
            text="Read https://example.com/article",
        )
        SlackBridgeClient.update_message(
            channel_id="C123",
            message_id="123.456",
            text="Updated https://example.com/article",
        )

        mock_get_client.return_value.chat_postMessage.assert_called_once_with(
            channel="C123",
            text="Read https://example.com/article",
            unfurl_links=True,
            unfurl_media=True,
        )
        mock_get_client.return_value.chat_update.assert_called_once_with(
            channel="C123",
            ts="123.456",
            text="Updated https://example.com/article",
            unfurl_links=True,
            unfurl_media=True,
        )


class RecentSlackBridgeReconciliationCommandTests(TestCase):
    def setUp(self):
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="T123",
            slack_channel_id="C123",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id="relay-general",
            destination_channel_name="general",
            enabled=True,
            sync_replies=True,
            sync_deletes=True,
        )

    @patch(
        "integrations.management.commands."
        "reconcile_recent_community_bridge_slack.call_command"
    )
    def test_reconciles_each_enabled_mapping_with_bounded_apply(self, mock_reconcile):
        def write_report(command_name, **kwargs):
            self.assertEqual(
                command_name, "reconcile_community_bridge_slack_threads"
            )
            kwargs["stdout"].write(
                json.dumps(
                    {
                        "totals": {
                            "errors": 0,
                            "mismatches": 2,
                            "repairs_enqueued": 2,
                            "links_restored": 0,
                        }
                    }
                )
            )

        mock_reconcile.side_effect = write_report
        output = StringIO()

        call_command(
            "reconcile_recent_community_bridge_slack",
            lookback_seconds=3600,
            max_roots_per_channel=50,
            maximum_history_messages=500,
            wait_seconds=30,
            stdout=output,
        )

        kwargs = mock_reconcile.call_args.kwargs
        self.assertEqual(kwargs["slack_channel_id"], ["C123"])
        self.assertTrue(kwargs["include_unthreaded"])
        self.assertTrue(kwargs["apply"])
        self.assertTrue(kwargs["confirm_historical_repair"])
        self.assertEqual(kwargs["max_roots"], 50)
        self.assertEqual(kwargs["maximum_history_messages"], 500)
        self.assertEqual(kwargs["wait_seconds"], 30)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"channels": 1, "errors": 0, "mismatches": 2, "repairs": 2},
        )


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
            delivery.payload["metadata"]["slack_raw_text"],
            "Hello <@U999> <http://example.com|Example>",
        )
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

    def test_approved_reaction_add_and_remove_share_one_owned_object(self):
        self.channel.destination_platform = CommunityBridgePlatform.BUZZ
        self.channel.destination_workspace_id = "mlai-community"
        self.channel.destination_channel_id = "a" * 32
        self.channel.save(
            update_fields=[
                "destination_platform",
                "destination_workspace_id",
                "destination_channel_id",
            ]
        )
        base_event = {
            "type": "reaction_added",
            "user": "U12345",
            "reaction": "thumbsup",
            "item": {
                "type": "message",
                "channel": self.channel.slack_channel_id,
                "ts": "1710000000.2000",
            },
        }
        added = self._post(
            {"type": "event_callback", "event_id": "EvReactionAdd", "event": base_event}
        )
        removed = self._post(
            {
                "type": "event_callback",
                "event_id": "EvReactionRemove",
                "event": {**base_event, "type": "reaction_removed"},
            }
        )

        self.assertEqual(added.data["status"], "enqueued")
        self.assertEqual(removed.data["status"], "enqueued")
        deliveries = list(CommunityBridgeDelivery.objects.order_by("id"))
        self.assertEqual(deliveries[0].delivery_type, CommunityBridgeDeliveryType.REACTION_ADD)
        self.assertEqual(deliveries[1].delivery_type, CommunityBridgeDeliveryType.REACTION_REMOVE)
        self.assertEqual(deliveries[0].source_message_id, deliveries[1].source_message_id)
        self.assertEqual(deliveries[0].source_parent_message_id, "1710000000.2000")
        self.assertEqual(deliveries[0].payload["text"], "👍")

    def test_reaction_for_legacy_discord_mapping_fails_closed(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvReactionDiscord",
                "event": {
                    "type": "reaction_added",
                    "user": "U12345",
                    "reaction": "thumbsup",
                    "item": {
                        "type": "message",
                        "channel": self.channel.slack_channel_id,
                        "ts": "1710000000.2000",
                    },
                },
            }
        )

        self.assertEqual(response.data["status"], "ignored")
        receipt = CommunityBridgeReceipt.objects.get(receipt_key="EvReactionDiscord")
        self.assertEqual(receipt.error_text, "reaction_sync_unsupported")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_unapproved_or_bridge_bot_reaction_is_ignored(self):
        for event_id, user, reaction in [
            ("EvReactionCustom", "U12345", "party_parrot"),
            ("EvReactionEcho", "UBRIDGEBOT", "thumbsup"),
        ]:
            response = self._post(
                {
                    "type": "event_callback",
                    "event_id": event_id,
                    "event": {
                        "type": "reaction_added",
                        "user": user,
                        "reaction": reaction,
                        "item": {
                            "type": "message",
                            "channel": self.channel.slack_channel_id,
                            "ts": "1710000000.2000",
                        },
                    },
                }
            )
            self.assertEqual(response.data["status"], "ignored")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

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

    def test_user_backed_slack_app_message_and_reply_are_enqueued(self):
        root = self._post(
            {
                "type": "event_callback",
                "event_id": "EvThirdPartyBotRoot",
                "event": {
                    "type": "message",
                    "subtype": "bot_message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "UROOBOT",
                    "bot_id": "BROO",
                    "ts": "1710000000.3100",
                    "text": "A community member earned points",
                },
            }
        )
        reply = self._post(
            {
                "type": "event_callback",
                "event_id": "EvThirdPartyBotReply",
                "event": {
                    "type": "message",
                    "subtype": "thread_broadcast",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "U12345",
                    "ts": "1710000000.3200",
                    "thread_ts": "1710000000.3100",
                    "text": "Great flex",
                },
            }
        )

        self.assertEqual(root.data["status"], "enqueued")
        self.assertEqual(reply.data["status"], "enqueued")
        deliveries = list(CommunityBridgeDelivery.objects.order_by("id"))
        self.assertEqual(deliveries[0].payload["source_author_id"], "UROOBOT")
        self.assertEqual(deliveries[0].source_parent_message_id, "")
        self.assertEqual(deliveries[1].source_parent_message_id, "1710000000.3100")
        self.assertTrue(deliveries[1].payload["metadata"]["broadcast"])
        self.assertEqual(
            deliveries[1].payload["metadata"]["slack_created_at"], 1710000000
        )

    def test_slack_app_message_without_user_is_ignored(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvBotWithoutUser",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "bot_id": "BNOUSER",
                    "ts": "1710000000.3300",
                    "text": "No stable Slack author",
                },
            }
        )

        self.assertEqual(response.data["status"], "ignored")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_user_backed_slack_app_edit_and_delete_are_enqueued(self):
        edit = self._post(
            {
                "type": "event_callback",
                "event_id": "EvThirdPartyBotEdit",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "message": {
                        "ts": "1710000000.3400",
                        "user": "UROOBOT",
                        "bot_id": "BROO",
                        "text": "Updated points",
                    },
                },
            }
        )
        delete = self._post(
            {
                "type": "event_callback",
                "event_id": "EvThirdPartyBotDelete",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "deleted_ts": "1710000000.3400",
                    "previous_message": {
                        "ts": "1710000000.3400",
                        "user": "UROOBOT",
                        "bot_id": "BROO",
                    },
                },
            }
        )

        self.assertEqual(edit.data["status"], "enqueued")
        self.assertEqual(delete.data["status"], "enqueued")
        self.assertEqual(
            list(
                CommunityBridgeDelivery.objects.order_by("id").values_list(
                    "delivery_type", flat=True
                )
            ),
            [CommunityBridgeDeliveryType.EDIT, CommunityBridgeDeliveryType.DELETE],
        )

    def test_hidden_plain_message_is_ignored(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvHiddenPlainMessage",
                "event": {
                    "type": "message",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "user": "U12345",
                    "ts": "1710000000.3500",
                    "text": "hidden create",
                    "hidden": True,
                },
            }
        )

        self.assertEqual(response.data["status"], "ignored")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_hidden_slack_edit_and_delete_events_are_enqueued(self):
        edit = self._post(
            {
                "type": "event_callback",
                "event_id": "EvHiddenMessageEdit",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "hidden": True,
                    "message": {
                        "ts": "1710000000.3600",
                        "user": "U12345",
                        "text": "edited message",
                    },
                },
            }
        )
        delete = self._post(
            {
                "type": "event_callback",
                "event_id": "EvHiddenMessageDelete",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel_type": "channel",
                    "channel": self.channel.slack_channel_id,
                    "hidden": True,
                    "deleted_ts": "1710000000.3600",
                    "previous_message": {
                        "ts": "1710000000.3600",
                        "user": "U12345",
                    },
                },
            }
        )

        self.assertEqual(edit.data["status"], "enqueued")
        self.assertEqual(delete.data["status"], "enqueued")
        deliveries = list(CommunityBridgeDelivery.objects.order_by("id"))
        self.assertEqual(
            [delivery.delivery_type for delivery in deliveries],
            [CommunityBridgeDeliveryType.EDIT, CommunityBridgeDeliveryType.DELETE],
        )
        self.assertEqual(
            [delivery.source_message_id for delivery in deliveries],
            ["1710000000.3600", "1710000000.3600"],
        )

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

    def test_mapped_private_channel_message_is_mirrored(self):
        response = self._post(
            {
                "type": "event_callback",
                "event_id": "EvPrivateChannel",
                "event": {
                    "type": "message",
                    "channel_type": "group",
                    "channel": self.channel.slack_channel_id,
                    "user": "U12345",
                    "ts": "1710000000.7000",
                    "text": "private channel message",
                },
            }
        )

        self.assertEqual(response.data["status"], "enqueued")
        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(delivery.source_message_id, "1710000000.7000")
        self.assertEqual(delivery.payload["text"], "private channel message")

    def test_direct_messages_are_never_mirrored(self):
        for index, channel_type in enumerate(("im", "mpim")):
            response = self._post(
                {
                    "type": "event_callback",
                    "event_id": f"EvDirectMessage{index}",
                    "event": {
                        "type": "message",
                        "channel_type": channel_type,
                        "channel": self.channel.slack_channel_id,
                        "user": "U12345",
                        "ts": f"1710000000.80{index}0",
                        "text": "direct message",
                    },
                }
            )
            self.assertEqual(response.data["status"], "ignored", channel_type)

        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_mapping_capability_flags_fail_closed(self):
        self.channel.sync_edits = False
        self.channel.sync_deletes = False
        self.channel.sync_replies = False
        self.channel.save(update_fields=["sync_edits", "sync_deletes", "sync_replies"])
        payloads = [
            {
                "type": "message",
                "subtype": "message_changed",
                "channel_type": "channel",
                "channel": self.channel.slack_channel_id,
                "message": {"ts": "1710000000.6000", "user": "U123", "text": "edited"},
            },
            {
                "type": "message",
                "subtype": "message_deleted",
                "channel_type": "channel",
                "channel": self.channel.slack_channel_id,
                "deleted_ts": "1710000000.6000",
                "previous_message": {"ts": "1710000000.6000", "user": "U123"},
            },
            {
                "type": "message",
                "channel_type": "channel",
                "channel": self.channel.slack_channel_id,
                "user": "U123",
                "ts": "1710000000.6001",
                "thread_ts": "1710000000.6000",
                "text": "reply",
            },
        ]
        reasons = []
        for index, event in enumerate(payloads):
            response = self._post(
                {
                    "type": "event_callback",
                    "event_id": f"EvCapability{index}",
                    "event": event,
                }
            )
            self.assertEqual(response.data["status"], "ignored")
            reasons.append(
                CommunityBridgeReceipt.objects.get(
                    receipt_key=f"EvCapability{index}"
                ).error_text
            )
        self.assertEqual(
            reasons,
            ["edit_sync_disabled", "delete_sync_disabled", "reply_sync_disabled"],
        )
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
            slack_workspace_id="T-MLAI",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="nostr-channel-event-id",
            destination_channel_name="community",
            stdout=out,
        )

        payload = json.loads(out.getvalue())
        channel = CommunityBridgeChannel.objects.get(slack_channel_id="C-MLAI-CHAT")
        self.assertEqual(payload["destination_platform"], CommunityBridgePlatform.BUZZ)
        self.assertEqual(channel.slack_workspace_id, "T-MLAI")
        self.assertEqual(channel.destination_workspace_id, "chat.mlai.au")
        self.assertEqual(channel.destination_channel_id, "nostr-channel-event-id")
        self.assertEqual(channel.discord_channel_id, "")

    def test_command_requires_workspace_for_buzz_mapping(self):
        with self.assertRaisesMessage(
            CommandError,
            "--slack-workspace-id is required for MLAI Chat mappings.",
        ):
            call_command(
                "upsert_community_bridge_channel",
                slack_channel_id="C-MLAI-CHAT",
                destination_platform=CommunityBridgePlatform.BUZZ,
                destination_channel_id="nostr-channel-event-id",
            )

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
            slack_workspace_id="T-MLAI",
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

    def test_mapped_parent_resolves_in_both_directions(self):
        link = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.0001",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id="a" * 64,
        )

        direct = resolve_mapped_message(
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=link.source_message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
        )
        reverse = resolve_mapped_message(
            source_platform=CommunityBridgePlatform.BUZZ,
            source_channel_id=self.channel.destination_channel_id,
            source_message_id=link.destination_message_id,
            destination_platform=CommunityBridgePlatform.SLACK,
        )

        self.assertEqual(direct["destination_message_id"], "a" * 64)
        self.assertEqual(reverse["destination_message_id"], "1710000000.0001")
        self.assertEqual(reverse["destination_channel_id"], self.channel.slack_channel_id)

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
            slack_workspace_id="T-MLAI",
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
            source_workspace_id="T-MLAI",
            source_channel_id="C-MLAI-CHAT",
            source_message_id="1710000000.1000",
            source_author_id="U123",
            source_author_display_name="Alice Nguyen",
            source_author_avatar_url="https://avatars.slack-edge.com/2026-08-10/alice_192.png",
            linked_pubkey="9" * 64,
            source_created_at=1710000000,
            broadcast=False,
        )

        self.assertEqual(result["message_id"], "a" * 64)
        call = mock_post.call_args
        self.assertEqual(call.args[0], "http://buzz-bridge-adapter:8090/v1/deliveries")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + "a" * 40)
        self.assertEqual(call.kwargs["json"]["delivery_id"], "42")
        self.assertEqual(call.kwargs["json"]["created_at"], 1785568000)
        self.assertEqual(call.kwargs["json"]["source_workspace_id"], "T-MLAI")
        self.assertEqual(call.kwargs["json"]["source_author_display_name"], "Alice Nguyen")
        self.assertEqual(
            call.kwargs["json"]["source_author_avatar_url"],
            "https://avatars.slack-edge.com/2026-08-10/alice_192.png",
        )
        self.assertEqual(call.kwargs["json"]["linked_pubkey"], "9" * 64)
        self.assertEqual(call.kwargs["json"]["source_created_at"], 1710000000)
        self.assertFalse(call.kwargs["json"]["broadcast"])
        self.assertEqual(call.kwargs["timeout"], 12)

    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_lookup_validates_trusted_adapter_response(self, mock_post):
        mock_post.return_value = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "channel_id": "922c3b22-8002-4c3c-a37b-ce406a5e606e",
                "matches": [
                    {
                        "source_message_id": "1710000000.1000",
                        "destination_message_id": "a" * 64,
                        "parent_message_id": "b" * 64,
                        "broadcast": True,
                        "created_at": 1785568000,
                    }
                ],
            },
        )

        matches = BuzzBridgeClient.lookup_messages(
            channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            source_workspace_id="TMLAI",
            source_channel_id="CGENERAL",
            source_message_ids=["1710000000.1000"],
        )

        self.assertEqual(matches[0]["destination_message_id"], "a" * 64)
        self.assertEqual(matches[0]["parent_message_id"], "b" * 64)
        self.assertTrue(matches[0]["broadcast"])
        self.assertEqual(
            mock_post.call_args.args[0],
            "http://buzz-bridge-adapter:8090/v1/lookups",
        )

    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_private_batch_uses_one_authenticated_adapter_request(self, mock_post):
        channel_id = "922c3b22-8002-4c3c-a37b-ce406a5e606e"
        deliveries = [
            {
                "delivery_id": str(index),
                "created_at": 1785568000 + index,
                "operation": "create",
                "channel_id": channel_id,
                "participant_pubkeys": ["1" * 64, "2" * 64],
                "text": f"private {index}",
                "source_workspace_id": "TMLAI",
                "source_channel_id": "DONE",
                "source_message_id": f"178556800{index}.000100",
                "source_author_id": "UONE",
                "source_author_display_name": "One",
                "source_author_avatar_url": None,
                "linked_pubkey": "1" * 64,
                "target_message_id": None,
                "parent_message_id": None,
            }
            for index in range(2)
        ]
        mock_post.return_value = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "deliveries": [
                    {
                        "channel_id": channel_id,
                        "message_id": character * 64,
                        "parent_message_id": "",
                    }
                    for character in ("a", "b")
                ]
            },
        )

        result = BuzzBridgeClient.deliver_private_batch(deliveries)

        self.assertEqual([item["message_id"] for item in result], ["a" * 64, "b" * 64])
        self.assertEqual(
            mock_post.call_args.args[0],
            "http://buzz-bridge-adapter:8090/v1/private-deliveries/batch",
        )
        self.assertEqual(mock_post.call_args.kwargs["json"], {"deliveries": deliveries})
        self.assertEqual(mock_post.call_count, 1)

    @patch("integrations.services.community_bridge.buzz.requests.post")
    def test_private_batch_falls_back_during_an_adapter_rolling_deploy(self, mock_post):
        channel_id = "922c3b22-8002-4c3c-a37b-ce406a5e606e"
        deliveries = [
            {
                "delivery_id": str(index),
                "channel_id": channel_id,
                "text": f"private {index}",
            }
            for index in range(2)
        ]
        mock_post.side_effect = [
            SimpleNamespace(ok=False, status_code=404),
            SimpleNamespace(
                ok=True,
                status_code=200,
                json=lambda: {
                    "channel_id": channel_id,
                    "message_id": "a" * 64,
                    "parent_message_id": "",
                },
            ),
            SimpleNamespace(
                ok=True,
                status_code=200,
                json=lambda: {
                    "channel_id": channel_id,
                    "message_id": "b" * 64,
                    "parent_message_id": "",
                },
            ),
        ]

        result = BuzzBridgeClient.deliver_private_batch(deliveries)

        self.assertEqual([item["message_id"] for item in result], ["a" * 64, "b" * 64])
        self.assertEqual(
            [call.args[0] for call in mock_post.call_args_list],
            [
                "http://buzz-bridge-adapter:8090/v1/private-deliveries/batch",
                "http://buzz-bridge-adapter:8090/v1/private-deliveries",
                "http://buzz-bridge-adapter:8090/v1/private-deliveries",
            ],
        )

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


class CommunityBridgeDeadLetterReplayTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI",
            slack_channel_id="CMLAICHAT",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
        )
        self.delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.DEAD,
            source_event_key="EvDead",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.9000",
            target_channel_id=self.channel.destination_channel_id,
            attempts=5,
            last_error="transient provider failure",
            available_at=timezone.now(),
        )

    def test_requeue_preserves_delivery_id_and_resets_retry_state(self):
        original_id = self.delivery.id
        original_created_at = self.delivery.created_at
        call_command(
            "requeue_community_bridge_delivery",
            str(original_id),
            confirm=True,
        )
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.id, original_id)
        self.assertEqual(self.delivery.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(self.delivery.attempts, 0)
        self.assertEqual(self.delivery.last_error, "")
        self.assertEqual(self.delivery.created_at, original_created_at)

    def test_requeue_requires_confirmation_and_dead_status(self):
        with self.assertRaisesMessage(CommandError, "--confirm is required"):
            call_command("requeue_community_bridge_delivery", str(self.delivery.id))
        self.delivery.status = CommunityBridgeDeliveryStatus.FAILED
        self.delivery.save(update_fields=["status"])
        with self.assertRaisesMessage(CommandError, "only a dead"):
            call_command(
                "requeue_community_bridge_delivery",
                str(self.delivery.id),
                confirm=True,
            )

    def test_timestamp_refresh_requires_both_explicit_safety_confirmations(self):
        with self.assertRaisesMessage(CommandError, "--confirm-stale-relay-timestamp"):
            call_command(
                "requeue_community_bridge_delivery",
                str(self.delivery.id),
                confirm=True,
                refresh_event_timestamp=True,
            )
        with self.assertRaisesMessage(CommandError, "--confirm-no-destination-event"):
            call_command(
                "requeue_community_bridge_delivery",
                str(self.delivery.id),
                confirm=True,
                refresh_event_timestamp=True,
                confirm_stale_relay_timestamp=True,
            )

    def test_timestamp_refresh_is_guarded_by_absent_destination_link(self):
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=self.delivery.source_platform,
            source_channel_id=self.delivery.source_channel_id,
            source_message_id=self.delivery.source_message_id,
            destination_platform=self.delivery.target_platform,
            destination_channel_id=self.delivery.target_channel_id,
            destination_message_id="a" * 64,
        )

        with self.assertRaisesMessage(CommandError, "destination message link exists"):
            call_command(
                "requeue_community_bridge_delivery",
                str(self.delivery.id),
                confirm=True,
                refresh_event_timestamp=True,
                confirm_stale_relay_timestamp=True,
                confirm_no_destination_event=True,
            )

    def test_confirmed_stale_timestamp_refresh_preserves_delivery_id(self):
        stale_created_at = timezone.now() - timedelta(hours=1)
        CommunityBridgeDelivery.objects.filter(id=self.delivery.id).update(
            created_at=stale_created_at
        )

        call_command(
            "requeue_community_bridge_delivery",
            str(self.delivery.id),
            confirm=True,
            refresh_event_timestamp=True,
            confirm_stale_relay_timestamp=True,
            confirm_no_destination_event=True,
        )

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertGreater(self.delivery.created_at, stale_created_at)


class CommunityBridgeStagingVerificationTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI",
            slack_channel_id="CMLAICHAT",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="a" * 32,
        )
        for source_platform, source_id, destination_platform, destination_id in (
            (
                CommunityBridgePlatform.SLACK,
                "1710000000.9000",
                CommunityBridgePlatform.BUZZ,
                "b" * 64,
            ),
            (
                CommunityBridgePlatform.BUZZ,
                "c" * 64,
                CommunityBridgePlatform.SLACK,
                "1710000001.9000",
            ),
        ):
            CommunityBridgeMessageLink.objects.create(
                channel=self.channel,
                source_platform=source_platform,
                source_channel_id=(
                    self.channel.slack_channel_id
                    if source_platform == CommunityBridgePlatform.SLACK
                    else self.channel.destination_channel_id
                ),
                source_message_id=source_id,
                destination_platform=destination_platform,
                destination_channel_id=(
                    self.channel.destination_channel_id
                    if destination_platform == CommunityBridgePlatform.BUZZ
                    else self.channel.slack_channel_id
                ),
                destination_message_id=destination_id,
            )
            CommunityBridgeDelivery.objects.create(
                channel=self.channel,
                target_platform=destination_platform,
                source_platform=source_platform,
                delivery_type=CommunityBridgeDeliveryType.CREATE,
                status=CommunityBridgeDeliveryStatus.COMPLETED,
                source_event_key=f"stage:{source_id}",
                source_channel_id=(
                    self.channel.slack_channel_id
                    if source_platform == CommunityBridgePlatform.SLACK
                    else self.channel.destination_channel_id
                ),
                source_message_id=source_id,
                target_channel_id=(
                    self.channel.destination_channel_id
                    if destination_platform == CommunityBridgePlatform.BUZZ
                    else self.channel.slack_channel_id
                ),
                available_at=timezone.now(),
                completed_at=timezone.now(),
            )

    def test_verifier_reports_durable_bidirectional_evidence(self):
        output = StringIO()
        call_command(
            "verify_community_bridge_staging",
            slack_channel_id=self.channel.slack_channel_id,
            slack_message_id="1710000000.9000",
            buzz_event_id="c" * 64,
            stdout=output,
        )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["dead_delivery_count"], 0)

    def test_verifier_fails_when_mapping_has_a_dead_delivery(self):
        CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.EDIT,
            status=CommunityBridgeDeliveryStatus.DEAD,
            source_event_key="stage:dead",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="different-message",
            target_channel_id=self.channel.destination_channel_id,
            available_at=timezone.now(),
        )
        with self.assertRaisesMessage(CommandError, "dead delivery"):
            call_command(
                "verify_community_bridge_staging",
                slack_channel_id=self.channel.slack_channel_id,
                slack_message_id="1710000000.9000",
                buzz_event_id="c" * 64,
            )


class CommunityBridgeInspectionTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI",
            slack_channel_id="CGENERAL",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="a" * 32,
            destination_channel_name="general",
        )

    def test_inspector_reports_payload_free_delivery_metadata(self):
        receipt = CommunityBridgeReceipt.objects.create(
            channel=self.channel,
            platform=CommunityBridgePlatform.SLACK,
            receipt_key="Ev-inspect-1",
            event_type="message",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.9000",
            status=CommunityBridgeReceiptStatus.ENQUEUED,
            queued_delivery_count=1,
            payload={"event": {"text": "must not be reported"}},
            processed_at=timezone.now(),
        )
        CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            receipt=receipt,
            target_platform=CommunityBridgePlatform.BUZZ,
            source_platform=CommunityBridgePlatform.SLACK,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
            source_event_key=receipt.receipt_key,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=receipt.source_message_id,
            target_channel_id=self.channel.destination_channel_id,
            payload={"text": "must not be reported"},
            available_at=timezone.now(),
            completed_at=timezone.now(),
        )
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=receipt.source_message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id="b" * 64,
            source_payload={"text": "must not be reported"},
        )

        output = StringIO()
        call_command(
            "inspect_community_bridge",
            slack_channel_id=self.channel.slack_channel_id,
            slack_message_id=receipt.source_message_id,
            stdout=output,
        )
        result = json.loads(output.getvalue())

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["receipts"][0]["status"], CommunityBridgeReceiptStatus.ENQUEUED)
        self.assertEqual(result["deliveries"][0]["status"], CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(result["message_links"][0]["destination_message_id"], "b" * 64)
        self.assertNotIn("must not be reported", output.getvalue())

    def test_inspector_reports_not_found_without_exposing_other_messages(self):
        CommunityBridgeReceipt.objects.create(
            channel=self.channel,
            platform=CommunityBridgePlatform.SLACK,
            receipt_key="Ev-other",
            event_type="message",
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000001.1000",
            payload={"event": {"text": "private payload"}},
        )
        output = StringIO()

        call_command(
            "inspect_community_bridge",
            slack_channel_id=self.channel.slack_channel_id,
            slack_message_id="1710000002.2000",
            stdout=output,
        )
        result = json.loads(output.getvalue())

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["receipts"], [])
        self.assertEqual(result["recent_receipts"]["count"], 1)
        self.assertNotIn("private payload", output.getvalue())


@override_settings(SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge")
class CommunityBridgeSlackThreadRepairTests(TestCase):
    def setUp(self):
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI",
            slack_channel_id="CGENERAL",
            slack_channel_name="general",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="a" * 32,
            destination_channel_name="general",
        )
        self.root_message_id = "1786660929.427979"
        self.reply_message_id = "1786666478.295369"
        self.thread = [
            {
                "ts": self.root_message_id,
                "user": "UROOBOT",
                "bot_id": "BROO",
                "text": "Alan earned points",
            },
            {
                "ts": self.reply_message_id,
                "thread_ts": self.root_message_id,
                "user": "USAM",
                "text": "Great flex",
            },
        ]

    def _command(self, phase):
        output = StringIO()
        call_command(
            "repair_community_bridge_slack_thread",
            slack_channel_id=self.channel.slack_channel_id,
            root_message_id=self.root_message_id,
            phase=phase,
            apply=True,
            confirm_historical_repair=True,
            stdout=output,
        )
        return json.loads(output.getvalue())

    @patch(
        "integrations.management.commands.repair_community_bridge_slack_thread."
        "SlackBridgeClient.get_thread_messages"
    )
    def test_root_phase_enqueues_app_authored_root_with_fresh_relay_timestamp(self, mock_thread):
        mock_thread.return_value = self.thread
        started_at = timezone.now()

        result = self._command("root")
        repeated = self._command("root")

        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(repeated["existing"], 1)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)
        self.assertEqual(delivery.delivery_type, CommunityBridgeDeliveryType.CREATE)
        self.assertEqual(delivery.source_message_id, self.root_message_id)
        self.assertEqual(delivery.source_parent_message_id, "")
        self.assertEqual(delivery.payload["source_author_id"], "UROOBOT")
        self.assertEqual(delivery.payload["metadata"]["slack_raw_text"], "Alan earned points")
        self.assertEqual(delivery.payload["metadata"]["slack_created_at"], 1786660929)
        self.assertGreaterEqual(delivery.created_at, started_at)
        self.assertLessEqual(delivery.created_at, timezone.now())

    @patch(
        "integrations.management.commands.repair_community_bridge_slack_thread."
        "SlackBridgeClient.get_thread_messages"
    )
    def test_delete_then_recreate_phase_reparents_existing_top_level_reply(self, mock_thread):
        mock_thread.return_value = self.thread
        root_destination_id = "b" * 64
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=self.root_message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=root_destination_id,
        )
        orphan = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id=self.reply_message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id="c" * 64,
            destination_parent_message_id="",
        )

        deleted = self._command("delete_orphans")
        orphan.source_deleted_at = timezone.now()
        orphan.destination_deleted_at = timezone.now()
        orphan.save(update_fields=["source_deleted_at", "destination_deleted_at"])
        recreate_started_at = timezone.now()
        recreated = self._command("recreate_orphans")

        deliveries = list(CommunityBridgeDelivery.objects.order_by("id"))
        self.assertEqual(deleted["enqueued"], 1)
        self.assertEqual(recreated["enqueued"], 1)
        self.assertEqual(
            [delivery.delivery_type for delivery in deliveries],
            [CommunityBridgeDeliveryType.DELETE, CommunityBridgeDeliveryType.CREATE],
        )
        self.assertEqual(deliveries[1].source_parent_message_id, self.root_message_id)
        self.assertEqual(deliveries[1].payload["text"], "Great flex")
        self.assertEqual(
            deliveries[1].payload["metadata"]["slack_created_at"],
            1786666478,
        )
        self.assertGreaterEqual(deliveries[1].created_at, recreate_started_at)
        self.assertLessEqual(deliveries[1].created_at, timezone.now())


@override_settings(
    SLACK_BRIDGE_BOT_TOKEN="xoxb-bridge",
    BUZZ_BRIDGE_ADAPTER_URL="http://buzz-bridge-adapter:8090",
    BUZZ_BRIDGE_ADAPTER_TOKEN="a" * 40,
    BUZZ_BRIDGE_CALLBACK_SECRET="b" * 40,
)
class BuzzCommunityBridgeWorkerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alice.bridge@example.com",
            password="Correct-Horse-Bridge-9!",
            slack_id="U123",
            first_name="Alice",
        )
        self.device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key="9" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="T-MLAI",
            slack_channel_id="C-MLAI-CHAT",
            slack_channel_name="community",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_workspace_id="chat.mlai.au",
            destination_channel_id="922c3b22-8002-4c3c-a37b-ce406a5e606e",
            destination_channel_name="community",
        )
        self.client = CommunityBridgeDiscordClient()
        self.identity = CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="T-MLAI",
            slack_user_id="U123",
            buzz_pubkey="9" * 64,
            display_name="Alice",
            verification_method=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
            verification_reference="ops-proof-123",
            verified_at=timezone.now(),
        )

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

    @patch(
        "integrations.services.community_bridge.worker.SlackBridgeClient.resolve_message_text",
        return_value="Ask @Alice Nguyen in #general",
    )
    @patch(
        "integrations.services.community_bridge.worker.SlackBridgeClient.get_user_profile",
        return_value={"display_name": "Alice", "avatar_url": ""},
    )
    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_create_resolves_slack_mentions_before_buzz_delivery(
        self, mock_deliver, _mock_profile, mock_resolve_text
    ):
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            message_id="1710000000.1500",
        )
        delivery.payload["metadata"] = {
            "slack_raw_text": "Ask <@U999> in <#C999>"
        }
        delivery.save(update_fields=["payload"])
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "f" * 64,
            "parent_message_id": "",
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        mock_resolve_text.assert_called_once_with("Ask <@U999> in <#C999>")
        self.assertIn("Ask @Alice Nguyen in #general", mock_deliver.call_args.kwargs["text"])
        self.assertNotIn("<@U999>", mock_deliver.call_args.kwargs["text"])

    @patch(
        "integrations.services.community_bridge.worker.SlackBridgeClient.get_user_profile",
        return_value={
            "display_name": "Alice Nguyen",
            "avatar_url": "https://avatars.slack-edge.com/2026-08-10/alice_192.png",
        },
    )
    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_create_preserves_reply_and_creates_message_link(self, mock_deliver, _mock_profile):
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
        delivery.payload["metadata"] = {
            "broadcast": True,
            "slack_created_at": 1710000000,
        }
        delivery.save(update_fields=["payload"])
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
        self.assertEqual(kwargs["created_at"], int(delivery.created_at.timestamp()))
        self.assertEqual(kwargs["source_workspace_id"], "T-MLAI")
        self.assertEqual(kwargs["source_channel_id"], self.channel.slack_channel_id)
        self.assertEqual(kwargs["source_message_id"], "1710000000.2000")
        self.assertEqual(kwargs["source_author_id"], "U123")
        self.assertEqual(kwargs["source_author_display_name"], "Alice")
        self.assertEqual(
            kwargs["source_author_avatar_url"],
            "https://avatars.slack-edge.com/2026-08-10/alice_192.png",
        )
        self.assertEqual(kwargs["linked_pubkey"], "9" * 64)
        self.assertEqual(kwargs["source_created_at"], 1710000000)
        self.assertTrue(kwargs["broadcast"])
        link = resolve_message_link(
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2000",
            destination_platform=CommunityBridgePlatform.BUZZ,
        )
        self.assertEqual(link["destination_message_id"], "2" * 64)

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_reply_waits_for_parent_mapping_instead_of_becoming_top_level(self, mock_deliver):
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            message_id="1710000000.2200",
            parent_id="1710000000.2100",
        )

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.FAILED)
        self.assertIn("CommunityBridgeParentNotReady", delivery.last_error)
        self.assertIn("1710000000.2100", delivery.last_error)
        mock_deliver.assert_not_called()

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_recreated_reply_clears_deleted_link_markers(self, mock_deliver):
        parent_event_id = "a" * 64
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2300",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=parent_event_id,
        )
        deleted_at = timezone.now()
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.2400",
            source_parent_message_id="1710000000.2300",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id="b" * 64,
            source_deleted_at=deleted_at,
            destination_deleted_at=deleted_at,
        )
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            message_id="1710000000.2400",
            parent_id="1710000000.2300",
        )
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "c" * 64,
            "parent_message_id": parent_event_id,
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        link = CommunityBridgeMessageLink.objects.get(
            source_message_id="1710000000.2400"
        )
        self.assertEqual(link.destination_message_id, "c" * 64)
        self.assertEqual(link.destination_parent_message_id, parent_event_id)
        self.assertIsNone(link.source_deleted_at)
        self.assertIsNone(link.destination_deleted_at)

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

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_reconciliation_override_deletes_unlinked_duplicate(self, mock_deliver):
        duplicate_event_id = "6" * 64
        delivery = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.DELETE,
            message_id="1710000000.4100",
        )
        delivery.payload["metadata"] = {
            "destination_message_id_override": duplicate_event_id,
            "slack_created_at": 1710000000,
        }
        delivery.save(update_fields=["payload"])
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "9" * 64,
            "parent_message_id": "",
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(
            mock_deliver.call_args.kwargs["target_message_id"], duplicate_event_id
        )
        self.assertFalse(
            CommunityBridgeMessageLink.objects.filter(
                source_message_id="1710000000.4100"
            ).exists()
        )

    @patch("integrations.services.community_bridge.worker.BuzzBridgeClient.deliver")
    def test_slack_reaction_add_and_remove_target_their_mapped_objects(self, mock_deliver):
        original_event_id = "7" * 64
        reaction_event_id = "8" * 64
        source_reaction_id = "reaction:" + "a" * 64
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=self.channel.slack_channel_id,
            source_message_id="1710000000.7000",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=self.channel.destination_channel_id,
            destination_message_id=original_event_id,
        )
        added = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.REACTION_ADD,
            message_id=source_reaction_id,
            parent_id="1710000000.7000",
        )
        added.payload = {
            "source_author_id": "U123",
            "text": "👍",
            "metadata": {
                "slack_message_id": "1710000000.7000",
                "slack_reaction": "thumbsup",
            },
        }
        added.save(update_fields=["payload"])
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": reaction_event_id,
            "parent_message_id": original_event_id,
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))
        add_kwargs = mock_deliver.call_args.kwargs
        self.assertEqual(add_kwargs["operation"], CommunityBridgeDeliveryType.REACTION_ADD)
        self.assertEqual(add_kwargs["target_message_id"], original_event_id)
        self.assertEqual(add_kwargs["source_message_id"], "1710000000.7000")
        self.assertEqual(add_kwargs["text"], "👍")

        removed = self._delivery(
            delivery_type=CommunityBridgeDeliveryType.REACTION_REMOVE,
            message_id=source_reaction_id,
            parent_id="1710000000.7000",
        )
        removed.payload = {"source_author_id": "U123", "text": "👍"}
        removed.save(update_fields=["payload"])
        mock_deliver.return_value = {
            "channel_id": self.channel.destination_channel_id,
            "message_id": "9" * 64,
            "parent_message_id": "",
        }

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))
        remove_kwargs = mock_deliver.call_args.kwargs
        self.assertEqual(remove_kwargs["operation"], CommunityBridgeDeliveryType.REACTION_REMOVE)
        self.assertEqual(remove_kwargs["target_message_id"], reaction_event_id)
        link = CommunityBridgeMessageLink.objects.get(source_message_id=source_reaction_id)
        self.assertIsNotNone(link.destination_deleted_at)

    @patch("integrations.services.community_bridge.worker.SlackBridgeClient.remove_reaction")
    @patch("integrations.services.community_bridge.worker.SlackBridgeClient.add_reaction")
    def test_buzz_reaction_round_trip_uses_slack_reaction_api(
        self,
        mock_add_reaction,
        mock_remove_reaction,
    ):
        original_event_id = "a" * 64
        reaction_event_id = "b" * 64
        slack_message_id = "1710000000.8000"
        CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_channel_id=self.channel.destination_channel_id,
            source_message_id=original_event_id,
            destination_platform=CommunityBridgePlatform.SLACK,
            destination_channel_id=self.channel.slack_channel_id,
            destination_message_id=slack_message_id,
        )
        added = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.SLACK,
            source_platform=CommunityBridgePlatform.BUZZ,
            delivery_type=CommunityBridgeDeliveryType.REACTION_ADD,
            source_event_key="buzz-reaction-add",
            source_channel_id=self.channel.destination_channel_id,
            source_message_id=reaction_event_id,
            source_parent_message_id=original_event_id,
            target_channel_id=self.channel.slack_channel_id,
            payload={"source_author_id": "c" * 64, "text": "👍", "attachments": []},
            available_at=timezone.now(),
        )

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))
        added.refresh_from_db()
        self.assertEqual(added.status, CommunityBridgeDeliveryStatus.COMPLETED)
        mock_add_reaction.assert_called_once_with(
            channel_id=self.channel.slack_channel_id,
            message_id=slack_message_id,
            reaction="thumbsup",
        )

        removed = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.SLACK,
            source_platform=CommunityBridgePlatform.BUZZ,
            delivery_type=CommunityBridgeDeliveryType.REACTION_REMOVE,
            source_event_key="buzz-reaction-remove",
            source_channel_id=self.channel.destination_channel_id,
            source_message_id=reaction_event_id,
            source_parent_message_id=original_event_id,
            target_channel_id=self.channel.slack_channel_id,
            payload={"source_author_id": "c" * 64, "text": "👍", "attachments": []},
            available_at=timezone.now(),
        )
        asyncio.run(self.client.process_pending_deliveries_once(limit=5))
        removed.refresh_from_db()
        self.assertEqual(removed.status, CommunityBridgeDeliveryStatus.COMPLETED)
        mock_remove_reaction.assert_called_once_with(
            channel_id=self.channel.slack_channel_id,
            message_id=slack_message_id,
            reaction="thumbsup",
        )

    @patch("integrations.services.community_bridge.worker.SlackBridgeClient.post_message")
    def test_verified_buzz_identity_is_used_for_slack_attribution(self, mock_post):
        mock_post.return_value = {
            "channel": self.channel.slack_channel_id,
            "message_id": "1710000000.9000",
        }
        delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel,
            target_platform=CommunityBridgePlatform.SLACK,
            source_platform=CommunityBridgePlatform.BUZZ,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            source_event_key="buzz-create-linked",
            source_channel_id=self.channel.destination_channel_id,
            source_message_id="8" * 64,
            target_channel_id=self.channel.slack_channel_id,
            payload={
                "source_author_id": "9" * 64,
                "source_author_display_name": "",
                "text": "Hello from MLAI Chat",
                "attachments": [],
            },
            available_at=timezone.now(),
        )

        asyncio.run(self.client.process_pending_deliveries_once(limit=5))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertIn("Alice (MLAI Chat)", mock_post.call_args.kwargs["text"])
        self.assertRegex(
            mock_post.call_args.kwargs["client_msg_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )


class CommunityBridgeAccountIdentityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="account-identity@example.com",
            password="Correct-Horse-Account-9!",
            slack_id="U-ACCOUNT",
            first_name="Current",
            last_name="Name",
        )
        self.original_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key="1" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.rotated_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key="2" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        self.link = CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="T-ACCOUNT",
            slack_user_id="U-ACCOUNT",
            buzz_pubkey=self.original_device.public_key,
            display_name="Stale Name",
            verification_method=CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE,
            verification_reference="account-link-1",
            verified_at=timezone.now(),
        )

    def test_slack_identity_uses_account_profile_and_rotates_revoked_device(self):
        identity = verified_identity_for_slack(
            slack_workspace_id="T-ACCOUNT",
            slack_user_id="U-ACCOUNT",
        )
        self.assertEqual(identity["identity_source"], "mlai_account")
        self.assertEqual(identity["display_name"], "Current Name")
        self.assertEqual(identity["buzz_pubkey"], self.original_device.public_key)

        self.original_device.status = DeviceBindingStatus.REVOKED
        self.original_device.revoked_at = timezone.now()
        self.original_device.save(update_fields=["status", "revoked_at", "updated_at"])

        identity = verified_identity_for_slack(
            slack_workspace_id="T-ACCOUNT",
            slack_user_id="U-ACCOUNT",
        )
        self.assertEqual(identity["buzz_pubkey"], self.rotated_device.public_key)

    def test_any_active_account_device_resolves_to_the_same_slack_identity(self):
        identity = verified_identity_for_buzz(
            slack_workspace_id="T-ACCOUNT",
            buzz_pubkey=self.rotated_device.public_key,
        )
        self.assertEqual(identity["slack_user_id"], "U-ACCOUNT")
        self.assertEqual(identity["user_profile_id"], str(self.user.community_chat_profile_id))

    def test_revoked_device_does_not_resolve(self):
        self.rotated_device.status = DeviceBindingStatus.REVOKED
        self.rotated_device.revoked_at = timezone.now()
        self.rotated_device.save(update_fields=["status", "revoked_at", "updated_at"])

        self.assertIsNone(
            verified_identity_for_buzz(
                slack_workspace_id="T-ACCOUNT",
                buzz_pubkey=self.rotated_device.public_key,
            )
        )

    def test_legacy_key_link_remains_readable_during_migration_window(self):
        CommunityBridgeIdentityLink.objects.create(
            slack_workspace_id="T-LEGACY",
            slack_user_id="U-LEGACY",
            buzz_pubkey="3" * 64,
            display_name="Legacy",
            verification_method=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
            verification_reference="legacy-link",
            verified_at=timezone.now(),
        )
        identity = verified_identity_for_buzz(
            slack_workspace_id="T-LEGACY",
            buzz_pubkey="3" * 64,
        )
        self.assertEqual(identity["identity_source"], "legacy_key")


class CommunityBridgeIdentityCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alice.identity@example.com",
            password="Correct-Horse-Identity-9!",
            slack_id="U123",
            first_name="Alice",
        )
        self.device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key="a" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )

    def _verify(self, **overrides):
        options = {
            "slack_workspace_id": "T-MLAI",
            "slack_user_id": "U123",
            "mlai_profile_id": str(self.user.community_chat_profile_id),
            "buzz_pubkey": "a" * 64,
            "display_name": "Alice",
            "verification_method": CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
            "verification_reference": "ticket-123-and-signed-challenge-456",
            "confirm_dual_control": True,
            "stdout": StringIO(),
        }
        options.update(overrides)
        call_command("verify_community_bridge_identity", **options)
        return options["stdout"]

    def test_verification_requires_explicit_dual_control_confirmation(self):
        with self.assertRaisesMessage(CommandError, "--confirm-dual-control is required"):
            self._verify(confirm_dual_control=False)

    def test_verification_can_select_the_accounts_active_device(self):
        out = self._verify(buzz_pubkey=None)
        self.assertEqual(json.loads(out.getvalue())["buzz_pubkey"], self.device.public_key)

    def test_verify_and_revoke_identity_link(self):
        out = self._verify()
        link = CommunityBridgeIdentityLink.objects.get()

        self.assertEqual(json.loads(out.getvalue())["status"], "created")
        self.assertEqual(link.slack_workspace_id, "T-MLAI")
        self.assertEqual(link.slack_user_id, "U123")
        self.assertEqual(link.user, self.user)
        self.assertEqual(link.buzz_pubkey, "a" * 64)
        self.assertIsNone(link.revoked_at)

        revoke_out = StringIO()
        call_command(
            "revoke_community_bridge_identity",
            slack_workspace_id="T-MLAI",
            slack_user_id="U123",
            reason="user requested unlink",
            stdout=revoke_out,
        )
        link.refresh_from_db()
        self.assertEqual(json.loads(revoke_out.getvalue())["status"], "revoked")
        self.assertIsNotNone(link.revoked_at)
        self.assertEqual(link.revocation_reason, "user requested unlink")

    def test_account_cannot_link_to_a_different_slack_user(self):
        self._verify()

        with self.assertRaisesMessage(CommandError, "Slack user is not connected"):
            self._verify(slack_user_id="U456")


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
