"""Coworking booking notice, with mocked Slack and bridge rows; no database.

Run: python scripts/test_without_database.py community_chat.tests.test_coworking_announcement_unit
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from community_chat import coworking_announcement as notice

CHANNELS = "integrations.models.CommunityBridgeChannel.objects"
LINKS = "integrations.models.CommunityBridgeIdentityLink.objects"
SEND = "integrations.services.slack.SlackService.send_message"


@override_settings(
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
    COMMUNITY_CHAT_COWORKING_ANNOUNCE_ENABLED=True,
)
class CoworkingAnnouncementTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=42, full_name="Isaak Example")
        self.booking = SimpleNamespace(pk="booking-1")
        self.channels = self.enterContext(patch(CHANNELS))
        self.channels.filter.return_value.values.return_value.first.return_value = {
            "slack_workspace_id": "TMLAI", "slack_channel_id": "CCOWORK",
        }
        self.links = self.enterContext(patch(LINKS))
        self.slack_id = self.links.filter.return_value.exclude.return_value.values_list.return_value.first
        self.slack_id.return_value = None
        self.send = self.enterContext(patch(SEND, return_value=(True, "1.2")))

    def test_chat_only_member_is_announced_by_name_without_mentioning_roo(self):
        self.assertTrue(notice.announce_booking(self.user, self.booking))
        channel, text = self.send.call_args.args
        self.assertEqual(channel, "CCOWORK")
        self.assertEqual(
            text,
            "Isaak Example is booked in for coworking today. "
            "Reminder: no food is permitted in the coworking space.",
        )
        self.assertNotIn("<@UROO>", text)
        query = self.channels.filter.call_args.kwargs
        self.assertEqual(query["slack_workspace_id"], "TMLAI")
        self.assertTrue(query["enabled"])

    def test_linked_member_is_mentioned(self):
        self.slack_id.return_value = "UISAAK"
        notice.announce_booking(self.user, self.booking)
        self.assertTrue(self.send.call_args.args[1].startswith("<@UISAAK> is booked in"))

    def test_name_cannot_inject_slack_markup(self):
        self.user.full_name = "<!channel> & <@UROO>"
        notice.announce_booking(self.user, self.booking)
        text = self.send.call_args.args[1]
        self.assertNotIn("<", text)
        self.assertNotIn("&", text)

    def test_nameless_member_gets_a_neutral_label(self):
        self.user.full_name = ""
        notice.announce_booking(self.user, self.booking)
        self.assertTrue(self.send.call_args.args[1].startswith("An MLAI member is booked in"))

    def test_failures_never_raise(self):
        self.send.side_effect = RuntimeError("network")
        self.assertFalse(notice.announce_booking(self.user, self.booking))
        self.send.side_effect = None
        self.send.return_value = (False, None)
        self.assertFalse(notice.announce_booking(self.user, self.booking))

    def test_missing_channel_posts_nothing(self):
        self.channels.filter.return_value.values.return_value.first.return_value = None
        self.assertFalse(notice.announce_booking(self.user, self.booking))
        self.send.assert_not_called()

    @override_settings(COMMUNITY_CHAT_COWORKING_ANNOUNCE_ENABLED=False)
    def test_kill_switch_posts_nothing(self):
        self.assertFalse(notice.announce_booking(self.user, self.booking))
        self.send.assert_not_called()
        self.channels.filter.assert_not_called()
