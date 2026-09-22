from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from slack_sdk.errors import SlackApiError

from integrations.services import slack_mention_directory as directory
from integrations.services import slack_dm_mirror as mirror
from integrations.services.message_sync.scheduler import BudgetDeferred


@override_settings(
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TMLAI",
    SLACK_BRIDGE_BOT_TOKEN="synthetic-directory-token",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class WorkspaceMentionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.user = SimpleNamespace(pk=1, is_active=True)
        self.client = MagicMock()
        self.client.auth_test.return_value = {"team_id": "TMLAI"}
        self.client.conversations_members.return_value = {"members": ["UALICE"]}
        self.client.users_list.return_value = {
            "members": [
                {
                    "id": "UALICE",
                    "team_id": "TMLAI",
                    "profile": {
                        "display_name": "Alice",
                        "email": "private@example.test",
                    },
                },
                {"id": "UBOT", "is_bot": True, "team_id": "TMLAI", "name": "Other app"},
                {"id": "UDELETED", "deleted": True},
                {"id": "UFOREIGN", "team_id": "TOTHER"},
            ]
        }
        patches = [
            patch.object(directory, "require_community_access"),
            patch.object(
                directory.SlackBridgeClient, "get_client", return_value=self.client
            ),
            patch.object(directory.SlackDmMirrorConversation, "objects"),
            patch.object(directory.CommunityBridgeChannel, "objects"),
            patch(
                "integrations.services.slack_mentions.CommunityBridgeIdentityLink.objects"
            ),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.mocks[
            2
        ].select_related.return_value.filter.return_value.first.return_value = None
        self.mocks[3].filter.return_value.first.return_value = SimpleNamespace(
            slack_channel_id="CPUBLIC"
        )
        self.mocks[4].filter.return_value.values_list.return_value = []

    def search(self, **kwargs):
        return directory.search_workspace_mentions(
            self.user, channel_id="11111111-1111-4111-8111-111111111111", **kwargs
        )

    def test_no_import_grant_can_find_roo_people_and_apps_without_emails(self):
        result = self.search()
        self.assertEqual(
            [u["slack_user_id"] for u in result["users"]], ["UROO", "UALICE", "UBOT"]
        )
        self.assertNotIn("private@example.test", str(result))
        self.assertTrue(result["users"][1]["is_member"])
        self.assertFalse(result["users"][0]["is_member"])

    def test_search_follows_an_empty_matching_page_to_later_people(self):
        self.client.users_list.side_effect = [
            {"members": [], "response_metadata": {"next_cursor": "second"}},
            {"members": [{"id": "ULATER", "team_id": "TMLAI", "name": "Later Person"}]},
        ]
        first = self.search(query="later")
        self.assertEqual(first["users"], [])
        self.assertTrue(first["next_cursor"])
        second = self.search(query="later", cursor=first["next_cursor"])
        self.assertEqual(second["users"][0]["slack_user_id"], "ULATER")

    def test_paused_private_import_never_uses_owner_token_or_claims_membership(self):
        self.mocks[
            2
        ].select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(
            status="paused"
        )
        self.mocks[3].filter.return_value.first.return_value = None
        result = self.search(query="roo")
        self.assertIsNone(result["users"][0]["is_member"])
        self.assertFalse(result["users"][0]["native_only"])
        self.client.conversations_members.assert_not_called()

    def test_membership_scope_failure_does_not_hide_directory_or_claim_empty_membership(
        self,
    ):
        self.client.conversations_members.side_effect = SlackApiError(
            "scope", {"error": "missing_scope"}
        )
        for _ in range(2):
            result = self.search(query="alice")
            self.assertIsNone(result["users"][0]["is_member"])
            self.assertFalse(result["membership_pending"])

    def test_wrong_bot_workspace_fails_closed(self):
        self.client.auth_test.return_value = {"team_id": "TOTHER"}
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            self.search()
        self.client.users_list.assert_not_called()

    def test_roo_remains_available_while_provider_budget_is_deferred(self):
        self.client.auth_test.side_effect = BudgetDeferred(3)
        result = self.search(query="roo")
        self.assertEqual(result["users"][0]["slack_user_id"], "UROO")
        self.assertEqual(result["retry_after_seconds"], 3)
        self.assertTrue(result["next_cursor"])

    def test_unapproved_members_cannot_read_directory(self):
        self.mocks[0].side_effect = PermissionError("approval required")
        with self.assertRaises(PermissionError):
            self.search()
        self.client.auth_test.assert_not_called()

    def test_active_private_import_still_uses_owner_boundary(self):
        grant = SimpleNamespace(status="active", revoked_at=None)
        self.mocks[
            2
        ].select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(
            status="live", grant=grant
        )
        with patch.object(
            directory, "search_mentions", return_value={"users": []}
        ) as private:
            self.search()
        self.assertIs(private.call_args.args[0], grant)
        self.client.auth_test.assert_not_called()
