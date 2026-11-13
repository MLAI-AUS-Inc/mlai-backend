"""An owner's own OpenAI dot is accepted only in that owner's Slack IM."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from integrations.services import slack_personal_dot as dots
from integrations.services.slack_dm_mirror import (
    _history_message_author_allowed,
    _is_eligible_slack_user,
    _normalize_private_slack_event,
    _personal_dot_conversations,
)

ENABLED = {
    "COMMUNITY_CHAT_RELAY_URL": "wss://chat.mlai.au",
    "COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID": "TMLAI",
    "COMMUNITY_CHAT_ROO_SLACK_USER_ID": "UROO",
    "COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED": True,
    "COMMUNITY_CHAT_DOT_SLACK_APP_IDS": "ACHATGPT",
}


def grant(owner="UOWNER", dot="UMAEK", app="ACHATGPT", workspace="TMLAI"):
    metadata = {}
    if dot:
        metadata[dots.PERSONAL_DOT_KEY] = {
            "workspace_id": workspace,
            "owner_slack_user_id": owner,
            "slack_user_id": dot,
            "app_id": app,
            "display_name": "Maek",
        }
    return SimpleNamespace(
        pk=1,
        slack_workspace_id="TMLAI",
        slack_user_id=owner,
        connection=SimpleNamespace(provider_metadata=metadata),
    )


@override_settings(**ENABLED)
class PersonalDotSlackTests(SimpleTestCase):
    def profile(self, **changes):
        base = {
            "id": "UMAEK",
            "team_id": "TMLAI",
            "is_bot": True,
            "name": "maek",
            "profile": {"api_app_id": "ACHATGPT", "display_name": "Maek"},
        }
        base.update(changes)
        return base

    def message(self, **changes):
        return {
            "type": "message",
            "channel": "DMAEK",
            "user": "UMAEK",
            "bot_id": "BMAEK",
            "app_id": "ACHATGPT",
            "ts": "1788800000.000001",
            "text": "Your next meeting is at 3pm.",
            **changes,
        }

    def normalize(self, message, *, registered=True):
        with mock.patch.object(
            dots, "workspace_personal_dot_author", return_value=registered
        ):
            return _normalize_private_slack_event(
                {"team_id": "TMLAI", "event_id": "EvMAEK"}, message
            )

    def test_feature_needs_switch_and_allowed_app(self):
        self.assertTrue(dots.personal_dot_enabled())
        self.assertEqual(dots.personal_dot_target(grant()), ("TMLAI", "UMAEK"))
        for overrides in (
            {"COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED": False},
            {"COMMUNITY_CHAT_DOT_SLACK_APP_IDS": ""},
            {"COMMUNITY_CHAT_DOT_SLACK_APP_IDS": "not-an-app"},
        ):
            with self.subTest(overrides=overrides), override_settings(**overrides):
                self.assertFalse(dots.personal_dot_enabled())
                self.assertIsNone(dots.personal_dot_target(grant()))

    def test_registration_must_match_owner_workspace_and_app(self):
        for registered in (
            grant(app="AOTHER"),
            grant(workspace="TOTHER"),
            grant(dot=None),
            grant(dot="not-a-user"),
        ):
            with self.subTest(registered=registered.connection.provider_metadata):
                self.assertIsNone(dots.personal_dot_target(registered))
        moved = grant()
        moved.slack_user_id = "UNEWOWNER"
        self.assertIsNone(dots.personal_dot_target(moved))

    def test_only_fetched_bot_of_allowed_app_is_a_dot(self):
        self.assertTrue(dots.is_personal_dot_user(self.profile(), grant=grant()))
        for changes in (
            {"id": "UOTHER"},
            {"team_id": "TOTHER"},
            {"is_bot": False},
            {"deleted": True},
            {"is_stranger": True},
            {"profile": {"api_app_id": "AOTHER"}},
            {"profile": {}},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(
                    dots.is_personal_dot_user(self.profile(**changes), grant=grant())
                )
        # Another owner's registration does not open this owner's dot.
        self.assertFalse(
            dots.is_personal_dot_user(self.profile(), grant=grant(dot="UTHEIRS"))
        )
        # Roo is never a personal dot, and the people picker stays human-only.
        self.assertFalse(
            dots.is_dot_bot_profile(self.profile(id="UROO"), workspace_id="TMLAI")
        )
        self.assertFalse(
            _is_eligible_slack_user(
                self.profile(), workspace_id="TMLAI", owner_slack_user_id="UOWNER"
            )
        )

    def test_live_dot_reply_is_marked_for_owner_routing(self):
        result = self.normalize(self.message(thread_ts="1788799900.000001"))
        self.assertEqual(result["source_author_id"], "UMAEK")
        self.assertEqual(result["personal_dot_author"], "UMAEK")
        self.assertEqual(result["metadata"]["thread_ts"], "1788799900.000001")

    def test_unregistered_bots_and_shared_conversations_are_dropped(self):
        self.assertIsNone(self.normalize(self.message(), registered=False))
        for channel in ("GROOM", "CROOM"):
            with self.subTest(channel=channel):
                self.assertIsNone(self.normalize(self.message(channel=channel)))

    def test_dot_edits_and_deletions_are_preserved(self):
        for subtype, key in (
            ("message_changed", "message"),
            ("message_deleted", "previous_message"),
        ):
            event = {
                "type": "message",
                "channel": "DMAEK",
                "subtype": subtype,
                "event_ts": "1788800001.000001",
                key: self.message(),
            }
            with self.subTest(subtype=subtype):
                result = self.normalize(event)
                self.assertEqual(result["source_author_id"], "UMAEK")
                self.assertEqual(
                    result["metadata"]["target_source_message_id"],
                    "1788800000.000001",
                )

    def test_routing_keeps_only_the_registering_owner_im(self):
        mine = SimpleNamespace(grant=grant(), participant_slack_ids=["UOWNER", "UMAEK"])
        theirs = SimpleNamespace(
            grant=grant(owner="UTHEM", dot="UOTHERDOT"),
            participant_slack_ids=["UTHEM", "UMAEK"],
        )
        crowded = SimpleNamespace(
            grant=grant(), participant_slack_ids=["UOWNER", "UMAEK", "UGUEST"]
        )
        self.assertEqual(
            _personal_dot_conversations([mine, theirs, crowded], "TMLAI", "UMAEK"),
            [mine],
        )
        with override_settings(COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED=False):
            self.assertEqual(_personal_dot_conversations([mine], "TMLAI", "UMAEK"), [])

    def test_history_requires_owner_and_their_dot_only(self):
        conversation = SimpleNamespace(
            slack_workspace_id="TMLAI",
            slack_conversation_id="DMAEK",
            slack_conversation_type="im",
            participant_slack_ids=["UOWNER", "UMAEK"],
            grant=grant(),
        )
        self.assertTrue(_history_message_author_allowed(conversation, self.message()))
        self.assertFalse(
            _history_message_author_allowed(conversation, self.message(user="UOTHER"))
        )
        for participants in (["UMAEK", "UOTHER"], ["UOWNER", "UMAEK", "UOTHER"], []):
            with self.subTest(participants=participants):
                conversation.participant_slack_ids = participants
                self.assertFalse(
                    _history_message_author_allowed(conversation, self.message())
                )
        conversation.participant_slack_ids = ["UOWNER", "UMAEK"]
        conversation.grant = grant(dot="UTHEIRS")
        self.assertFalse(_history_message_author_allowed(conversation, self.message()))


@override_settings(**ENABLED)
class RegisterPersonalDotTests(SimpleTestCase):
    def register(self, user, slack_user_id="UMAEK", owner=None):
        owner = owner or grant(dot=None)
        owner.refresh_from_db = lambda: None
        from integrations.services import slack_dm_mirror as mirror

        writes = []

        def write(target, record):
            writes.append(record)
            target.connection.provider_metadata[dots.PERSONAL_DOT_KEY] = record

        with mock.patch.object(
            mirror, "_capture_slack_grant_api_authority", return_value=object()
        ), mock.patch.object(
            mirror, "_call_slack_with_grant_authority", return_value={"user": user}
        ) as call, mock.patch.object(dots, "_write", side_effect=write):
            result = dots.register_personal_dot(owner, slack_user_id=slack_user_id)
        return result, writes, call

    def bot(self, **changes):
        return {
            "id": "UMAEK",
            "team_id": "TMLAI",
            "is_bot": True,
            "name": "maek",
            "profile": {"api_app_id": "ACHATGPT", "display_name": "Maek", "image_192": "https://a.slack-edge.com/m.png"},
            **changes,
        }

    def test_registers_a_validated_dot(self):
        result, writes, call = self.register(self.bot())
        self.assertEqual(call.call_args.args[1], "users_info")
        self.assertEqual(call.call_args.kwargs["user"], "UMAEK")
        self.assertEqual(writes[0]["app_id"], "ACHATGPT")
        self.assertEqual(writes[0]["owner_slack_user_id"], "UOWNER")
        self.assertEqual(result["slack_user_id"], "UMAEK")
        self.assertEqual(result["display_name"], "Maek")
        self.assertNotIn("token", str(result).lower())

    def test_rejects_people_other_apps_self_and_roo(self):
        for user in (self.bot(is_bot=False), self.bot(profile={"api_app_id": "AOTHER"})):
            with self.subTest(user=user), self.assertRaises(dots.PersonalDotError):
                self.register(user)
        for slack_user_id in ("UOWNER", "UROO", "maek", ""):
            with self.subTest(slack_user_id=slack_user_id), self.assertRaises(
                dots.PersonalDotError
            ):
                self.register(self.bot(id=slack_user_id), slack_user_id=slack_user_id)

    def test_disabled_switch_rejects_registration(self):
        with override_settings(COMMUNITY_CHAT_PERSONAL_DOT_SLACK_ENABLED=False):
            with self.assertRaises(dots.PersonalDotError) as raised:
                self.register(self.bot())
        self.assertEqual(raised.exception.code, "personal_dot_disabled")
