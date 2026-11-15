"""Slack-style personal Dot accounts: handle and About panel, without a database."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from agent_runtime import native_installations as ni

COMMUNITY = "11111111-1111-4111-8111-111111111111"
CONFIG = (COMMUNITY, {"relay_url": "https://chat.mlai.au"})


def dot(name="Maek", events=None, first="Sam", last="Donegan"):
    return SimpleNamespace(pk=uuid4(), name=name, app_id="openai-dot", community_id=COMMUNITY,
                           owner=SimpleNamespace(first_name=first, last_name=last), profile_events=events or [])


def published(handle):
    return [{"kind": 0, "content": json.dumps({"nip05": handle})}]


@patch.object(ni.native_sources, "community_config", return_value=CONFIG)
class HandleTests(SimpleTestCase):
    def test_owner_then_dot_like_slack(self, _):
        self.assertEqual(ni.dot_handle(dot(), taken={None}), "sam-donegan-maek@chat.mlai.au")

    def test_symbols_and_case_are_folded(self, _):
        self.assertEqual(ni.dot_handle(dot(name="Dr. Maek!", first="Sam ", last="O'Neil"), taken={None}),
                         "sam-o-neil-dr-maek@chat.mlai.au")

    def test_collision_gets_a_number(self, _):
        taken = {"sam-donegan-maek@chat.mlai.au", "sam-donegan-maek-2@chat.mlai.au"}
        self.assertEqual(ni.dot_handle(dot(), taken=taken), "sam-donegan-maek-3@chat.mlai.au")

    def test_published_handle_never_changes(self, _):
        self.assertEqual(ni.dot_handle(dot(name="Renamed", events=published("sam-donegan-maek@chat.mlai.au")),
                                       taken={None}), "sam-donegan-maek@chat.mlai.au")

    def test_nameless_owner_still_gets_two_words(self, _):
        self.assertEqual(ni.dot_handle(dot(name="", first="", last=""), taken={None}), "member-dot@chat.mlai.au")

    def test_other_dots_in_the_community_are_checked(self, _):
        query = MagicMock()
        query.exclude.return_value.only.return_value = [SimpleNamespace(profile_events=published(
            "sam-donegan-maek@chat.mlai.au"))]
        with patch.object(ni.AgentInstallation.objects, "filter", return_value=query):
            self.assertEqual(ni.dot_handle(dot()), "sam-donegan-maek-2@chat.mlai.au")


class ProfileTests(SimpleTestCase):
    def test_profile_carries_handle_and_about(self):
        profile = ni._profile(dot(), None, handle="sam-donegan-maek@chat.mlai.au")
        self.assertEqual(profile["name"], "sam-donegan-maek")
        self.assertEqual(profile["display_name"], "Maek")
        self.assertEqual(profile["nip05"], "sam-donegan-maek@chat.mlai.au")
        self.assertTrue(profile["bot"])
        self.assertIn("Sam Donegan's personal OpenAI dot", profile["about"])
        self.assertLessEqual(len(profile["about"]), 1000)

    def test_without_handle_profile_is_unchanged(self):
        self.assertEqual(ni._profile(dot(), None), {"name": "Maek", "display_name": "Maek", "bot": True})

    def test_flag_off_publishes_no_new_handle(self):
        self.assertIsNone(ni.profile_handle(dot()))
        self.assertEqual(ni.profile_handle(dot(events=published("sam-donegan-maek@chat.mlai.au"))),
                         "sam-donegan-maek@chat.mlai.au")

    @override_settings(AGENT_RUNTIME_DOT_ACCOUNTS_ENABLED=True)
    def test_flag_on_assigns_a_handle(self):
        with patch.object(ni, "dot_handle", return_value="sam-donegan-maek@chat.mlai.au"):
            self.assertEqual(ni.profile_handle(dot()), "sam-donegan-maek@chat.mlai.au")
        self.assertIsNone(ni.profile_handle(SimpleNamespace(app_id="mlai-community", profile_events=[])))

    def test_malformed_published_handle_is_ignored(self):
        self.assertIsNone(ni.current_handle(dot(events=published("Maek"))))


@patch.object(ni.native_sources, "community_config", return_value=CONFIG)
class StoredHandleTests(SimpleTestCase):
    def test_stored_handle_wins(self, _):
        value = dot()
        value.handle = "sam-donegan-maek@chat.mlai.au"
        self.assertEqual(ni.dot_handle(value, taken={None}), "sam-donegan-maek@chat.mlai.au")

    @override_settings(AGENT_RUNTIME_DOT_ACCOUNTS_ENABLED=True)
    def test_profile_handle_records_handle_and_about_for_saving(self, _):
        value = dot()
        with patch.object(ni, "dot_handle", return_value="sam-donegan-maek@chat.mlai.au"):
            self.assertEqual(ni.profile_handle(value), "sam-donegan-maek@chat.mlai.au")
        self.assertEqual(value.handle, "sam-donegan-maek@chat.mlai.au")
        self.assertIn("personal OpenAI dot", value.about)
