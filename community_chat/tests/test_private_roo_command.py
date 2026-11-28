from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
import time

from django.test import SimpleTestCase, override_settings

from community_chat import roo_command as cmd


@override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True)
class PrivateCommandTests(SimpleTestCase):
    def setUp(self):
        self.profile = str(uuid4())
        self.actor = {
            "identity_source": "mlai_account",
            "user_profile_id": self.profile,
            "slack_user_id": "UONE",
        }
        self.data = {
            "text": "@Roo show my points",
            "tags": [["slack-mention", "UROO", "Roo"]],
            "request_id": f"{int(time.time()*1000)}:{uuid4()}",
        }
        self.request = SimpleNamespace(
            user=SimpleNamespace(pk=1, slack_id="UONE", community_chat_profile_id=self.profile),
            data=self.data,
            community_chat_public_key="a" * 64,
        )
        for name, value in (
            ("public_roo_target", ("TTEAM", "UROO")),
            ("public_roo_endpoint", ("https://roo.test/api/chat-command", "test")),
            ("verified_identity_for_buzz", self.actor),
            ("has_ai_consent", True),
        ):
            self.enterContext(patch.object(cmd, name, return_value=value))
        self.post = self.enterContext(
            patch.object(
                cmd.requests,
                "post",
                return_value=Mock(status_code=200, json=lambda: {"message": "Your result"}),
            )
        )

    def test_dispatch_discloses_only_submitted_command_and_verified_identity(self):
        self.assertEqual(cmd.RooCommandView().post(self.request).data, {"message": "Your result"})
        sent = self.post.call_args.kwargs["json"]
        self.assertEqual(
            set(sent),
            {
                "version",
                "posting_mode",
                "source_event_id",
                "actor_profile_id",
                "recipient_targets",
                "source_labels",
                "user_id",
                "text",
                "workspace_id",
            },
        )
        self.assertEqual(sent["text"], "<@UROO> show my points")
        self.assertEqual(sent["user_id"], "UONE")
        cmd.RooCommandView().post(self.request)
        self.assertEqual(
            sent["source_event_id"], self.post.call_args.kwargs["json"]["source_event_id"]
        )

    def test_rejects_history_lookalikes_unselected_and_code_before_dispatch(self):
        for change in (
            {"channel_id": "DSECRET"},
            {"thread_ts": "1.2"},
            {"tags": []},
            {"text": "`@Roo`"},
            {"tags": [["slack-mention", "UFAKE", "Roo"]]},
            {"user_id": "UADMIN"},
            {"text": "@Roo give <@UOTHER> 100 points for helping"},
        ):
            with self.subTest(change=change):
                self.request.data = {**self.data, **change}
                self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 400)
        self.post.assert_not_called()

    def test_current_actor_and_consent_are_required(self):
        with patch.object(cmd, "has_ai_consent", return_value=False):
            self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 403)
        self.actor["user_profile_id"] = str(uuid4())
        self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 403)
        self.post.assert_not_called()

    def test_selected_dm_counterpart_is_bound_to_existing_wallet(self):
        other = str(uuid4())
        self.data.update(
            text="@Roo give @Andrew 4 points for helping",
            tags=self.data["tags"] + [["slack-mention", "UANDREW", "Andrew"]],
        )
        with patch.object(
            cmd.User.objects,
            "filter",
            return_value=[
                SimpleNamespace(
                    slack_id="UANDREW", community_chat_profile_id=other, full_name="Andrew"
                )
            ],
        ):
            self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 200)
        sent = self.post.call_args.kwargs["json"]
        self.assertEqual(sent["recipient_targets"][0]["profile_id"], other)
        self.assertEqual(sent["text"], "<@UROO> give <@UANDREW> 4 points for helping")

    def test_conflicting_account_and_device_actor_is_rejected(self):
        self.request.user.slack_id = "UOTHER"
        self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 403)
        self.post.assert_not_called()

    def test_native_person_selection_checks_original_and_current_device_owner(self):
        person = {"user_profile_id": str(uuid4()), "display_name": "Member"}
        key = "a" * 64
        self.data.update(
            text="@Roo give @Member 4 points for helping",
            tags=self.data["tags"] + [["mlai-mention", key, "Member"]],
        )
        identity = "integrations.services.community_bridge.identity."
        with patch(identity + "community_account_for_key_at", return_value=person) as past, patch(
            identity + "community_account_for_key", return_value=person
        ):
            self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 200)
        past.assert_called_once_with(
            slack_workspace_id="TTEAM",
            buzz_pubkey=key,
            source_created_at=int(self.data["request_id"][:13]) // 1000,
        )
        self.assertEqual(
            self.post.call_args.kwargs["json"]["recipient_targets"][0]["profile_id"],
            person["user_profile_id"],
        )
        self.post.reset_mock()
        with patch(identity + "community_account_for_key_at", return_value=person), patch(
            identity + "community_account_for_key", return_value=None
        ):
            self.assertEqual(cmd.RooCommandView().post(self.request).status_code, 400)
        self.post.assert_not_called()
