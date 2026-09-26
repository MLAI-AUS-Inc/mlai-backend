from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from slack_sdk.errors import SlackApiError

from integrations.services import slack_mention_directory as directory
from integrations.services import slack_dm_mirror as mirror
from integrations.services import slack_mentions as mentions
from integrations.services import slack_workspace_users as snapshots
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

    def test_snapshot_cursor_keeps_original_order_during_refresh(self):
        self.client.users_list.return_value = {
            "members": [
                {"id": "UBOB", "team_id": "TMLAI", "name": "Bob Person"},
                {"id": "UCARL", "team_id": "TMLAI", "name": "Carl Person"},
            ]
        }
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        first = self.search(query="person", limit=1)
        self.assertEqual(first["users"][0]["slack_user_id"], "UBOB")
        _, scope = snapshots.configured_scope()
        snapshot_key = snapshots.cache_key(scope, "snapshot")
        saved = cache.get(snapshot_key)
        saved["completed_at"] = 0
        cache.set(snapshot_key, saved, timeout=snapshots.SNAPSHOT_TTL_SECONDS)
        self.client.users_list.return_value = {
            "members": [
                {"id": "UAARDVARK", "team_id": "TMLAI", "name": "Aardvark Person"},
                {"id": "UBOB", "team_id": "TMLAI", "name": "Bob Person"},
                {"id": "UCARL", "team_id": "TMLAI", "name": "Carl Person"},
            ]
        }
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        second = self.search(query="person", cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["users"][0]["slack_user_id"], "UCARL")
        self.assertEqual(self.search(query="person", limit=1)["users"][0]["slack_user_id"], "UAARDVARK")

    def test_owner_private_search_can_use_public_names_with_owner_validation(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.client.users_list.reset_mock()
        validate = MagicMock()
        read = MagicMock(side_effect=AssertionError("unexpected Slack read"))
        result = mentions._search_directory(
            workspace="TMLAI", channel="DPRIVATE",
            private=SimpleNamespace(
                participant_slack_ids=["UALICE"], participant_profiles={}
            ),
            query="alice", cursor="", limit=7, read=read,
            cache_key=lambda category, value: f"private:{category}:{value}",
            validate=validate,
        )
        self.assertEqual(result["users"][0]["slack_user_id"], "UALICE")
        self.assertTrue(result["users"][0]["is_member"])
        validate.assert_called_once()
        read.assert_not_called()

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

    def test_background_warm_searches_all_pages_without_live_directory_calls(self):
        self.client.users_list.side_effect = [
            {
                "members": [
                    {"id": "UBOB", "team_id": "TMLAI", "name": "Bob Person"},
                    {"id": "UCARL", "team_id": "TMLAI", "name": "Carl Person"},
                ],
                "response_metadata": {"next_cursor": "page-2"},
            },
            {
                "members": [
                    {
                        "id": "UALICE", "team_id": "TMLAI",
                        "profile": {
                            "display_name": "Alice Person",
                            "email": "secret@example.test",
                        },
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.assertIsNone(snapshots.cached_workspace_users("TMLAI"))
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.assertEqual(len(snapshots.cached_workspace_users("TMLAI")), 3)
        self.client.users_list.reset_mock()

        first = self.search(query="person", limit=1)
        self.assertEqual(first["users"][0]["slack_user_id"], "UALICE")
        self.assertTrue(first["users"][0]["is_member"])
        self.assertTrue(first["next_cursor"])
        second = self.search(query="person", cursor=first["next_cursor"], limit=1)
        third = self.search(query="person", cursor=second["next_cursor"], limit=1)
        self.assertEqual(
            [second["users"][0]["slack_user_id"], third["users"][0]["slack_user_id"]],
            ["UBOB", "UCARL"],
        )
        self.assertEqual(third["next_cursor"], "")
        self.assertNotIn("secret@example.test", str(snapshots.cached_workspace_users("TMLAI")))
        self.client.users_list.assert_not_called()

    def test_warm_deferral_keeps_last_complete_snapshot_and_avoids_retry_spin(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        workspace, scope = snapshots.configured_scope()
        snapshot_key = snapshots.cache_key(scope, "snapshot")
        saved = cache.get(snapshot_key)
        saved["completed_at"] = 0
        cache.set(snapshot_key, saved, timeout=snapshots.SNAPSHOT_TTL_SECONDS)
        self.client.users_list.side_effect = BudgetDeferred(3)
        self.assertEqual(snapshots.warm_workspace_directory_once(), 0)
        calls = self.client.users_list.call_count
        self.assertEqual(snapshots.warm_workspace_directory_once(), 0)
        self.assertEqual(self.client.users_list.call_count, calls)
        self.assertEqual(len(snapshots.cached_workspace_users(workspace)), 2)

    def test_snapshot_is_bound_to_configured_credential_and_workspace(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.assertIsNone(snapshots.cached_workspace_users("TOTHER"))
        with override_settings(SLACK_BRIDGE_BOT_TOKEN="another-installation"):
            self.assertIsNone(snapshots.cached_workspace_users("TMLAI"))
        self.client.auth_test.return_value = {"team_id": "TOTHER"}
        cache.clear()
        with self.assertRaisesMessage(ValueError, "slack_directory_workspace_mismatch"):
            snapshots.warm_workspace_directory_once()
        self.assertIsNone(snapshots.cached_workspace_users("TMLAI"))
