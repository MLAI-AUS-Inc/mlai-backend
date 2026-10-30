"""Database-free security and contract regressions for bridge author resolution."""

from types import SimpleNamespace
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_bridge_actor import CommunityBridgeRooActorView
from integrations.services.community_bridge.roo_actor import BridgeActorError, resolve_roo_actor
from integrations.services.community_bridge.identity import verified_identity_for_buzz
from community_chat.models import DeviceBindingStatus
from integrations.services.community_bridge.roo_actor import _verified_member_poster
from integrations.services.community_bridge.member_attempts import command_hash

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
            channel_id=7,
            channel=SimpleNamespace(enabled=True, audience="community", destination_channel_id="chat-channel", slack_workspace_id="TMLAI",
                                    slack_visibility="public", slack_archived=False, slack_deleted=False, hold_reason="", mirror_decision="mirror"),
            source_channel_id="chat-channel", source_message_id="e6" * 32,
            source_author_id="bc" * 32, source_deleted_at=None, destination_deleted_at=None,
            destination_channel_id="CMEMES", destination_payload={},
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
        self.account = self.enterContext(patch(f"{MODULE}.community_account_for_key", return_value={
            "user_id": 42, "user_profile_id": "profile-id", "public_key": "bc" * 32,
        }))
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
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["posting_mode"], "bot")
        self.assertEqual(result["actor_profile_id"], "profile-id")
        self.assertEqual(result["recipient_targets"], [])
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
        self.resolve.assert_not_called()

    def test_verified_member_sender_may_equal_human_slack_actor(self):
        self.link.destination_payload = {"poster": {"kind": "member"}}
        with patch(f"{MODULE}._verified_member_poster") as verify:
            result = resolve_roo_actor(**{**CONTEXT, "bridge_user_id": "USAM"})
        self.assertEqual(result["posting_mode"], "member")
        self.assertEqual(result["user_id"], result["bridge_user_id"])
        verify.assert_called_once_with(self.link, {"kind": "member"}, self.account.return_value, "USAM")

    def test_member_current_slack_identity_cannot_change_after_delivery(self):
        self.link.destination_payload = {"poster": {"kind": "member"}}
        with patch(f"{MODULE}._verified_member_poster"):
            self.assert_rejected("bridge_actor_unverified", bridge_user_id="UOTHER")

    def test_revoked_signing_device_cannot_resolve_an_actor(self):
        self.account.return_value = None
        self.assert_rejected("bridge_actor_unverified")
        self.resolve.assert_not_called()

    def test_native_recipient_context_comes_from_verified_source_selection(self):
        profile = "00000000-0000-4000-8000-000000000002"
        self.link.source_payload["text"] += " @Chat Member"
        self.link.source_payload["metadata"]["mlai_mention_tags"] = [["mlai-mention", "aa" * 32, "Chat Member"]]
        self.link.source_payload["metadata"]["mlai_mention_bindings"] = [{"public_key": "aa" * 32, "profile_id": profile}]
        with patch("integrations.services.community_bridge.identity.community_account_for_key", return_value={
            "user_profile_id": profile, "display_name": "Current Account Name",
        }):
            result = resolve_roo_actor(**CONTEXT)
        self.assertEqual(result["recipient_targets"], [{
            "kind": "mlai_profile", "profile_id": profile, "display_name": "Current Account Name",
            "source_labels": ["Chat Member"],
        }])
        self.assertEqual(result["actor_profile_id"], "profile-id")
        self.assertEqual(result["source_labels"], ["Chat Member"])

    def test_omitted_native_targets_keep_signed_labels_for_amount_masking(self):
        self.link.source_payload["text"] += " @4 points"
        self.link.source_payload["metadata"]["mlai_mention_tags"] = [["mlai-mention", "aa" * 32, "4 points"]]
        result = resolve_roo_actor(**CONTEXT)
        self.assertEqual(result["recipient_targets"], [])
        self.assertEqual(result["source_labels"], ["4 points"])

    def test_ambiguous_signed_labels_remain_display_only_even_with_no_authority(self):
        self.link.source_payload["text"] += " @4 points"
        self.link.source_payload["metadata"]["mlai_mention_tags"] = [
            ["mlai-mention", "aa" * 32, "4 points"], ["mlai-mention", "bb" * 32, "4 Points"],
        ]
        result = resolve_roo_actor(**CONTEXT)
        self.assertEqual(result["recipient_targets"], [])
        self.assertEqual(result["source_labels"], ["4 points", "4 Points"])

    def test_bot_owner_proof_cannot_be_substituted(self):
        self.link.destination_payload = {"poster": {
            "kind": "bot", "user_id": 99, "public_key": self.link.source_author_id,
            "workspace_id": "TMLAI", "slack_user_id": "UBRIDGE",
        }}
        self.assert_rejected("unknown_bridge_sender", 404)

    def test_members_source_never_returns_text_or_resolves_roo_identity(self):
        self.link.channel.audience = "members"
        self.assert_rejected("bridge_context_revoked")
        self.resolve.assert_not_called()
        self.consent.assert_not_called()

    def test_missing_committed_delivery_is_retryable(self):
        self.manager.select_related.return_value.filter.return_value.first.return_value = None
        self.assert_rejected("bridge_delivery_pending", 409)
        self.resolve.assert_not_called()

    def test_missing_member_link_is_retryable_only_for_known_current_member(self):
        self.manager.select_related.return_value.filter.return_value.first.return_value = None
        with patch(f"{MODULE}.CommunityBridgeIdentityLink.objects") as identities:
            identities.filter.return_value.exists.return_value = True
            self.assert_rejected("bridge_delivery_pending", 409, bridge_user_id="USAM")
            identities.filter.return_value.exists.return_value = False
            self.assert_rejected("unknown_bridge_sender", 404, bridge_user_id="UUNRELATEDBOT")

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

    def test_private_archived_deleted_and_held_mappings_are_denied(self):
        for field, value in (("slack_visibility", "private"), ("slack_archived", True), ("slack_deleted", True),
                             ("hold_reason", "unverified"), ("mirror_decision", "hold")):
            original = getattr(self.link.channel, field)
            setattr(self.link.channel, field, value)
            with self.subTest(field=field):
                self.assert_rejected("bridge_context_revoked")
            setattr(self.link.channel, field, original)

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


class BridgeRooAccountDeviceTests(SimpleTestCase):
    """All devices use the existing account link; matching email is not authority."""

    def test_desktop_browser_and_ios_resolve_one_existing_slack_account(self):
        identity_module = "integrations.services.community_bridge.identity"
        user = SimpleNamespace(pk=42, full_name="Member", community_chat_profile_id="profile-id")
        link = SimpleNamespace(
            id=1, user_id=42, user=user, slack_workspace_id="TMLAI", slack_user_id="UMEMBER",
            buzz_pubkey="aa" * 32, display_name="Member", verification_method="slack_oauth",
            verified_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        )
        with (patch(f"{identity_module}.CommunityChatDevice.objects") as devices,
              patch(f"{identity_module}.CommunityBridgeIdentityLink.objects") as links):
            links.select_related.return_value.filter.return_value.first.return_value = link
            for platform, pubkey in (("desktop", "aa" * 32), ("browser", "bb" * 32), ("ios", "cc" * 32)):
                with self.subTest(platform=platform):
                    devices.select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(
                        user_id=42, public_key=pubkey,
                    )
                    identity = verified_identity_for_buzz(slack_workspace_id="TMLAI", buzz_pubkey=pubkey)
                    self.assertEqual(identity["slack_user_id"], "UMEMBER")
                    self.assertEqual(identity["user_profile_id"], "profile-id")
                    self.assertEqual(identity["buzz_pubkey"], pubkey)
                    devices.select_related.return_value.filter.assert_called_with(
                        public_key=pubkey, status=DeviceBindingStatus.VERIFIED,
                        revoked_at__isnull=True, user__is_active=True,
                    )
                    links.select_related.return_value.filter.assert_called_with(
                        slack_workspace_id="TMLAI", user_id=42, revoked_at__isnull=True,
                    )

    def test_revoked_device_cannot_reuse_an_account_link_or_match_by_email(self):
        identity_module = "integrations.services.community_bridge.identity"
        with (patch(f"{identity_module}.CommunityChatDevice.objects") as devices,
              patch(f"{identity_module}.CommunityBridgeIdentityLink.objects") as links):
            devices.select_related.return_value.filter.return_value.first.return_value = None
            links.filter.return_value.first.return_value = None
            self.assertIsNone(verified_identity_for_buzz(slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32))
            links.select_related.assert_not_called()
            links.filter.assert_called_once_with(
                slack_workspace_id="TMLAI", buzz_pubkey="aa" * 32,
                user__isnull=True, revoked_at__isnull=True,
            )


class BridgeRooMemberProofTests(SimpleTestCase):
    """The server acknowledgement and current grant are independently required."""

    def setUp(self):
        self.poster = {
            "kind": "member", "grant_id": 12, "user_id": 42, "connection_id": 13,
            "workspace_id": "TMLAI", "slack_user_id": "USAM", "public_key": "bc" * 32,
            "consent_generation": "epoch", "consent_version": "v1",
        }
        self.link = SimpleNamespace(
            channel_id=7, channel=SimpleNamespace(slack_workspace_id="TMLAI"),
            source_channel_id="chat-channel", source_message_id="e6" * 32,
            source_author_id="bc" * 32, destination_channel_id="CMEMES",
            destination_parent_message_id=CONTEXT["thread_ts"], destination_message_id=CONTEXT["message_id"],
            destination_payload={"text_hash": "text-hash"},
        )
        self.wire = {"kwargs": {
            "channel": "CMEMES", "thread_ts": CONTEXT["thread_ts"], "client_msg_id": "stable-id",
        }, "parent": CONTEXT["thread_ts"], "text_hash": "text-hash"}
        self.command = {"actor": self.poster, "wire": self.wire}
        self.fact = {
            "version": 1, "channel": "CMEMES", "ts": CONTEXT["message_id"], "operation": "create",
            "actor_verified": True, "outcome": "accepted", "text_hash": "text-hash", "client_msg_id": "stable-id",
        }
        self.delivery = SimpleNamespace(status="completed", completed_at="recorded", payload={
            "source_author_id": "bc" * 32, "_community_delivery_v1": self.command,
            "_member_network_attempt_v1": {
                "status": "acknowledged", "command_hash": command_hash(self.command), "receipt": self.fact,
            },
        })
        self.deliveries = self.enterContext(patch(f"{MODULE}.CommunityBridgeDelivery.objects"))
        self.deliveries.filter.return_value.first.return_value = self.delivery
        self.grants = self.enterContext(patch(f"{MODULE}.SlackDmMirrorGrant.objects"))
        self.grants.select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(pk=12)
        self.authority = SimpleNamespace(**{key: self.poster[key] for key in (
            "grant_id", "user_id", "connection_id", "workspace_id", "slack_user_id", "consent_generation", "consent_version",
        )}, scopes=("chat:write", "channels:read"))
        self.capture = self.enterContext(patch(f"{MODULE}.dm._capture_slack_grant_api_authority", return_value=self.authority))
        self.enabled = self.enterContext(patch(f"{MODULE}.access.enabled", return_value=True))
        self.public = self.enterContext(patch(f"{MODULE}.public_channels_enabled", return_value=True))
        self.enterContext(patch(f"{MODULE}.access._membership", return_value=SimpleNamespace()))
        self.membership = self.enterContext(patch(f"{MODULE}.access._membership_state", return_value="joined"))

    def verify(self):
        return _verified_member_poster(self.link, self.poster, {"user_id": 42}, "USAM")

    def test_acknowledged_member_is_revalidated_without_refresh_or_provider_io(self):
        self.verify()
        self.capture.assert_called_once_with(self.grants.select_related.return_value.filter.return_value.first.return_value, refresh_token=False)
        query = self.deliveries.filter.call_args.kwargs
        self.assertEqual(query["source_message_id"], self.link.source_message_id)
        self.assertEqual(query["channel_id"], self.link.channel_id)
        self.assertEqual(query["target_channel_id"], self.link.destination_channel_id)

    def test_pending_or_missing_delivery_remains_retryable(self):
        for delivery in (None, SimpleNamespace(status="processing")):
            self.deliveries.filter.return_value.first.return_value = delivery
            with self.assertRaises(BridgeActorError) as error:
                self.verify()
            self.assertEqual((error.exception.code, error.exception.status), ("bridge_delivery_pending", 409))
        self.capture.assert_not_called()

    def test_unacknowledged_tampered_or_wrong_scoped_delivery_is_denied(self):
        pristine = deepcopy(self.delivery.payload)
        mutations = [
            lambda p: p["_member_network_attempt_v1"].update(status="uncertain"),
            lambda p: p["_member_network_attempt_v1"].update(command_hash="altered"),
            lambda p: p["_member_network_attempt_v1"]["receipt"].update(actor_verified=False),
            lambda p: p["_member_network_attempt_v1"]["receipt"].update(ts="1790416956.999999"),
            lambda p: p["_community_delivery_v1"]["wire"]["kwargs"].update(channel="COTHER"),
            lambda p: p["_community_delivery_v1"]["wire"]["kwargs"].update(thread_ts="1790416956.999999"),
            lambda p: p["_community_delivery_v1"]["wire"]["kwargs"].pop("client_msg_id"),
            lambda p: p.update(source_author_id="aa" * 32),
        ]
        for mutate in mutations:
            self.delivery.payload = deepcopy(pristine)
            mutate(self.delivery.payload)
            with self.subTest(mutate=mutate), self.assertRaises(BridgeActorError) as error:
                self.verify()
            self.assertEqual(error.exception.code, "bridge_actor_unverified")
        self.capture.assert_not_called()

    def test_failed_completed_without_timestamp_and_changed_poster_are_denied(self):
        for field, value in (("status", "failed"), ("completed_at", None)):
            original = getattr(self.delivery, field)
            setattr(self.delivery, field, value)
            with self.assertRaises(BridgeActorError):
                self.verify()
            setattr(self.delivery, field, original)
        for field, value in (("user_id", 999), ("slack_user_id", "UOTHER"), ("public_key", "aa" * 32)):
            original = self.poster[field]
            self.poster[field] = value
            with self.assertRaises(BridgeActorError):
                self.verify()
            self.poster[field] = original

    def test_revoked_missing_grant_consent_membership_and_changed_connection_are_denied(self):
        for mock in (self.enabled, self.public, self.membership):
            original = mock.return_value
            mock.return_value = False
            with self.assertRaises(BridgeActorError):
                self.verify()
            mock.return_value = original
        grant_query = self.grants.select_related.return_value.filter.return_value.first
        grant_query.return_value = None
        with self.assertRaises(BridgeActorError):
            self.verify()
        grant_query.return_value = SimpleNamespace(pk=12)
        self.authority.connection_id = 999
        with self.assertRaises(BridgeActorError):
            self.verify()
