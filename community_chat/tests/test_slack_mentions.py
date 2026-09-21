from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase, override_settings
from integrations.services import slack_mentions as mentions
from integrations.services.slack_channel_mentions import render_slack_mentions
from integrations.services.message_sync.scheduler import BudgetDeferred
from community_chat.tests import test_slack_dm_mirror as fixtures


@override_settings(
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
class SlackMentionDirectoryTests(TestCase):
    def setUp(self):
        cache.clear()
        fixtures.SlackDmMirrorOwnerTests.setUp(self)
        self.grant, self.conversation = (
            fixtures.SlackDmMirrorOwnerTests._live_conversation(self)
        )
        self.channel = str(self.conversation.mlai_channel_id)

    @patch("integrations.services.slack_mentions._read")
    def test_directory_includes_roo_humans_nonmembers_and_no_emails(self, read):
        read.return_value = {
            "members": [
                {
                    "id": "UTWO",
                    "team_id": "TMLAI",
                    "profile": {
                        "display_name": "Alice",
                        "email": "private@example.test",
                    },
                },
                {
                    "id": "UTHREE",
                    "team_id": "TMLAI",
                    "profile": {"display_name": "Bob"},
                },
                {"id": "UDELETED", "deleted": True},
                {"id": "UFOREIGN", "team_id": "TOTHER"},
            ]
        }
        result = mentions.search_mentions(self.grant, channel_id=self.channel)
        users = {user["slack_user_id"]: user for user in result["users"]}
        self.assertEqual(set(users), {"UROO", "UTWO", "UTHREE"})
        self.assertTrue(users["UTWO"]["is_member"])
        self.assertFalse(users["UROO"]["is_member"])
        self.assertFalse(users["UTHREE"]["is_member"])
        self.assertNotIn("private@example.test", str(result))
        # A different query reuses the sanitized page, independent of imported DMs.
        self.assertEqual(
            mentions.search_mentions(self.grant, channel_id=self.channel, query="bob")[
                "users"
            ][0]["slack_user_id"],
            "UTHREE",
        )
        self.assertEqual(read.call_count, 1)

    @patch("integrations.services.slack_mentions._read")
    def test_budget_wait_keeps_a_resumable_cursor_and_roo_visible(self, read):
        read.side_effect = BudgetDeferred(3)
        page = mentions.search_mentions(
            self.grant, channel_id=self.channel, query="roo"
        )
        self.assertEqual(page["users"][0]["slack_user_id"], "UROO")
        self.assertTrue(page["next_cursor"])
        self.assertEqual(page["retry_after_seconds"], 3)

    @patch("integrations.services.slack_mentions._read")
    def test_later_slack_pages_are_not_lost_when_earlier_pages_have_no_match(
        self, read
    ):
        read.side_effect = [
            {
                "members": [{"id": "UALICE", "name": "alice"}],
                "response_metadata": {"next_cursor": "next"},
            },
            {"members": [{"id": "UBOB", "name": "bob"}]},
        ]
        first = mentions.search_mentions(
            self.grant, channel_id=self.channel, query="bob"
        )
        self.assertEqual(first["users"], [])
        second = mentions.search_mentions(
            self.grant,
            channel_id=self.channel,
            query="bob",
            cursor=first["next_cursor"],
        )
        self.assertEqual(second["users"][0]["slack_user_id"], "UBOB")
        self.assertEqual(second["next_cursor"], "")

    @patch("integrations.services.slack_mentions._read")
    def test_native_channel_search_exposes_directory_without_claiming_membership(
        self, read
    ):
        import uuid

        read.return_value = {
            "members": [{"id": "UTWO", "team_id": "TMLAI", "name": "Alice"}]
        }
        result = mentions.search_mentions(
            self.grant, channel_id=str(uuid.uuid4()), query="alice"
        )
        self.assertTrue(result["users"][0]["native_only"])
        self.assertFalse(result["users"][0]["is_member"])
        self.assertEqual(read.call_count, 1)

    @patch("integrations.services.slack_mentions._read")
    def test_invitation_checks_owner_membership_and_only_then_calls_slack(self, read):
        self.conversation.slack_conversation_id = "CPRIVATE"
        self.conversation.save()
        from integrations.services.slack_chat_catalog import CATALOG_KEY

        self.grant.connection.provider_metadata[CATALOG_KEY] = {
            "CPRIVATE": {"kind": "private_channel"}
        }
        self.grant.connection.save(update_fields=["provider_metadata"])
        read.side_effect = [
            {"channel": {"id": "CPRIVATE", "is_member": True}},
            {"ok": True},
        ]
        self.assertEqual(
            mentions.invite_mentions(
                self.grant, channel_id=self.channel, user_ids=["UROO"]
            ),
            {"invited": ["UROO"]},
        )
        self.assertEqual(read.call_args.args[1], "conversations_invite")
        read.reset_mock()
        read.side_effect = [{"channel": {"id": "CPRIVATE", "is_member": False}}]
        with self.assertRaises(mentions.mirror.SlackDmMirrorAuthorizationError):
            mentions.invite_mentions(
                self.grant, channel_id=self.channel, user_ids=["UROO"]
            )
        self.assertEqual(read.call_count, 1)

    def test_other_owners_private_channels_are_not_directory_authority(self):
        self.grant.user = self.second
        self.grant.pk = -1
        with self.assertRaises(mentions.mirror.SlackDmMirrorAuthorizationError):
            mentions.channel_for_grant(self.grant, self.channel)

    @patch("integrations.services.slack_mentions._read")
    def test_dms_cannot_silently_gain_new_members_or_share_their_history(self, read):
        with self.assertRaisesMessage(
            mentions.mirror.SlackDmMirrorError, "new group DM"
        ):
            mentions.invite_mentions(
                self.grant, channel_id=self.channel, user_ids=["UROO"]
            )
        read.assert_not_called()


class ExplicitSlackMentionTests(SimpleTestCase):
    def test_mentions_preserve_selected_ids_and_exclude_code(self):
        self.assertEqual(
            render_slack_mentions(
                "Hi @Alice and `@Roo`",
                [
                    ["slack-mention", "UALICE", "Alice"],
                    ["slack-mention", "UROO", "Roo"],
                ],
            ),
            ("Hi <@UALICE> and `@Roo`", ["UALICE"]),
        )

    def test_foreign_deleted_and_special_users_are_excluded(self):
        for user in (
            {"id": "USLACKBOT"},
            {"id": "U123", "deleted": True},
            {"id": "U123", "team_id": "TOTHER"},
            {"id": "here"},
        ):
            self.assertFalse(mentions.eligible_user(user, "TMLAI"))
        self.assertTrue(
            mentions.eligible_user(
                {"id": "UROO", "is_bot": True, "team_id": "TMLAI"}, "TMLAI"
            )
        )


class ReactionPermissionTests(TestCase):
    def test_only_mirrored_events_require_public_sharing_permission(self):
        from integrations.models import (
            CommunityBridgeChannel,
            CommunityBridgeMessageLink,
        )
        from community_chat.reaction_permissions import reaction_requires_ai_consent

        channel = CommunityBridgeChannel.objects.create(
            slack_channel_id="CPUBLIC",
            slack_workspace_id="TMLAI",
            destination_platform="buzz",
            destination_channel_id="public-mirror",
        )
        CommunityBridgeMessageLink.objects.create(
            channel=channel,
            source_platform="slack",
            source_channel_id="CPUBLIC",
            source_message_id="123.456",
            destination_platform="buzz",
            destination_channel_id="public-mirror",
            destination_message_id="a" * 64,
        )
        self.assertTrue(reaction_requires_ai_consent("a" * 64))
        self.assertFalse(reaction_requires_ai_consent("b" * 64))
        self.assertFalse(reaction_requires_ai_consent("invalid"))
        with override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False):
            self.assertFalse(reaction_requires_ai_consent("a" * 64))
