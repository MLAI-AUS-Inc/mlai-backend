"""Database-free contract checks for private Slack import classification."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from community_chat.slack_views import _import_history_days
from integrations.services.slack_chat_catalog import (
    CATALOG_KEY,
    PRIVATE_CHANNEL_CONSENT,
    PRIVATE_CHANNEL_SCOPES,
    catalog_payload,
    catalog_tombstones,
    conversation_kind,
    private_channels_enabled,
    raw_conversation_kind,
)
from integrations.services import slack_dm_mirror as mirror


class SlackChatCatalogTests(SimpleTestCase):
    def conversation(self, channel_id="CPRIVATE", kind="private_channel"):
        connection = SimpleNamespace(
            scopes=list(PRIVATE_CHANNEL_SCOPES),
            provider_metadata={
                CATALOG_KEY: {channel_id: {"kind": kind, "name": "planning"}}
            },
        )
        grant = SimpleNamespace(
            connection=connection, consent_version=PRIVATE_CHANNEL_CONSENT
        )
        return SimpleNamespace(
            grant=grant,
            slack_conversation_id=channel_id,
            mlai_channel_id="mirror",
            participant_buzz_pubkeys=["owner-device", "import-shadow"],
        )

    def test_default_is_seven_and_only_seven_or_thirty_can_be_requested(self):
        self.assertEqual(_import_history_days({}), 7)
        self.assertEqual(_import_history_days({"history_days": 30}), 30)
        for value in (0, -1, 31, 365, True, "30", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _import_history_days({"history_days": value})

    def test_slack_flags_distinguish_private_channels_from_group_dms(self):
        self.assertEqual(
            raw_conversation_kind({"id": "CPRIVATE", "is_private": True}),
            "private_channel",
        )
        self.assertEqual(
            raw_conversation_kind({"id": "GPRIVATE", "is_private": True}),
            "private_channel",
        )
        self.assertEqual(
            raw_conversation_kind(
                {"id": "GGROUP", "is_mpim": True, "is_private": True}
            ),
            "mpim",
        )
        self.assertIsNone(raw_conversation_kind({"id": "CPUBLIC"}))

    def test_disconnect_tombstones_erase_names_but_preserve_private_consent_boundary(
        self,
    ):
        metadata = self.conversation("GPRIVATE").grant.connection.provider_metadata
        metadata["other_private_metadata"] = "discard"
        self.assertEqual(
            catalog_tombstones(metadata), {"GPRIVATE": {"kind": "private_channel"}}
        )

    def test_catalog_is_scoped_to_the_provisioned_device(self):
        conversation = self.conversation()
        self.assertEqual(
            catalog_payload([conversation], "owner-device"),
            [{"channel_id": "mirror", "kind": "private_channel"}],
        )
        self.assertEqual(catalog_payload([conversation], "other-member"), [])
        self.assertEqual(catalog_payload([conversation], None), [])

    def test_existing_consent_does_not_silently_import_private_channels(self):
        conversation = self.conversation()
        self.assertTrue(private_channels_enabled(conversation.grant))
        conversation.grant.consent_version = "slack-dm-mirror-v3-owner-direct-and-group"
        self.assertFalse(private_channels_enabled(conversation.grant))
        conversation.grant.consent_version = PRIVATE_CHANNEL_CONSENT
        conversation.grant.connection.scopes = []
        self.assertFalse(private_channels_enabled(conversation.grant))

    def test_history_scopes_use_source_type_instead_of_slack_id_prefix(self):
        conversation = self.conversation("GPRIVATE")
        scopes = mirror._history_required_scopes(
            "GPRIVATE", kind=conversation_kind(conversation)
        )
        self.assertTrue(PRIVATE_CHANNEL_SCOPES.issubset(scopes))
        self.assertNotIn("mpim:history", scopes)

    def test_existing_private_mirror_cannot_resume_body_io_without_consent(self):
        conversation = self.conversation()
        mirror._require_private_channel_consent(conversation)
        conversation.grant.consent_version = "slack-dm-mirror-v3-owner-direct-and-group"
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            mirror._require_private_channel_consent(conversation)

    @patch.object(mirror, "_call_slack_with_grant_authority")
    def test_private_channels_page_all_members_beyond_the_dm_limit(self, call):
        call.side_effect = [
            {
                "members": ["OWNER"] + [f"U{i}" for i in range(200)],
                "response_metadata": {"next_cursor": "more"},
            },
            {"members": ["LAST"], "response_metadata": {}},
        ]
        members = mirror._conversation_participant_ids(
            None, {"id": "CPRIVATE", "is_private": True}, owner_slack_user_id="OWNER"
        )
        self.assertEqual(len(members), 202)
        self.assertIn("LAST", members)
        self.assertEqual(call.call_args.kwargs["cursor"], "more")

    @patch.object(mirror, "_call_slack_with_grant_authority")
    def test_removed_owner_cannot_import_a_private_channel(self, call):
        call.return_value = {"members": ["OTHER"], "response_metadata": {}}
        self.assertEqual(
            mirror._conversation_participant_ids(
                None,
                {"id": "CPRIVATE", "is_private": True},
                owner_slack_user_id="OWNER",
            ),
            [],
        )

    def test_group_catalog_uses_slack_people_not_transport_or_owner_device_keys(self):
        conversation = self.conversation("GGROUP", "mpim")
        conversation.grant.slack_user_id = "OWNER"
        conversation.participant_slack_ids = ["OWNER", "ALICE", "BOB", "BOB"]
        conversation.participant_profiles = {
            "OWNER": {
                "display_name": "Sam",
                "avatar_url": "https://avatars.slack-edge.com/sam.png",
            },
            "ALICE": {
                "display_name": "Alice",
                "avatar_url": "https://avatars.slack-edge.com/alice.png",
            },
            "REMOVED": {"display_name": "Removed member"},
        }
        people = catalog_payload([conversation], "owner-device")[0]["participants"]
        self.assertEqual(
            [person["slack_user_id"] for person in people], ["OWNER", "ALICE", "BOB"]
        )
        self.assertEqual(
            people[1]["avatar_url"], "https://avatars.slack-edge.com/alice.png"
        )
        self.assertEqual(people[2]["display_name"], "BOB")
        self.assertTrue(people[0]["is_owner"])
        self.assertFalse(people[1]["is_owner"])
        self.assertEqual(catalog_payload([conversation], "another-account"), [])
