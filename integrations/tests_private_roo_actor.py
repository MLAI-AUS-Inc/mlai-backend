"""Private Roo handoff security regressions without a database or provider calls."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_bridge_actor import PrivateBridgeRooActorView
from integrations.services.community_bridge import private_roo_actor as actor
from integrations.services.community_bridge.roo_actor import BridgeActorError

MODULE = actor.__name__
TEXT = "<@UROO> give <@UMEMBER> 100 points for helping at an event"
SOURCE = "ab" * 32
CONTEXT = dict(workspace_id="TMLAI", channel_id="DROO", message_id="1793786540.585259",
               thread_ts="1793786540.585259", bridge_user_id="UADMIN")


@override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True, ROO_API_KEY="synthetic-roo-key")
class PrivateRooActorTests(SimpleTestCase):
    def setUp(self):
        self.target = self.enterContext(patch(f"{MODULE}.public_roo_target", return_value=("TMLAI", "UROO")))
        self.grant = SimpleNamespace(user_id=7, connection_id=8, connection=object(), slack_workspace_id="TMLAI",
            slack_user_id="UADMIN", status="active", revoked_at=None, user=SimpleNamespace(is_active=True))
        self.conversation = SimpleNamespace(grant=self.grant, slack_workspace_id="TMLAI",
            slack_conversation_id="DROO", participant_slack_ids=["UADMIN", "UROO"],
            mlai_channel_id="00000000-0000-4000-8000-000000000001", participant_hash="participants", status="live")
        self.oauth = self.enterContext(patch.object(actor.dm, "connection_slack_oauth_generation", return_value="oauth-1"))
        self.generation = self.enterContext(patch.object(actor.dm, "_grant_consent_generation", return_value="consent-1"))
        self.delivery = SimpleNamespace(conversation=self.conversation, source_message_id=SOURCE,
            source_author_id="cd" * 32, completed_at=datetime.now(timezone.utc), metadata={
                "source_event_id": SOURCE, "participant_hash": "participants", "thread_ts": "",
            })
        self.delivery.metadata.update(actor.roo_dm_receipt(self.delivery, TEXT))
        self.conversations = self.enterContext(patch.object(actor.SlackDmMirrorConversation, "objects"))
        self.conversations.select_related.return_value.filter.return_value.first.return_value = self.conversation
        self.deliveries = self.enterContext(patch.object(actor.SlackDmMirrorDelivery, "objects"))
        self.rows = self.deliveries.filter.return_value
        self.rows.filter.return_value.first.return_value = self.delivery
        self.rows.filter.return_value.filter.return_value.exists.return_value = False
        self.auth = self.enterContext(patch.object(actor.dm, "_assert_grant_connection_authorized"))
        self.writable = self.enterContext(patch.object(actor.dm, "_assert_slack_conversation_writable"))
        self.consent = self.enterContext(patch(f"{MODULE}.has_ai_consent", return_value=True))
        self.devices = self.enterContext(patch.object(actor.dm, "_conversation_owner_device_pubkeys", return_value=[self.delivery.source_author_id]))
        self.device = self.enterContext(patch.object(actor.dm, "_active_verified_device", return_value=object()))
        self.account = self.enterContext(patch(f"{MODULE}.community_account_for_key", return_value={
            "user_id": 7, "user_profile_id": "00000000-0000-4000-8000-000000000002",
        }))

    def reject(self, code, status=403, **context):
        with self.assertRaises(BridgeActorError) as caught:
            actor.resolve_roo_dm_actor(**{**CONTEXT, **context})
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def test_exact_completed_owner_delivery_returns_digest_without_private_text(self):
        result = actor.resolve_roo_dm_actor(**CONTEXT)
        self.assertEqual(result["user_id"], "UADMIN")
        self.assertEqual(result["posting_mode"], "member")
        self.assertEqual(result["source_event_id"], SOURCE)
        self.assertEqual(result["message_text_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual(result["recipient_targets"], [])
        self.assertNotIn("text", result)
        self.assertEqual(self.deliveries.filter.call_args.kwargs, {
            "conversation": self.conversation, "source_platform": "buzz",
        })
        query = self.conversations.select_related.return_value.filter.call_args.kwargs
        self.assertEqual(query["grant__slack_user_id"], "UADMIN")
        self.assertEqual(query["slack_conversation_id"], "DROO")
        self.auth.assert_called_once_with(self.grant)
        self.consent.assert_called_once_with(7)

    def test_only_the_owner_and_public_roo_one_to_one_dm_is_eligible(self):
        for participants in (["UADMIN", "UOTHER"], ["UADMIN", "UROO", "UOTHER"], []):
            with self.subTest(participants=participants):
                self.conversation.participant_slack_ids = participants
                self.reject("bridge_context_revoked")

    def test_public_and_group_channels_and_malformed_scope_are_rejected(self):
        for field, value in (("channel_id", "CPUBLIC"), ("channel_id", "GGROUP"),
                             ("message_id", "bad"), ("bridge_user_id", "display name")):
            with self.subTest(field=field, value=value):
                self.reject("invalid_bridge_context", 400, **{field: value})

    def test_wrong_workspace_roo_itself_and_unknown_owners_are_rejected(self):
        self.reject("unknown_bridge_sender", 404, workspace_id="TOTHER")
        self.reject("unknown_bridge_sender", 404, bridge_user_id="UROO")
        self.conversations.select_related.return_value.filter.return_value.first.return_value = None
        self.reject("unknown_bridge_sender", 404)

    def test_disabled_owner_revoked_grant_and_paused_conversation_are_rejected(self):
        for obj, field, value in ((self.grant.user, "is_active", False), (self.grant, "status", "paused"),
                                  (self.grant, "revoked_at", "revoked"), (self.conversation, "status", "paused")):
            original = getattr(obj, field)
            with self.subTest(field=field):
                setattr(obj, field, value)
                self.reject("bridge_context_revoked")
            setattr(obj, field, original)

    def test_stale_connection_archived_conversation_and_withdrawn_ai_consent_are_rejected(self):
        for check in (self.auth, self.writable):
            check.side_effect = actor.dm.SlackDmMirrorAuthorizationError("revoked")
            self.reject("bridge_context_revoked")
            check.side_effect = None
        self.consent.return_value = False
        self.reject("bridge_actor_consent_required")

    def test_callback_commit_race_is_retryable_but_unrelated_messages_are_terminal(self):
        self.rows.filter.return_value.first.return_value = None
        self.rows.filter.return_value.exists.return_value = True
        self.reject("bridge_delivery_pending", 409)
        self.rows.filter.return_value.exists.return_value = False
        self.reject("unknown_bridge_sender", 404)

    def test_scope_thread_and_authority_receipt_cannot_be_substituted(self):
        self.reject("bridge_actor_unverified", thread_ts="1793786540.999999")
        for field, value in (("connection_id", 99), ("oauth_generation", "new"),
                             ("consent_generation", "new"), ("mlai_channel_id", "other"),
                             ("text_sha256", "bad")):
            original = deepcopy(self.delivery.metadata)
            with self.subTest(field=field):
                self.delivery.metadata["roo_dm_v1"][field] = value
                self.reject("bridge_actor_unverified")
            self.delivery.metadata = original

    def test_legacy_missing_receipt_changed_participants_and_source_id_are_denied(self):
        original = deepcopy(self.delivery.metadata)
        for key, value in (("roo_dm_v1", None), ("participant_hash", "other"), ("source_event_id", "ef" * 32)):
            with self.subTest(key=key):
                self.delivery.metadata = {**original, key: value}
                self.reject("bridge_actor_unverified")

    def test_edited_or_deleted_source_cannot_invoke_roo(self):
        self.rows.filter.return_value.filter.return_value.exists.return_value = True
        self.reject("bridge_context_revoked")

    def test_revoked_device_or_changed_account_cannot_invoke_roo(self):
        self.devices.return_value = []
        self.reject("bridge_actor_unverified")
        self.devices.return_value = [self.delivery.source_author_id]
        self.device.return_value = None
        self.reject("bridge_actor_unverified")
        self.device.return_value = object()
        self.account.return_value = {"user_id": 99}
        self.reject("bridge_actor_unverified")

    def test_private_receipt_is_never_created_for_other_destinations(self):
        for participants, channel in ((["UADMIN", "UOTHER"], "DOTHER"), (["UADMIN", "UROO"], "CPUBLIC")):
            self.conversation.participant_slack_ids = participants
            self.conversation.slack_conversation_id = channel
            self.assertEqual(actor.roo_dm_receipt(self.delivery, TEXT), {})

    def test_actual_private_sender_commits_digest_of_normalized_provider_text(self):
        """Exercise the send boundary and receipt storage with every I/O mocked."""
        self.grant.connection = SimpleNamespace(access_token="synthetic-owner-token")
        self.grant.save = Mock()
        self.conversation.save = Mock()
        self.delivery.status = "processing"
        self.delivery.operation = "create"
        self.delivery.encrypted_text = TEXT
        self.delivery.pk = 17
        self.delivery.save = Mock()
        normalized = TEXT + " <https://example.test/>"
        client = Mock()
        client.chat_postMessage.return_value = {"ts": CONTEXT["message_id"], "message": {"text": normalized}}
        with (patch.object(actor.dm, "WebClient"),
              patch.object(actor.dm, "budgeted_client", return_value=client),
              patch.object(actor.dm, "_verify_ai_recipients_before_send"),
              patch.object(actor.dm, "_verify_roo_mentions_before_send"),
              patch.object(actor.dm, "_assert_private_delivery_authorized_locked"),
              patch("integrations.services.community_bridge.broadcast_mentions.prepare_private_slack_broadcasts", return_value=TEXT)):
            actor.dm._deliver_to_slack(self.delivery)
        self.assertEqual(self.delivery.status, "completed")
        self.assertEqual(self.delivery.encrypted_text, "")
        self.assertEqual(self.delivery.metadata["roo_dm_v1"]["text_sha256"], hashlib.sha256(normalized.encode()).hexdigest())
        self.assertEqual(self.delivery.metadata["slack_ts"], CONTEXT["message_id"])
        self.delivery.save.assert_called()

    def test_endpoint_requires_roo_key_and_is_not_cached(self):
        view = PrivateBridgeRooActorView.as_view()
        factory = APIRequestFactory()
        self.assertEqual(view(factory.get("/", CONTEXT)).status_code, 403)
        with patch("integrations.api_views_bridge_actor.resolve_roo_dm_actor", return_value={"user_id": "UADMIN"}) as resolve:
            response = view(factory.get("/", CONTEXT, HTTP_X_API_KEY="synthetic-roo-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        resolve.assert_called_once_with(**CONTEXT)
