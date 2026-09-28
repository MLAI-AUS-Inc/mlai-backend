"""Database-free authorization and transport checks for Roo action handoff."""

from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.core.cache import cache
from rest_framework.permissions import IsAuthenticated

from community_chat.slack_action_views import SlackMessageActionView
from integrations.services.slack_dm_mirror import SlackDmMirrorError


@override_settings(
    ROO_SERVICE_URL="https://roo.example",
    ROO_INTERNAL_MENTION_API_KEY="synthetic",
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
class SlackMessageActionsTests(SimpleTestCase):
    def setUp(self):
        self.data = {
            "workspace_id": "TMLAI",
            "channel_id": "DROO",
            "message_ts": "1790000000.000001",
            "thread_ts": "1790000000.000001",
            "action_id": "confirm_topic_btn_0",
            "action_hash": "a" * 64,
        }
        self.request = SimpleNamespace(user=SimpleNamespace(pk=42))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(cache.clear)
        self.prefix = "community_chat.slack_action_views."

        def mock(name, **kwargs):
            return self.stack.enter_context(patch(self.prefix + name, **kwargs))

        self.grant = mock(
            "active_grant_for_user",
            return_value=SimpleNamespace(
                slack_workspace_id="TMLAI", slack_user_id="UOWNER"
            ),
        )
        self.private = mock("SlackDmMirrorConversation.objects.filter")
        self.private.return_value.first.return_value = object()
        self.public = mock("CommunityBridgeChannel.objects.filter")
        self.public.return_value.exists.return_value = False
        mock(
            "_capture_slack_grant_api_authority",
            return_value=SimpleNamespace(slack_user_id="UOWNER"),
        )
        self.authority = mock("_lock_slack_grant_api_authority")
        mock("transaction.atomic", side_effect=lambda: nullcontext())
        self.post = mock(
            "requests.post",
            return_value=Mock(
                status_code=200,
                json=lambda: {
                    "message": {
                        "user": "UROO",
                        "ts": self.data["message_ts"],
                        "text": "Done",
                    }
                },
            ),
        )

    def invoke(self, **overrides):
        return SlackMessageActionView()._handoff(
            self.request, {**self.data, **overrides}, perform=True
        )

    def test_authenticated_identity_overrides_client_identity(self):
        response = self.invoke(user_id="UATTACKER", value="different-job")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post.call_args.kwargs["json"]["user_id"], "UOWNER")
        self.assertNotIn("value", self.post.call_args.kwargs["json"])
        self.authority.assert_called_once()
        self.assertIn(IsAuthenticated, SlackMessageActionView.permission_classes)

    def test_disconnected_and_other_workspace_never_dispatch(self):
        self.grant.side_effect = SlackDmMirrorError("Disconnected")
        self.assertEqual(self.invoke().status_code, 403)
        self.grant.side_effect = None
        self.assertEqual(self.invoke(workspace_id="TOTHER").status_code, 403)
        self.post.assert_not_called()

    def test_conversation_must_belong_to_account_or_enabled_public_mapping(self):
        self.private.return_value.first.return_value = None
        self.assertEqual(self.invoke().status_code, 403)
        self.post.assert_not_called()

    def test_malformed_or_unsupported_actions_do_not_dispatch(self):
        for changes in (
            {"action_id": "admin_delete"},
            {"action_hash": "bad"},
            {"channel_id": "https://evil.example"},
        ):
            self.assertEqual(self.invoke(**changes).status_code, 400)
        self.post.assert_not_called()

    def test_get_is_read_only_and_still_authorized(self):
        response = SlackMessageActionView()._handoff(
            self.request, self.data, perform=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.post.call_args.kwargs["json"]["perform"])
        self.assertEqual(response.data["content"], "Done")

    def test_changed_consent_prevents_dispatch(self):
        self.authority.side_effect = SlackDmMirrorError("Revoked")
        self.assertEqual(self.invoke().status_code, 403)
        self.post.assert_not_called()

    def test_timeout_blocks_duplicate_choice_until_result_can_be_checked(self):
        import requests

        self.post.side_effect = requests.Timeout()
        self.assertEqual(self.invoke().status_code, 503)
        self.assertEqual(self.invoke().status_code, 409)
        self.assertEqual(self.post.call_count, 1)
