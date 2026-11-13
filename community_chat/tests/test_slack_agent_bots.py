"""Owner-configured Slack agent bots (for example an OpenAI dot) use the Roo mirror path."""

import json
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from community_chat.tests.test_roo_channel_mentions import conversation_fixture
from integrations.services import slack_agent_bots as bots
from integrations.services.slack_channel_mentions import roo_channel_targets
from integrations.services.slack_dm_mirror import (
    _history_message_author_allowed,
    _normalize_private_slack_event,
    _with_agent_author,
)
from integrations.services.slack_roo import ai_recipient_user_ids

MAEK = {
    "workspace_id": "TMLAI",
    "bot_user_id": "UMAEK",
    "bot_id": "BMAEK",
    "app_id": "ADOTS",
    "owner_user_id": 7,
    "name": "Maek",
}
ON = dict(
    SLACK_AGENT_BOTS_ENABLED=True,
    SLACK_AGENT_BOTS=[MAEK],
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)


def message(**changes):
    return {
        "type": "message",
        "channel": "DMAEK",
        "user": "UMAEK",
        "bot_id": "BMAEK",
        "ts": "1788800000.000001",
        "text": "Here is your update.",
        **changes,
    }


def normalize(event, workspace="TMLAI", allow_channels=False):
    return _normalize_private_slack_event(
        {"team_id": workspace, "event_id": "EvMAEK"}, event,
        allow_roo_channels=allow_channels,
    )


@override_settings(**ON)
class SlackAgentBotConfigurationTests(SimpleTestCase):
    def test_disabled_flag_hides_every_configured_bot(self):
        with override_settings(SLACK_AGENT_BOTS_ENABLED=False):
            self.assertEqual(bots.configured_bots(), ())
            self.assertIsNone(normalize(message()))

    def test_json_string_configuration_is_accepted(self):
        with override_settings(SLACK_AGENT_BOTS=json.dumps([MAEK])):
            self.assertEqual(bots.bot_for_user("TMLAI", "UMAEK").name, "Maek")

    def test_malformed_entries_are_ignored_never_guessed(self):
        bad = [
            {**MAEK, "workspace_id": "mlai"},
            {**MAEK, "bot_user_id": "Maek"},
            {**MAEK, "owner_user_id": "7"},
            {**MAEK, "owner_user_id": True},
            {**MAEK, "name": "<@here>"},
            {**MAEK, "kind": "other"},
            {**MAEK, "bot_id": "XNOPE"},
            "not-a-dict",
        ]
        with override_settings(SLACK_AGENT_BOTS=bad):
            self.assertEqual(bots.configured_bots(), ())
        with override_settings(SLACK_AGENT_BOTS="{not json"):
            self.assertEqual(bots.configured_bots(), ())

    def test_owner_payload_lists_only_the_callers_bots(self):
        self.assertEqual(
            bots.owner_payload(7, "TMLAI"),
            [{"slack_user_id": "UMAEK", "slack_workspace_id": "TMLAI", "name": "Maek",
              "kind": "openai_dot"}],
        )
        self.assertEqual(bots.owner_payload(8, "TMLAI"), [])
        self.assertEqual(bots.owner_payload(7, "TOTHER"), [])


@override_settings(**ON)
class SlackAgentBotIdentityTests(SimpleTestCase):
    def profile(self, **changes):
        return {
            "id": "UMAEK", "team_id": "TMLAI", "is_bot": True,
            "profile": {"api_app_id": "ADOTS", "bot_id": "BMAEK"}, **changes,
        }

    def test_only_the_owner_can_open_a_dm_with_a_fresh_matching_profile(self):
        self.assertTrue(bots.is_agent_bot_user(self.profile(), workspace_id="TMLAI", owner_user_id=7))
        self.assertFalse(bots.is_agent_bot_user(self.profile(), workspace_id="TMLAI", owner_user_id=8))
        for changes in [
            {"id": "UOTHER", "name": "Maek"},
            {"team_id": "TOTHER"},
            {"is_bot": False},
            {"deleted": True},
            {"is_stranger": True},
            {"profile": {"api_app_id": "AOTHER", "bot_id": "BMAEK"}},
            {"profile": {"api_app_id": "ADOTS", "bot_id": "BOTHER"}},
        ]:
            with self.subTest(changes=changes):
                self.assertFalse(bots.is_agent_bot_user(
                    self.profile(**changes), workspace_id="TMLAI", owner_user_id=7))


@override_settings(**ON)
class SlackAgentBotMirrorTests(SimpleTestCase):
    def test_dm_reply_is_mirrored_with_its_bot_author(self):
        result = normalize(message(thread_ts="1788799900.000001"))
        self.assertEqual(result["source_author_id"], "UMAEK")
        self.assertEqual(result["text"], "Here is your update.")
        self.assertEqual(result["metadata"]["thread_ts"], "1788799900.000001")

    def test_userless_reply_is_attributed_only_by_exact_bot_id(self):
        result = normalize(message(user=None, subtype="bot_message"))
        self.assertEqual(result["source_author_id"], "UMAEK")
        self.assertIsNone(normalize(message(user=None, bot_id="BOTHER", subtype="bot_message")))

    def test_other_bots_workspaces_and_unallowed_channels_are_excluded(self):
        self.assertIsNone(normalize(message(user="UBOT", bot_id="BBOT")))
        self.assertIsNone(normalize(message(bot_id="BOTHER")))
        self.assertIsNone(normalize(message(), workspace="TOTHER"))
        self.assertIsNone(normalize(message(channel="GCOMMITTEE")))
        self.assertEqual(
            normalize(message(channel="GCOMMITTEE"), allow_channels=True)["source_author_id"],
            "UMAEK",
        )

    def test_streamed_edits_update_the_same_message(self):
        event = {
            "type": "message", "channel": "DMAEK", "subtype": "message_changed",
            "event_ts": "1788800001.000001",
            "message": message(user=None, text="Here is your full update."),
        }
        result = normalize(event)
        self.assertEqual(result["source_author_id"], "UMAEK")
        self.assertEqual(result["metadata"]["target_source_message_id"], "1788800000.000001")

    def test_owner_messages_tagged_with_app_ids_stay_the_owners(self):
        result = normalize(message(user="UOWNER", bot_id=None, app_id="AMLAI"))
        self.assertEqual(result["source_author_id"], "UOWNER")

    def test_history_requires_exact_owner_and_bot_membership(self):
        conversation = SimpleNamespace(
            slack_workspace_id="TMLAI",
            slack_conversation_id="DMAEK",
            participant_slack_ids=["UOWNER", "UMAEK"],
            grant=SimpleNamespace(slack_user_id="UOWNER"),
        )
        self.assertTrue(_history_message_author_allowed(conversation, message()))
        userless = _with_agent_author(conversation, message(user=None))
        self.assertEqual(userless["user"], "UMAEK")
        self.assertTrue(_history_message_author_allowed(conversation, userless))
        conversation.participant_slack_ids = ["UOWNER", "UMAEK", "UOTHER"]
        self.assertFalse(_history_message_author_allowed(conversation, message()))

    def test_ai_consent_covers_roo_and_configured_bots(self):
        self.assertEqual(ai_recipient_user_ids("TMLAI"), {"UROO", "UMAEK"})
        self.assertEqual(ai_recipient_user_ids("TOTHER"), set())


@override_settings(**ON)
class SlackAgentBotChannelTargetTests(SimpleTestCase):
    def test_bot_member_of_private_channel_is_a_mention_target(self):
        conversation = conversation_fixture()
        conversation.participant_slack_ids = ["UONE", "UROO", "UMAEK"]
        targets = roo_channel_targets(conversation)
        self.assertEqual([t["slack_user_id"] for t in targets], ["UROO", "UMAEK"])
        self.assertEqual(targets[1]["display_name"], "Maek")
        self.assertTrue(targets[1]["is_bot"])

    def test_bot_that_is_not_a_member_is_not_offered(self):
        conversation = conversation_fixture()
        self.assertEqual([t["slack_user_id"] for t in roo_channel_targets(conversation)], ["UROO"])
        with override_settings(SLACK_AGENT_BOTS_ENABLED=False):
            conversation.participant_slack_ids = ["UONE", "UMAEK"]
            self.assertEqual(roo_channel_targets(conversation), [])
