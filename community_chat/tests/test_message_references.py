from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
)
from integrations.services.community_bridge.formatting import (
    normalize_slack_thread_references,
    slack_message_reference,
)
from integrations.services.community_bridge.store import _normalize_slack_event
from integrations.services.slack_dm_mirror import _slack_message_text
from community_chat.slack_message_references import (
    resolve_slack_message_reference,
    SlackMessageReferenceError,
)

URL = "https://example.slack.com/archives/CREF/p1789000000000001"
EVENT_ID = "a" * 64
CHANNEL_ID = "9a1657ac-f7aa-5db0-b632-d8bbeb6dfb50"


class ThreadAttachmentTests(SimpleTestCase):
    def test_canonical_only_and_never_copies_quoted_body(self):
        self.assertEqual(slack_message_reference(URL), ("CREF", "1789000000.000001"))
        for url in (
            "http://example.slack.com/archives/CREF/p1789000000000001",
            URL.replace("example.slack.com", "evil.test"),
            URL.replace("https://", "https://user@"),
            URL.replace("example.slack.com", "example.slack.com:444"),
        ):
            self.assertIsNone(slack_message_reference(url))
        self.assertEqual(
            normalize_slack_thread_references(
                [
                    {
                        "original_url": URL,
                        "text": "private quote",
                        "fallback": "secret author",
                    },
                    {"from_url": URL},
                    {"title_link": "https://example.com/ordinary"},
                ]
            ),
            [{"title": "Thread", "url": URL}],
        )

    def test_create_edit_and_private_import_retain_references(self):
        message = {
            "user": "UAUTHOR",
            "ts": "1789001000.000002",
            "text": "Reminder",
            "attachments": [
                {"is_msg_unfurl": True, "from_url": URL, "fallback": "private quote"}
            ],
        }
        for event in (
            {
                **message,
                "type": "message",
                "channel_type": "channel",
                "channel": "CSOURCE",
            },
            {
                "type": "message",
                "subtype": "message_changed",
                "channel_type": "channel",
                "channel": "CSOURCE",
                "message": message,
            },
        ):
            payload = _normalize_slack_event({"event": event})
            self.assertEqual(payload["attachments"], [{"title": "Thread", "url": URL}])
            self.assertNotIn("private quote", str(payload))
        self.assertEqual(_slack_message_text(message), f"Reminder\n\nThread: <{URL}>")


class MessageReferenceAccessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="reference@example.com")
        self.channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="CREF",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=CHANNEL_ID,
        )
        self.link = CommunityBridgeMessageLink.objects.create(
            channel=self.channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id="CREF",
            source_message_id="1789000000.000001",
            source_author_id="UAUTHOR",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=CHANNEL_ID,
            destination_message_id=EVENT_ID,
            source_payload={"text": "private body never returned"},
        )

    def test_public_mapping_returns_only_address_and_not_persisted_body(self):
        result = resolve_slack_message_reference(URL, user=self.user)
        self.assertEqual(
            result["thread"], {"channel_id": CHANNEL_ID, "message_id": EVENT_ID}
        )
        self.assertNotIn("private body", str(result))

    def test_unauthenticated_deleted_disabled_and_unknown_fail_closed(self):
        with self.assertRaises(SlackMessageReferenceError):
            resolve_slack_message_reference(
                URL, user=SimpleNamespace(is_authenticated=False)
            )
        self.channel.enabled = False
        self.channel.save()
        with self.assertRaises(SlackMessageReferenceError):
            resolve_slack_message_reference(URL, user=self.user)
        self.assertIsNone(
            resolve_slack_message_reference("https://example.com", user=self.user)
        )

    def test_private_mapping_is_owner_scoped_and_revocation_is_immediate(self):
        from django.utils import timezone
        from integrations.models import (
            ExternalServiceConnection,
            ExternalServiceProvider,
            SlackDmMirrorGrant,
            SlackDmMirrorConversation,
            SlackDmMirrorDelivery,
        )

        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="synthetic-test-token",
            external_account_id="TTEST",
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection,
            slack_workspace_id="TTEST",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        conversation = SlackDmMirrorConversation.objects.create(
            grant=grant,
            slack_workspace_id="TTEST",
            slack_conversation_id="DPRIVATE",
            participant_hash="current-reference-audience",
            status="live",
            mlai_channel_id=CHANNEL_ID,
        )
        SlackDmMirrorDelivery.objects.create(
            conversation=conversation,
            source_platform="slack",
            source_message_id="1789000000.000001",
            source_author_id="UALICE",
            operation="create",
            status="completed",
            available_at=timezone.now(),
            completed_at=timezone.now(),
            metadata={
                "destination_message_id": EVENT_ID,
                "participant_hash": conversation.participant_hash,
            },
        )
        url = URL.replace("CREF", "DPRIVATE")
        self.assertEqual(
            resolve_slack_message_reference(url, user=self.user)["thread"][
                "message_id"
            ],
            EVENT_ID,
        )
        other = get_user_model().objects.create_user(
            email="other-reference@example.com"
        )
        with self.assertRaises(SlackMessageReferenceError):
            resolve_slack_message_reference(url, user=other)
        grant.revoked_at = timezone.now()
        grant.save()
        with self.assertRaises(SlackMessageReferenceError):
            resolve_slack_message_reference(url, user=self.user)

    def test_deleted_public_mapping_is_unavailable(self):
        from django.utils import timezone

        self.link.source_deleted_at = timezone.now()
        self.link.save()
        with self.assertRaises(SlackMessageReferenceError):
            resolve_slack_message_reference(URL, user=self.user)

    def test_historical_repair_is_dry_by_default_and_only_enqueues_one_edit(self):
        from io import StringIO
        from django.core.management import call_command
        from integrations.models import CommunityBridgeDelivery

        message = {
            "ts": self.link.source_message_id,
            "user": "UAUTHOR",
            "text": "Reminder",
            "attachments": [{"from_url": URL.replace("CREF", "COTHER")}],
        }
        with patch(
            "integrations.services.community_bridge.slack.SlackBridgeClient.get_channel_history",
            return_value=[message],
        ), patch(
            "integrations.services.community_bridge.slack.SlackBridgeClient.resolve_message_text",
            side_effect=lambda value: value,
        ):
            call_command(
                "backfill_community_bridge_thread_references",
                slack_channel_id="CREF",
                stdout=StringIO(),
            )
            self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)
            for _ in range(2):
                call_command(
                    "backfill_community_bridge_thread_references",
                    slack_channel_id="CREF",
                    apply=True,
                    confirm_historical_edits=True,
                    stdout=StringIO(),
                )
        delivery = CommunityBridgeDelivery.objects.get()
        self.assertEqual(delivery.delivery_type, "edit")
        self.assertEqual(delivery.source_message_id, self.link.source_message_id)
        self.assertEqual(delivery.payload["metadata"]["slack_created_at"], 1789000000)
        self.assertEqual(
            delivery.payload["attachments"],
            [{"title": "Thread", "url": URL.replace("CREF", "COTHER")}],
        )

    def test_historical_repair_skips_newer_edits(self):
        from io import StringIO
        from django.core.management import call_command
        from integrations.models import CommunityBridgeDelivery

        def history(**kwargs):
            self.link.save()  # A normal delivery updated the link after the read began.
            return [
                {
                    "ts": self.link.source_message_id,
                    "user": "UAUTHOR",
                    "text": "Older snapshot",
                    "attachments": [{"from_url": URL}],
                }
            ]

        with patch(
            "integrations.services.community_bridge.slack.SlackBridgeClient.get_channel_history",
            side_effect=history,
        ), patch(
            "integrations.services.community_bridge.slack.SlackBridgeClient.resolve_message_text",
            side_effect=lambda value: value,
        ):
            call_command(
                "backfill_community_bridge_thread_references",
                slack_channel_id="CREF",
                apply=True,
                confirm_historical_edits=True,
                stdout=StringIO(),
            )
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 0)

    def test_endpoint_never_scrapes_unavailable_slack_threads(self):
        from rest_framework.test import APIClient
        from django.urls import reverse

        client = APIClient()
        client.force_authenticate(self.user)
        with patch(
            "community_chat.views.fetch_link_preview",
            side_effect=AssertionError("Slack thread must not use generic scraper"),
        ):
            response = client.get(reverse("community_chat_link_preview"), {"url": URL})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["thread"]["message_id"], EVENT_ID)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.channel.enabled = False
            self.channel.save()
            denied = client.get(reverse("community_chat_link_preview"), {"url": URL})
            self.assertEqual(denied.status_code, 422)
            self.assertNotIn("thread", denied.data)
