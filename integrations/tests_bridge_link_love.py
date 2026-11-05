"""Automatic rewards require completed public delivery, not a Slack account."""

from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory
from integrations.api_views_bridge_actor import CommunityBridgeRooActorView
from integrations.services.community_bridge.roo_actor import BridgeActorError, resolve_roo_actor
from integrations.tests_bridge_roo_actor import CONTEXT, MODULE

PROFILE = "00000000-0000-4000-8000-000000000001"


@override_settings(ROO_LINK_LOVE_SLACK_CHANNEL_ID="CMEMES", SLACK_BRIDGE_BOT_USER_ID="UBRIDGE",
                  COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True, ROO_API_KEY="synthetic-roo-key",
                  INTERNAL_API_KEY="synthetic-internal-key")
class LinkLoveActorTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch(f"{MODULE}.public_roo_target", return_value=("TMLAI", "UROO")))
        self.link = SimpleNamespace(channel_id=7, channel=SimpleNamespace(enabled=True, audience="community",
            slack_visibility="public", slack_archived=False, slack_deleted=False, hold_reason="", mirror_decision="mirror",
            destination_channel_id="chat-channel"), source_channel_id="chat-channel", source_message_id="ab" * 32,
            source_author_id="cd" * 32, source_deleted_at=None, destination_deleted_at=None,
            destination_message_id=CONTEXT["message_id"], destination_parent_message_id=CONTEXT["thread_ts"],
            destination_payload={"poster": {"kind": "bot", "user_id": 42, "public_key": "cd" * 32,
                "workspace_id": "TMLAI", "slack_user_id": "UBRIDGE"}}, source_payload={"text": "done"})
        self.links = self.enterContext(patch(f"{MODULE}.CommunityBridgeMessageLink.objects"))
        self.links.select_related.return_value.filter.return_value.first.return_value = self.link
        self.links.filter.return_value.first.return_value = None
        self.account = self.enterContext(patch(f"{MODULE}.community_account_for_key", return_value={
            "user_id": 42, "user_profile_id": PROFILE, "slack_user_id": ""}))
        self.identity = self.enterContext(patch(f"{MODULE}.verified_identity_for_buzz", return_value=None))
        self.receipt = self.enterContext(patch(f"{MODULE}._acknowledged_create"))
        self.consent = self.enterContext(patch(f"{MODULE}.has_ai_consent", return_value=True))

    def resolve(self, **overrides):
        return resolve_roo_actor(**{**CONTEXT, "purpose": "link_love", **overrides})

    def test_chat_only_done_resolves_canonical_owner_without_mention_or_slack_link(self):
        result = self.resolve()
        self.assertEqual(result["actor_profile_id"], PROFILE)
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["purpose"], "link_love")
        self.assertFalse(result["is_root_author"])
        self.assertNotIn("user_id", result)
        self.identity.assert_not_called()
        self.receipt.assert_called_with(self.link, self.link.destination_payload["poster"])
        self.consent.assert_called_once_with(42)

    def test_wrong_channel_root_private_and_unknown_purpose_are_denied(self):
        for override in ({"channel_id": "COTHER"}, {"channel_id": "DPRIVATE"},
                         {"thread_ts": CONTEXT["message_id"]}, {"purpose": "admin"}):
            with self.subTest(override=override), self.assertRaises(BridgeActorError):
                self.resolve(**override)
        self.links.select_related.assert_not_called()

    def test_revoked_device_withdrawn_consent_and_missing_receipt_fail_closed(self):
        self.account.return_value = None
        with self.assertRaises(BridgeActorError): self.resolve()
        self.account.return_value = {"user_id": 42, "user_profile_id": PROFILE, "slack_user_id": ""}
        self.consent.return_value = False
        with self.assertRaises(BridgeActorError): self.resolve()
        self.consent.return_value = True
        self.receipt.side_effect = BridgeActorError("bridge_actor_unverified", 403)
        with self.assertRaises(BridgeActorError): self.resolve()

    def test_own_chat_root_is_reported_even_across_devices(self):
        self.links.filter.return_value.first.return_value = SimpleNamespace(source_author_id="ef" * 32, destination_payload={})
        self.assertTrue(self.resolve()["is_root_author"])

    def test_rotating_the_root_device_does_not_allow_self_rewards(self):
        self.links.filter.return_value.first.return_value = SimpleNamespace(source_author_id="ef" * 32,
            destination_payload={"poster": {"kind": "bot", "user_id": 42}})
        self.account.side_effect = [self.account.return_value, None]
        self.assertTrue(self.resolve()["is_root_author"])

    def test_member_delivery_revalidates_original_grant(self):
        self.link.destination_payload["poster"]["kind"] = "member"
        with patch(f"{MODULE}._verified_member_poster") as check:
            self.resolve()
            check.assert_called_once()

    def test_legacy_bot_record_and_missing_link_are_denied_or_retryable(self):
        self.link.destination_payload = {}
        with self.assertRaises(BridgeActorError): self.resolve()
        self.links.select_related.return_value.filter.return_value.first.return_value = None
        with self.assertRaises(BridgeActorError) as caught: self.resolve()
        self.assertEqual(caught.exception.status, 409)

    def test_view_requires_roo_key_and_forwards_only_explicit_purpose(self):
        view = CommunityBridgeRooActorView.as_view()
        factory = APIRequestFactory()
        for key in ("", "synthetic-internal-key"):
            self.assertEqual(view(factory.get("/actor", {**CONTEXT, "purpose": "link_love"}, HTTP_X_API_KEY=key)).status_code, 403)
        result = view(factory.get("/actor", {**CONTEXT, "purpose": "link_love", "actor_profile_id": "forged"}, HTTP_X_API_KEY="synthetic-roo-key"))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["actor_profile_id"], PROFILE)
