from django.test import SimpleTestCase

from integrations.services.community_bridge.contracts import (
    BridgeAttachment,
    BridgeDeliveryResult,
    CanonicalBridgeEvent,
)


class CanonicalBridgeEventTests(SimpleTestCase):
    def test_create_event_serializes_provider_neutral_payload(self):
        event = CanonicalBridgeEvent(
            receipt_key="Ev-1",
            source_platform="slack",
            source_channel_id="C123",
            source_message_id="1710000000.0001",
            source_parent_message_id="1710000000.0000",
            source_author_id="U123",
            source_author_display_name="Sam",
            delivery_type="create",
            text="Hello",
            attachments=(BridgeAttachment(title="notes.txt", url="https://files.example/notes.txt"),),
            metadata={"bridge_origin": "slack"},
        )

        self.assertEqual(
            event.normalized_payload(),
            {
                "delivery_type": "create",
                "source_channel_id": "C123",
                "source_message_id": "1710000000.0001",
                "source_parent_message_id": "1710000000.0000",
                "source_author_id": "U123",
                "source_author_display_name": "Sam",
                "text": "Hello",
                "attachments": [
                    {"title": "notes.txt", "url": "https://files.example/notes.txt"}
                ],
                "metadata": {"bridge_origin": "slack"},
            },
        )

    def test_buzz_is_a_supported_platform(self):
        event = CanonicalBridgeEvent(
            receipt_key="buzz-event-id",
            source_platform="buzz",
            source_channel_id="nostr-channel-id",
            source_message_id="nostr-event-id",
            delivery_type="create",
        )
        self.assertEqual(event.source_platform, "buzz")

    def test_delete_event_rejects_retained_content(self):
        with self.assertRaisesMessage(ValueError, "delete events must not retain message content"):
            CanonicalBridgeEvent(
                receipt_key="Ev-delete",
                source_platform="slack",
                source_channel_id="C123",
                source_message_id="1710000000.0001",
                delivery_type="delete",
                text="message body must be discarded",
            )

    def test_unsupported_platform_fails_closed(self):
        with self.assertRaisesMessage(ValueError, "source_platform is unsupported"):
            CanonicalBridgeEvent(
                receipt_key="Ev-2",
                source_platform="unknown",
                source_channel_id="channel",
                source_message_id="message",
                delivery_type="create",
            )

    def test_attachment_rejects_non_http_urls(self):
        with self.assertRaisesMessage(ValueError, "attachment.url must use http or https"):
            BridgeAttachment(title="secret", url="file:///tmp/secret")


class BridgeDeliveryResultTests(SimpleTestCase):
    def test_destination_identifiers_are_required(self):
        with self.assertRaisesMessage(ValueError, "destination_message_id is required"):
            BridgeDeliveryResult(destination_channel_id="channel", destination_message_id="")

