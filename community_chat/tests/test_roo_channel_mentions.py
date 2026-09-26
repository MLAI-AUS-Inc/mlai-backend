"""Roo channel mentions retain Slack identity and owner-only delivery authority."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from community_chat.tests import test_slack_dm_mirror as fixtures
from community_chat.tests.privacy_fixtures import PROVIDERS, grant_test_ai_consent
from integrations.models import CommunityBridgeDeliveryStatus, CommunityBridgePlatform
from integrations.services import slack_dm_mirror as mirror
from integrations.services.slack_channel_mentions import (
    render_outgoing_roo_mentions,
    roo_channel_targets,
    validate_roo_channel_access,
)
from integrations.services.slack_chat_catalog import (
    CATALOG_KEY,
    PRIVATE_CHANNEL_CONSENT,
    catalog_payload,
)

SETTINGS = dict(
    COMMUNITY_CHAT_AI_PROVIDERS=PROVIDERS,
    COMMUNITY_CHAT_AI_DISCLOSURE_VERSION="test-v1",
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
TAG = ["slack-mention", "UROO", "Roo"]
BOT = {"id": "UROO", "team_id": "TMLAI", "is_bot": True}
CHANNEL = {"id": "CMASTER", "is_private": True}


def conversation_fixture():
    return SimpleNamespace(
        slack_workspace_id="TMLAI",
        slack_conversation_id="CMASTER",
        participant_slack_ids=["UONE", "UROO", "UTWO"],
        participant_profiles={
            "UROO": {
                "display_name": "Roo",
                "avatar_url": "https://example.test/roo.png",
            }
        },
        participant_buzz_pubkeys=["1" * 64],
        mlai_channel_id="private-mirror",
        status="live",
        history_backfilled_at=None,
        grant=SimpleNamespace(
            slack_user_id="UONE",
            status="active",
            revoked_at=None,
            consented_at=timezone.now(),
            history_days=30,
            consent_version=PRIVATE_CHANNEL_CONSENT,
            connection=SimpleNamespace(
                provider_metadata={
                    CATALOG_KEY: {"CMASTER": {"kind": "private_channel"}}
                }
            ),
        ),
    )


@override_settings(**SETTINGS)
class RooChannelMentionTests(SimpleTestCase):
    def test_catalog_exposes_only_member_bot_to_owner_device(self):
        conversation = conversation_fixture()
        self.assertEqual(
            catalog_payload([conversation], "1" * 64)[0]["mention_targets"][0][
                "slack_user_id"
            ],
            "UROO",
        )
        self.assertEqual(catalog_payload([conversation], "2" * 64), [])
        for members in (["UONE"], ["UROO"], []):
            conversation.participant_slack_ids = members
            self.assertEqual(roo_channel_targets(conversation), [])
        conversation = conversation_fixture()
        for metadata in (
            {"kind": "im"},
            {"kind": "mpim"},
            {"kind": "private_channel", "source_archived": True},
        ):
            conversation.grant.connection.provider_metadata[CATALOG_KEY][
                "CMASTER"
            ] = metadata
            self.assertEqual(roo_channel_targets(conversation), [])
        conversation = conversation_fixture()
        conversation.slack_workspace_id = "TOTHER"
        self.assertEqual(roo_channel_targets(conversation), [])

    def test_only_explicit_selected_identity_becomes_a_slack_mention(self):
        conversation = conversation_fixture()
        self.assertEqual(
            render_outgoing_roo_mentions("Hello @Roo", [], conversation),
            ("Hello @Roo", []),
        )
        self.assertEqual(
            render_outgoing_roo_mentions("Hello **@roo**, @Roo!", [TAG], conversation),
            ("Hello **<@UROO>**, <@UROO>!", ["UROO"]),
        )
        for tag in (
            ["slack-mention", "UOTHER", "Roo"],
            ["slack-mention", "UROO"],
            ["slack-mention", "UROO", "@Roo"],
            ["slack-mention", "UROO", "Roo\n"],
        ):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                render_outgoing_roo_mentions("@Roo", [tag], conversation)
        with self.assertRaises(ValueError):
            render_outgoing_roo_mentions("@Roo", [TAG] * 21, conversation)

    def test_code_email_and_partial_names_do_not_trigger_roo(self):
        literals = [
            "`@Roo`",
            "``@Roo ` example``",
            "```py\n@Roo\n```",
            "~~~\n@Roo\n~~~",
            "    @Roo",
            "\t@Roo",
            "mail@Roo",
            "@Room",
            "@Roo-bot",
        ]
        for literal in literals:
            with self.subTest(literal=literal):
                text = "@Roo please review:\n" + literal
                self.assertEqual(
                    render_outgoing_roo_mentions(text, [TAG], conversation_fixture()),
                    ("<@UROO> please review:\n" + literal, ["UROO"]),
                )

    def test_fresh_slack_checks_reject_removed_bot_owner_or_shared_channel(self):
        conversation = conversation_fixture()
        self.assertTrue(
            validate_roo_channel_access(conversation, CHANNEL, {"UONE", "UROO"}, BOT)
        )
        for field in (
            "is_archived",
            "is_ext_shared",
            "is_org_shared",
            "is_shared",
            "is_mpim",
        ):
            self.assertFalse(
                validate_roo_channel_access(
                    conversation, {**CHANNEL, field: True}, {"UONE", "UROO"}, BOT
                )
            )
        for members in ({"UONE"}, {"UROO"}, set()):
            self.assertFalse(
                validate_roo_channel_access(conversation, CHANNEL, members, BOT)
            )
        for changes in (
            {"deleted": True},
            {"id": "UOTHER"},
            {"team_id": "TOTHER"},
            {"is_bot": False},
        ):
            self.assertFalse(
                validate_roo_channel_access(
                    conversation, CHANNEL, {"UONE", "UROO"}, {**BOT, **changes}
                )
            )

    def test_live_roo_replies_and_mutations_require_explicit_private_channel_scope(
        self,
    ):
        message = {
            "type": "message",
            "channel": "CMASTER",
            "user": "UROO",
            "bot_id": "BROO",
            "subtype": "bot_message",
            "ts": "1789000000.000001",
            "thread_ts": "1788999999.000001",
            "text": "Answer",
        }
        payload = {"team_id": "TMLAI", "event_id": "EvROO"}
        self.assertIsNone(mirror._normalize_private_slack_event(payload, message))
        normalized = mirror._normalize_private_slack_event(
            payload, message, allow_roo_channels=True
        )
        self.assertEqual(normalized["metadata"]["thread_ts"], message["thread_ts"])
        self.assertIsNone(
            mirror._normalize_private_slack_event(
                payload, {**message, "user": "UOTHER"}, allow_roo_channels=True
            )
        )
        for subtype, field in (
            ("message_changed", "message"),
            ("message_deleted", "previous_message"),
        ):
            event = {
                "type": "message",
                "channel": "CMASTER",
                "subtype": subtype,
                "event_ts": "1789000001.000001",
                field: message,
            }
            self.assertEqual(
                mirror._normalize_private_slack_event(
                    payload, event, allow_roo_channels=True
                )["source_author_id"],
                "UROO",
            )
        conversation = conversation_fixture()
        self.assertTrue(mirror._history_message_author_allowed(conversation, message))
        conversation.participant_slack_ids.remove("UROO")
        self.assertFalse(mirror._history_message_author_allowed(conversation, message))


@override_settings(**SETTINGS)
class RooChannelDeliveryTests(TestCase):
    def setUp(self):
        fixtures.SlackDmMirrorOwnerTests.setUp(self)
        grant_test_ai_consent(self.first)
        self.grant, self.conversation = (
            fixtures.SlackDmMirrorOwnerTests._live_conversation(
                self,
                participant_slack_ids=["UONE", "UROO", "UTWO"],
                participant_identity_map={
                    "UONE": "1" * 64,
                    "UROO": "3" * 64,
                    "UTWO": "3" * 64,
                },
            )
        )
        self.first_connection.scopes = fixtures.OAUTH_SCOPES
        self.first_connection.provider_metadata[CATALOG_KEY] = {
            "CMASTER": {"kind": "private_channel"}
        }
        self.first_connection.save()
        self.grant.consent_version = PRIVATE_CHANNEL_CONSENT
        self.grant.save()
        self.conversation.slack_conversation_id = "CMASTER"
        self.conversation.save()

    def payload(self, event="a", parent="", author="1", tags=None, operation="create"):
        return {
            "receipt_key": f"message_{operation}:{event * 64}",
            "source_channel_id": str(self.conversation.mlai_channel_id),
            "raw_payload": {"tags": [TAG] if tags is None else tags},
            "normalized_event": {
                "delivery_type": operation,
                "source_message_id": event * 64,
                "source_parent_message_id": parent,
                "source_author_id": author * 64,
                "text": "@Roo please help",
            },
        }

    def slack_client(self, web_client, members=None):
        client = web_client.return_value
        client.conversations_info.return_value = {"channel": CHANNEL}
        client.conversations_members.return_value = {
            "members": ["UONE", "UROO", "UTWO"] if members is None else members
        }
        client.users_info.return_value = {"user": BOT}
        client.chat_postMessage.return_value = {"ts": "1789000000.000001"}
        client.chat_update.return_value = {"ts": "1789000000.000001"}
        return client

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_root_and_thread_deliver_real_mentions_once_with_original_thread(
        self, web_client
    ):
        client = self.slack_client(web_client)
        first = mirror.ingest_mlai_dm_event(self.payload())
        self.assertEqual(first["status"], "enqueued")
        delivery = self.conversation.deliveries.get(pk=first["delivery_id"])
        self.assertEqual(delivery.encrypted_text, "<@UROO> please help")
        self.assertEqual(delivery.metadata["slack_mention_ids"], ["UROO"])
        self.assertEqual(mirror.process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            client.chat_postMessage.call_args.kwargs["text"], "<@UROO> please help"
        )
        self.assertEqual(
            mirror.ingest_mlai_dm_event(self.payload())["status"], "duplicate"
        )
        reply = mirror.ingest_mlai_dm_event(self.payload(event="b", parent="a" * 64))
        self.assertEqual(reply["status"], "enqueued")
        self.assertEqual(mirror.process_ready_deliveries(limit=1), 1)
        self.assertEqual(
            client.chat_postMessage.call_args.kwargs["thread_ts"], "1789000000.000001"
        )
        self.assertEqual(client.chat_postMessage.call_count, 2)
        client.conversations_invite.assert_not_called()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_removed_owner_prevents_post_even_after_message_was_queued(
        self, web_client
    ):
        client = self.slack_client(web_client, members=["UROO", "UTWO"])
        self.assertEqual(
            mirror.ingest_mlai_dm_event(self.payload())["status"], "enqueued"
        )
        self.assertEqual(mirror.process_ready_deliveries(limit=1), 0)
        client.chat_postMessage.assert_not_called()

    def test_non_owner_and_wrong_slack_identity_cannot_enqueue_mentions(self):
        self.assertEqual(
            mirror.ingest_mlai_dm_event(self.payload(author="2"))["status"], "ignored"
        )
        self.assertEqual(
            mirror.ingest_mlai_dm_event(
                self.payload(tags=[["slack-mention", "invalid", "Roo"]])
            )["status"],
            "rejected",
        )
        self.assertFalse(self.conversation.deliveries.exists())

    @patch.object(mirror.BuzzBridgeClient, "provision_private_conversation")
    @patch.object(mirror, "WebClient")
    def test_roo_reply_waits_for_membership_refresh_and_keeps_thread(
        self, web_client, provision
    ):
        # Discover a real private mirror so its registration hash and owner
        # shadow identity are established by the production path.
        self.conversation.delete()
        client = self.slack_client(web_client)
        client.users_conversations.return_value = {
            "channels": [{**CHANNEL, "name": "master-app"}],
            "response_metadata": {},
        }
        client.users_list.return_value = {
            "members": [
                {"id": "UONE", "name": "Owner"},
                BOT,
                {"id": "UTWO", "name": "Member"},
            ],
            "response_metadata": {},
        }
        client.users_info.side_effect = lambda *, user: {
            "user": {
                "id": user,
                "team_id": "TMLAI",
                "name": "Roo" if user == "UROO" else user,
                "is_bot": user == "UROO",
                "profile": {},
            }
        }
        client.conversations_history.return_value = {
            "messages": [],
            "response_metadata": {},
        }
        provision.side_effect = lambda pubkeys, **kwargs: {
            "channel_id": str(self.conversation.mlai_channel_id),
            "participant_pubkeys": pubkeys,
        }
        payload = {
            "team_id": "TMLAI",
            "event_id": "EvPrivateRoo",
            "authorizations": [{"user_id": "UONE"}],
            "event": {
                "type": "message",
                "channel_type": "group",
                "channel": "CMASTER",
                "user": "UROO",
                "bot_id": "BROO",
                "subtype": "bot_message",
                "text": "Roo's answer",
                "ts": "1789000010.000001",
                "thread_ts": "1789000000.000001",
            },
        }
        result = mirror.ingest_slack_dm_event(payload)
        self.assertEqual(result["status"], "discovery_queued")
        self.assertFalse(self.grant.conversations.filter(deliveries__source_author_id="UROO").exists())
        mirror.discover_conversations(self.grant)
        self.conversation = self.grant.conversations.get(slack_conversation_id="CMASTER")
        reply = self.conversation.deliveries.get(source_author_id="UROO")
        self.assertEqual(reply.metadata["thread_ts"], "1789000000.000001")
        self.assertEqual(reply.encrypted_text, "Roo's answer")
        self.assertEqual(reply.source_platform, CommunityBridgePlatform.SLACK)
        mirror.ingest_slack_dm_event(payload)
        mirror.discover_conversations(self.grant)
        self.assertEqual(
            self.conversation.deliveries.filter(source_author_id="UROO").count(), 1
        )
