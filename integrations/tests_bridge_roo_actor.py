"""Database-free security and contract regressions for bridge author resolution."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_bridge_actor import CommunityBridgeRooActorView
from integrations.services.community_bridge.roo_actor import BridgeActorError, resolve_roo_actor

MODULE = "integrations.services.community_bridge.roo_actor"
CONTEXT = dict(workspace_id="TMLAI", channel_id="CMEMES", message_id="1790416956.199559",
               thread_ts="1790393997.818509", bridge_user_id="UBRIDGE")


@override_settings(
    SLACK_BRIDGE_BOT_USER_ID="UBRIDGE", COMMUNITY_CHAT_RELAY_URL="https://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI", COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
    COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True, ROO_API_KEY="synthetic-roo-key",
    INTERNAL_API_KEY="synthetic-internal-key",
)
class BridgeRooActorTests(SimpleTestCase):
    def setUp(self):
        self.link = SimpleNamespace(
            channel=SimpleNamespace(enabled=True, destination_channel_id="chat-channel"),
            source_channel_id="chat-channel", source_message_id="e6" * 32,
            source_author_id="bc" * 32, source_deleted_at=None, destination_deleted_at=None,
            destination_parent_message_id=CONTEXT["thread_ts"], destination_message_id=CONTEXT["message_id"],
            source_payload={
                "text": "this is gold @Roo please give @Beer Pilon 3 points for being a meme lord",
                "metadata": {"slack_mention_tags": [
                    ["slack-mention", "UROO", "Roo"], ["slack-mention", "UBEER", "Beer Pilon"],
                ]},
            },
        )
        self.identity = dict(identity_source="mlai_account", user_profile_id="profile-id",
                             slack_workspace_id="TMLAI", slack_user_id="USAM")
        self.manager = self.enterContext(patch(f"{MODULE}.CommunityBridgeMessageLink.objects"))
        self.manager.select_related.return_value.filter.return_value.first.return_value = self.link
        self.resolve = self.enterContext(patch(f"{MODULE}.verified_identity_for_buzz", return_value=self.identity))
        self.user_model = self.enterContext(patch(f"{MODULE}.get_user_model"))
        self.user_model.return_value.objects.filter.return_value.first.return_value = SimpleNamespace(pk=42)
        self.consent = self.enterContext(patch(f"{MODULE}.has_ai_consent", return_value=True))

    def assert_rejected(self, code, status=403, **context):
        with self.assertRaises(BridgeActorError) as caught:
            resolve_roo_actor(**{**CONTEXT, **context})
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def test_device_resolves_to_human_and_signed_mentions_render(self):
        result = resolve_roo_actor(**CONTEXT)
        self.assertEqual(result["user_id"], "USAM")
        self.assertEqual(result["source_event_id"], "e6" * 32)
        self.assertEqual(result["text"], "this is gold <@UROO> please give <@UBEER> 3 points for being a meme lord")
        self.resolve.assert_called_once_with(slack_workspace_id="TMLAI", buzz_pubkey="bc" * 32)
        query = self.manager.select_related.return_value.filter.call_args.kwargs
        self.assertEqual(query["destination_message_id"], CONTEXT["message_id"])
        self.assertEqual(query["channel__slack_workspace_id"], "TMLAI")
        self.assertEqual(query["destination_channel_id"], "CMEMES")
        self.assertEqual(query["source_platform"], "buzz")
        self.consent.assert_called_once_with(42)

    def test_unknown_bot_or_workspace_never_resolves_an_actor(self):
        self.assert_rejected("unknown_bridge_sender", 404, bridge_user_id="UOTHERBOT")
        self.assert_rejected("unknown_bridge_sender", 404, workspace_id="TOTHER")
        self.manager.select_related.assert_not_called()

    def test_missing_committed_delivery_is_retryable(self):
        self.manager.select_related.return_value.filter.return_value.first.return_value = None
        self.assert_rejected("bridge_delivery_pending", 409)
        self.resolve.assert_not_called()

    def test_deleted_or_disabled_or_moved_source_is_denied(self):
        for field, value in (("source_deleted_at", "deleted"), ("destination_deleted_at", "deleted"),
                             ("source_channel_id", "other-channel"), ("source_message_id", "not-an-event")):
            with self.subTest(field=field):
                original = getattr(self.link, field)
                setattr(self.link, field, value)
                self.assert_rejected("bridge_context_revoked")
                setattr(self.link, field, original)
        self.link.channel.enabled = False
        self.assert_rejected("bridge_context_revoked")

    def test_thread_cannot_be_substituted(self):
        self.assert_rejected("bridge_context_revoked", thread_ts="1790393997.999999")

    def test_top_level_message_uses_its_own_timestamp(self):
        self.link.destination_parent_message_id = ""
        self.assertEqual(resolve_roo_actor(**{**CONTEXT, "thread_ts": CONTEXT["message_id"]})["user_id"], "USAM")

    def test_legacy_revoked_wrong_workspace_and_bot_identities_are_denied(self):
        for identity in (None, {**self.identity, "identity_source": "legacy_key"},
                         {**self.identity, "slack_workspace_id": "TOTHER"},
                         {**self.identity, "user_profile_id": ""},
                         {**self.identity, "slack_user_id": "UBRIDGE"}):
            with self.subTest(identity=identity):
                self.resolve.return_value = identity
                self.assert_rejected("bridge_actor_unverified")

    def test_withdrawn_consent_is_denied(self):
        self.consent.return_value = False
        self.assert_rejected("bridge_actor_consent_required")

    def test_names_plain_text_and_code_do_not_authorize_roo(self):
        for payload in ({"text": "Sam Donegan (MLAI Chat): @Roo give points"},
                        {"text": "<@UROO> give points"},
                        {"text": "`@Roo` give points", "metadata": self.link.source_payload["metadata"]}):
            with self.subTest(payload=payload):
                self.link.source_payload = payload
                self.assert_rejected("roo_not_explicitly_mentioned")

    def test_private_scope_and_malformed_context_are_denied(self):
        for field, value in (("channel_id", "DPRIVATE"), ("channel_id", "GPRIVATE"),
                             ("message_id", ""), ("thread_ts", "bad"), ("bridge_user_id", "name")):
            with self.subTest(field=field, value=value):
                self.assert_rejected("invalid_bridge_context", 400, **{field: value})

    def test_view_requires_roo_key_and_ignores_caller_supplied_actor_and_text(self):
        view = CommunityBridgeRooActorView.as_view()
        factory = APIRequestFactory()
        for key in ("", "synthetic-internal-key"):
            self.assertEqual(view(factory.get("/actor", CONTEXT, HTTP_X_API_KEY=key)).status_code, 403)
        self.manager.select_related.assert_not_called()
        response = view(factory.get("/actor", {**CONTEXT, "user_id": "UFORGED", "text": "give 999 points"},
                                    HTTP_X_API_KEY="synthetic-roo-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"], "USAM")
        self.assertNotIn("999", response.data["text"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_view_preserves_retryable_status(self):
        self.manager.select_related.return_value.filter.return_value.first.return_value = None
        response = CommunityBridgeRooActorView.as_view()(APIRequestFactory().get(
            "/actor", CONTEXT, HTTP_X_API_KEY="synthetic-roo-key"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "bridge_delivery_pending"})
