import uuid
from unittest.mock import patch

from django.test import SimpleTestCase

from integrations.services.community_bridge.buzz import (
    BuzzBridgeClient,
    BuzzBridgePermanentError,
)
from integrations.services.community_bridge.contracts import (
    BridgeAttachment,
    BridgeDeliveryResult,
    CanonicalBridgeEvent,
)
from integrations.services.community_bridge.formatting import (
    emoji_to_slack_reaction,
    slack_reaction_to_emoji,
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
            source_author_avatar_url="https://avatars.slack-edge.com/sam.png",
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
                "source_author_avatar_url": "https://avatars.slack-edge.com/sam.png",
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

    def test_reaction_operations_preserve_the_owned_reaction_object(self):
        added = CanonicalBridgeEvent(
            receipt_key="reaction-add",
            source_platform="slack",
            source_channel_id="C123",
            source_message_id="reaction:abc",
            source_parent_message_id="1710000000.0001",
            source_author_id="U123",
            delivery_type="reaction_add",
            text="👍",
        )
        removed = CanonicalBridgeEvent(
            receipt_key="reaction-remove",
            source_platform="slack",
            source_channel_id="C123",
            source_message_id=added.source_message_id,
            source_parent_message_id=added.source_parent_message_id,
            source_author_id="U123",
            delivery_type="reaction_remove",
            text="👍",
        )
        self.assertEqual(added.normalized_payload()["source_parent_message_id"], "1710000000.0001")
        self.assertEqual(removed.normalized_payload()["source_message_id"], "reaction:abc")

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


class SlackReactionFormattingTests(SimpleTestCase):
    def test_common_unicode_reactions_keep_their_native_emoji(self):
        self.assertEqual(slack_reaction_to_emoji("heart"), "❤️")
        self.assertEqual(emoji_to_slack_reaction("❤️"), "heart")

    def test_valid_custom_shortcodes_round_trip_without_execution_markup(self):
        self.assertEqual(slack_reaction_to_emoji("ship_it+1"), ":ship_it+1:")
        self.assertEqual(emoji_to_slack_reaction(":ship_it+1:"), "ship_it+1")
        boundary_name = "a" * 62
        self.assertEqual(
            slack_reaction_to_emoji(boundary_name),
            f":{boundary_name}:",
        )
        self.assertEqual(
            emoji_to_slack_reaction(f":{boundary_name}:"),
            boundary_name,
        )

    def test_invalid_custom_shortcodes_fail_closed(self):
        self.assertEqual(slack_reaction_to_emoji("../secret"), "")
        self.assertEqual(emoji_to_slack_reaction(":two words:"), "")
        self.assertEqual(emoji_to_slack_reaction(":PartyParrot:"), "")
        self.assertEqual(emoji_to_slack_reaction(":_private:"), "")
        self.assertEqual(slack_reaction_to_emoji("a" * 63), "")


class PrivateConversationRegistrationTests(SimpleTestCase):
    @patch.object(BuzzBridgeClient, "_post_adapter")
    def test_registration_separates_callback_authors_from_all_participants(
        self,
        post_adapter,
    ):
        participants = ["1" * 64, "2" * 64, "3" * 64]
        callback_authors = ["1" * 64, "3" * 64]
        channel_id = str(uuid.uuid4())
        post_adapter.return_value = {
            "channel_id": channel_id,
            "participant_pubkeys": participants,
            "callback_author_pubkeys": callback_authors,
        }

        result = BuzzBridgeClient.provision_private_conversation(
            participants,
            callback_author_pubkeys=callback_authors,
            conversation_name="Slack DM",
        )

        self.assertEqual(result["callback_author_pubkeys"], callback_authors)
        post_adapter.assert_called_once_with(
            "v1/private-conversations",
            {
                "participant_pubkeys": participants,
                "callback_author_pubkeys": callback_authors,
                "conversation_name": "Slack DM",
            },
        )

    def test_registration_rejects_callback_author_outside_participants(self):
        with self.assertRaisesMessage(
            BuzzBridgePermanentError,
            "Private callback authors must be participant public keys",
        ):
            BuzzBridgeClient.provision_private_conversation(
                ["1" * 64, "2" * 64],
                callback_author_pubkeys=["3" * 64],
            )
