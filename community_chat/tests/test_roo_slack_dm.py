"""Public Roo is the only bot accepted in the owner's private Slack IM."""

from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from integrations.services.slack_dm_mirror import (
    _history_message_author_allowed,
    _is_eligible_slack_user,
    _normalize_private_slack_event,
)
from integrations.services.slack_roo import is_public_roo_user, public_roo_target


@override_settings(
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
class PublicRooSlackTests(SimpleTestCase):
    def profile(self, **changes):
        return {"id": "UROO", "team_id": "TMLAI", "is_bot": True, **changes}

    def message(self, **changes):
        return {
            "type": "message",
            "channel": "DROO",
            "user": "UROO",
            "bot_id": "BROO",
            "subtype": "bot_message",
            "ts": "1788800000.000001",
            "text": "How can I help?",
            **changes,
        }

    def normalize(self, message, workspace="TMLAI"):
        return _normalize_private_slack_event(
            {"team_id": workspace, "event_id": "EvROO"},
            message,
        )

    def test_target_is_disabled_outside_mlai_or_without_valid_configuration(self):
        self.assertEqual(public_roo_target(), ("TMLAI", "UROO"))
        for setting, value in [
            ("COMMUNITY_CHAT_RELAY_URL", "wss://other.example.test"),
            ("COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID", ""),
            ("COMMUNITY_CHAT_ROO_SLACK_USER_ID", "Roo"),
        ]:
            with self.subTest(setting=setting), override_settings(**{setting: value}):
                self.assertIsNone(public_roo_target())
                self.assertIsNone(self.normalize(self.message()))

    def test_only_fetched_first_party_bot_identity_is_allowed(self):
        self.assertTrue(is_public_roo_user(self.profile(), workspace_id="TMLAI"))
        for changes in [
            {"id": "UOTHER", "name": "Roo"},
            {"team_id": "TOTHER"},
            {"is_bot": False},
            {"deleted": True},
            {"is_stranger": True},
        ]:
            with self.subTest(changes=changes):
                self.assertFalse(
                    is_public_roo_user(self.profile(**changes), workspace_id="TMLAI")
                )
        self.assertFalse(is_public_roo_user(self.profile(), workspace_id="TOTHER"))
        # The general member picker stays human-only.
        self.assertFalse(
            _is_eligible_slack_user(
                self.profile(), workspace_id="TMLAI", owner_slack_user_id="UOWNER"
            )
        )

    def test_roo_live_reply_and_thread_metadata_survive_import(self):
        result = self.normalize(self.message(thread_ts="1788799900.000001"))
        self.assertEqual(result["source_author_id"], "UROO")
        self.assertEqual(result["text"], "How can I help?")
        self.assertEqual(result["metadata"]["thread_ts"], "1788799900.000001")

    def test_other_bots_workspaces_and_group_chats_are_excluded(self):
        for message in [
            self.message(user="UBOT"),
            self.message(channel="GROOM"),
            self.message(channel="CROOM"),
        ]:
            with self.subTest(message=message):
                self.assertIsNone(self.normalize(message))
        self.assertIsNone(self.normalize(self.message(), workspace="TOTHER"))

    def test_roo_edits_and_deletions_are_preserved_but_other_bots_are_not(self):
        for subtype, key in [
            ("message_changed", "message"),
            ("message_deleted", "previous_message"),
        ]:
            event = {
                "type": "message",
                "channel": "DROO",
                "subtype": subtype,
                "event_ts": "1788800001.000001",
                key: self.message(),
            }
            with self.subTest(subtype=subtype):
                result = self.normalize(event)
                self.assertEqual(result["source_author_id"], "UROO")
                self.assertEqual(
                    result["metadata"]["target_source_message_id"], "1788800000.000001"
                )
                event[key] = self.message(user="UOTHER", bot_id=None)
                self.assertIsNone(self.normalize(event))

    def test_history_requires_exact_owner_and_roo_membership(self):
        conversation = SimpleNamespace(
            slack_workspace_id="TMLAI",
            slack_conversation_id="DROO",
            participant_slack_ids=["UOWNER", "UROO"],
            grant=SimpleNamespace(slack_user_id="UOWNER"),
        )
        self.assertTrue(_history_message_author_allowed(conversation, self.message()))
        self.assertFalse(
            _history_message_author_allowed(conversation, self.message(user="UOTHER"))
        )
        for participants in [["UROO", "UOTHER"], ["UOWNER", "UROO", "UOTHER"], []]:
            conversation.participant_slack_ids = participants
            self.assertFalse(
                _history_message_author_allowed(conversation, self.message())
            )
