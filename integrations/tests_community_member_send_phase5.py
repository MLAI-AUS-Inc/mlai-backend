"""Community actor selection and real immutable delivery/authority boundaries."""

from copy import deepcopy
from datetime import timedelta
import threading
from queue import Queue
from time import monotonic, sleep
from unittest.mock import patch, Mock
import uuid

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.models import AiConsentRecord
from community_chat.privacy import set_ai_consent
from community_chat.tests.privacy_fixtures import grant_test_ai_consent, PROVIDERS
from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink, CommunityBridgeIdentityLink, BridgeSyncState
from integrations.services import slack_dm_mirror as dm
from integrations.services.community_bridge import community_delivery as community, member_delivery as member
from integrations.services.community_bridge import member_attempts as attempts
from integrations.services.community_bridge.ai_consent import PublicBridgeConsentRequired
from integrations.services.community_bridge.store import claim_ready_deliveries, ingest_inbound_event, ingest_slack_event
from integrations.services.message_sync.delivery import delivery_context, guard_delivery
from integrations.services.message_sync.scheduler import LeaseLost, BudgetDeferred
from integrations.tests_member_delivery_phase4c import MemberDeliveryFixture
from integrations.tests_message_sync_member_history_phase4b import SETTINGS


CONFIG = {**SETTINGS, "SLACK_COMMUNITY_SEND_AS_MEMBER": True,
          "MESSAGE_SYNC_SLACK_APP_ID": "ABOT", "SLACK_BRIDGE_BOT_USER_ID": "UBOT",
          "SLACK_BRIDGE_BOT_TOKEN": "synthetic-bot-token", "COMMUNITY_CHAT_AI_CONSENT_REQUIRED": False,
          "COMMUNITY_CHAT_AI_PROVIDERS": PROVIDERS, "COMMUNITY_CHAT_AI_DISCLOSURE_VERSION": "phase5-test"}
REAL_BOT_CALL = community._BotClient.call


class CommunityFixture(MemberDeliveryFixture):
    grant_initial_consent = True

    def setUp(self):
        super().setUp()
        if self.grant_initial_consent:
            for user in (self.a.user, self.b.user):
                grant_test_ai_consent(user)
        self.channel.audience = "community"
        self.channel.audience_generation = None
        self.channel.audience_hash = ""
        self.channel.audience_sync_pending = True  # deliberately irrelevant here
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.save()
        self.bot_calls = []
        bot = patch.object(community._BotClient, "call", autospec=True, side_effect=self.bot_transport)
        bot.start()
        self.addCleanup(bot.stop)

    def delivery(self, *args, marked=True, **kwargs):
        row = super().delivery(*args, **kwargs)
        if marked:
            row.payload = {**row.payload, community.ENQUEUE: {"version": 1, "mapping": member._identity(self.channel)}}
            row.save()
        return row

    def send(self, row):
        claimed = claim_ready_deliveries(1)
        self.assertEqual([item["id"] for item in claimed], [row.pk])
        with delivery_context(claimed[0]):
            community.execute(claimed[0])
        row.refresh_from_db()

    def bot_transport(self, client, method, **kwargs):
        guard_delivery(client.row)
        if client.read_only and method in member.WRITES.values():
            raise AssertionError("reconciliation made a new mutation")
        client.started_at = timezone.now()
        self.bot_calls.append((method, kwargs))
        if method == "auth_test":
            return {"ok": True, "team_id": "TTEST", "user_id": "UBOT", "bot_id": "BBOT", "app_id": "ABOT"}
        if method == "conversations_info":
            return {"ok": True, "channel": {"id": "CROOM", "is_private": False, "is_channel": True, "is_member": True, "num_members": 2}}
        if method in {"conversations_history", "conversations_replies"}:
            return {"ok": True, "messages": self.history_messages}
        client.mutation_started = True
        return {"ok": True, "channel": "CROOM", "ts": kwargs.get("ts") or kwargs.get("timestamp") or f"{self.now}.{1000 + len(self.bot_calls):06d}",
                "message": {"user": "UBOT", "bot_id": "BBOT", "app_id": "ABOT", "client_msg_id": kwargs.get("client_msg_id"), "text": kwargs.get("text", "")}}


@override_settings(**CONFIG)
class CommunityDeliveryTests(CommunityFixture, TransactionTestCase):
    def test_linked_joined_member_posts_without_header_or_members_ack(self):
        row = self.delivery()
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.payload[community.MEMBER_STAMP]["actor"]["grant_id"], self.a.pk)
        self.assertEqual(self.mutations[0][2]["text"], "hello")
        self.assertEqual(self.bot_calls, [])
        self.assertNotIn("scope", row.payload[community.MEMBER_STAMP])

    def test_unlinked_account_posts_bot_with_authoritative_profile(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        self.a.user.full_name = "Current MLAI profile"
        self.a.user.save()
        row = self.delivery()
        row.payload["source_author_display_name"] = "forged display"
        row.save()
        self.send(row)
        command = row.payload[community.MEMBER_STAMP]
        self.assertEqual(command["actor"]["kind"], "bot")
        self.assertIn("Current MLAI profile", command["wire"]["kwargs"]["text"])
        self.assertNotIn("forged display", command["wire"]["kwargs"]["text"])
        self.assertTrue(command["wire"]["kwargs"]["unfurl_links"])
        self.assertEqual(self.mutations, [])

    def test_ninth_older_key_is_valid_community_member_and_bot_authority(self):
        for n in range(3, 12):
            CommunityChatDevice.objects.create(user=self.a.user, public_key=f"{n:064x}", status="verified", verified_at=timezone.now())
        self.send(self.delivery())
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        bot = self.delivery(source_id="b" * 64)
        self.send(bot)
        self.assertEqual(bot.payload[community.MEMBER_STAMP]["actor"]["kind"], "bot")

    def test_definitive_no_scope_or_left_member_falls_back_only_for_new_command(self):
        self.a.connection.scopes.remove("reactions:write")
        self.a.connection.save()
        parent = self.delivery()
        self.send(parent)
        row = self.delivery(op="reaction_add", parent=parent.source_message_id, source_id="b" * 64)
        row.payload["text"] = "👍"
        row.save()
        self.send(row)
        self.assertEqual(row.payload[community.MEMBER_STAMP]["actor"]["kind"], "bot")

    def test_provider_budget_or_uncertainty_during_selection_never_calls_bot(self):
        for error in (BudgetDeferred(10), TimeoutError("synthetic")):
            row = self.delivery(source_id=("b" if isinstance(error, BudgetDeferred) else "c") * 64)
            with patch.object(community.access, "poster_for_author", side_effect=error):
                with self.assertRaises(type(error)):
                    self.send(row)
            self.assertEqual(self.bot_calls, [])
            CommunityBridgeDelivery.objects.filter(pk=row.pk).delete()

    def test_unknown_probe_membership_is_uncertain_not_bot_ineligibility(self):
        from integrations.models import SlackChannelMembership

        SlackChannelMembership.objects.filter(grant=self.a).delete()
        row = self.delivery()
        with patch.object(community.access, "_probe_membership", return_value=None):
            with self.assertRaises(BudgetDeferred):
                self.send(row)
        self.assertEqual(self.bot_calls, [])

    def test_malformed_credential_capture_through_real_selector_never_falls_back(self):
        row = self.delivery()
        capture = dm._capture_slack_grant_api_authority

        def malformed_refresh(grant, **kwargs):
            if kwargs.get("refresh_token") is False:
                return capture(grant, **kwargs)
            raise dm.SlackDmMirrorCredentialError("synthetic malformed refresh")

        with patch.object(dm, "_capture_slack_grant_api_authority", side_effect=malformed_refresh):
            with self.assertRaisesRegex(member.MemberDeliveryRetry, "community_credentials_unproven"):
                self.send(row)
        self.assertEqual(self.bot_calls, [])
        row.refresh_from_db()
        self.assertNotIn(community.MEMBER_STAMP, row.payload)

    def test_real_membership_probe_unknown_public_shape_never_chooses_bot(self):
        from integrations.models import SlackChannelMembership

        SlackChannelMembership.objects.filter(grant=self.a).delete()
        row = self.delivery()
        requested = []

        def malformed_info(authority, connection, method, **kwargs):
            self.assertEqual(method, "conversations_info")
            requested.append(method)
            # This is not an authoritative public conversation. The real
            # MemberSlackClient denies it before _probe can return None.
            return {"ok": True, "channel": {"id": "CROOM", "is_private": False}}

        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=malformed_info):
            with self.assertRaisesRegex(member.MemberDeliveryRetry, "community_authority_unproven"):
                self.send(row)
        self.assertEqual(requested, ["conversations_info"])
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.mutations, [])
        row.refresh_from_db()
        self.assertNotIn(community.MEMBER_STAMP, row.payload)

    def test_definitive_left_member_can_use_bot_for_a_fresh_command(self):
        from integrations.models import SlackChannelMembership

        SlackChannelMembership.objects.filter(grant=self.a).update(
            state="left", source="event", observed_at=timezone.now(),
        )
        row = self.delivery()
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.payload[community.MEMBER_STAMP]["actor"]["kind"], "bot")
        self.assertEqual(self.mutations, [])

    def test_unlinked_new_content_requires_ai_even_when_legacy_toggle_is_false(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        AiConsentRecord.objects.filter(user=self.a.user).update(withdrawn_at=timezone.now())
        row = self.delivery()
        with override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False):
            with self.assertRaises(PublicBridgeConsentRequired):
                self.send(row)
        self.assertEqual(self.bot_calls, [])

    def test_out_of_scope_phase5_preserves_linked_bot_baseline_and_denies_new_d2(self):
        from integrations.services.community_bridge.store import _serialize_delivery

        row = self.delivery(marked=False)
        claim = claim_ready_deliveries(1)[0]
        send = Mock(return_value="legacy bot")
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER", COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True), delivery_context(claim):
            self.assertFalse(community.required(_serialize_delivery(row)))
            self.assertEqual(member.legacy_send(claim, send, require_ai=True), "legacy bot")
            CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
            with self.assertRaises(PublicBridgeConsentRequired):
                member.legacy_send(claim, send, require_ai=True)
        send.assert_called_once()

    def test_bot_broadcast_role_revocation_refuses_frozen_request_before_sdk(self):
        from .services.community_bridge.broadcast_mentions import BroadcastPermissionDenied

        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        row.payload["text"] = "@channel synthetic notice"
        row.save()
        with patch("community_chat.permissions.device_chat_role", side_effect=["admin", "member"]):
            with self.assertRaises(BroadcastPermissionDenied):
                self.send(row)
        self.assertFalse(any(method in member.WRITES.values() for method, _ in self.bot_calls))

    def test_bot_create_then_linked_edit_and_ai_off_cleanup_keep_bot(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        self.send(row)
        CommunityBridgeIdentityLink.objects.create(user=self.a.user, slack_workspace_id="TTEST", slack_user_id=self.a.slack_user_id,
            buzz_pubkey="1" * 64, verified_at=timezone.now(), verification_method="operator_attested",
            display_name="Synthetic", verification_reference="synthetic-fixture")
        edit = self.delivery(op="edit", source_id=row.source_message_id)
        self.send(edit)
        self.assertEqual(edit.payload[community.MEMBER_STAMP]["actor"]["kind"], "bot")
        self.assertEqual(self.mutations, [])
        with override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True):
            delete = self.delivery(op="delete", source_id=row.source_message_id)
            self.send(delete)
        self.assertEqual(delete.status, "completed")

    def test_member_create_flag_off_never_becomes_bot_and_cleanup_uses_original(self):
        row = self.delivery()
        self.send(row)
        with override_settings(SLACK_COMMUNITY_SEND_AS_MEMBER=False):
            edit = self.delivery(op="edit", source_id=row.source_message_id)
            with self.assertRaises(BudgetDeferred):
                self.send(edit)
            CommunityBridgeDelivery.objects.filter(pk=edit.pk).delete()
            cleanup = self.delivery(op="delete", source_id=row.source_message_id)
            self.send(cleanup)
        self.assertEqual(cleanup.payload[community.MEMBER_STAMP]["actor"]["grant_id"], self.a.pk)
        self.assertEqual(self.bot_calls, [])

    def test_lost_original_member_link_cannot_fall_back_to_bot(self):
        row = self.delivery()
        self.send(row)
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        cleanup = self.delivery(op="delete", source_id=row.source_message_id)
        with self.assertRaises(member.MemberDeliveryUnavailable):
            self.send(cleanup)
        self.assertEqual(self.bot_calls, [])

    def test_member_reaction_remove_uses_reacting_member_not_parent_poster(self):
        parent = self.delivery()
        self.send(parent)
        add = self.delivery(op="reaction_add", parent=parent.source_message_id, source_id="b" * 64, author="2" * 64)
        add.payload["text"] = "👍"
        add.save()
        self.send(add)
        remove = self.delivery(op="reaction_remove", parent=parent.source_message_id, source_id=add.source_message_id, author="2" * 64)
        remove.payload["text"] = "👍"
        remove.save()
        self.send(remove)
        self.assertEqual(remove.payload[community.MEMBER_STAMP]["actor"]["grant_id"], self.b.pk)
        self.assertEqual(self.mutations[-1][0], self.b.pk)
        self.assertEqual(self.mutations[-1][1], "reactions_remove")

    def test_legacy_unstamped_and_forged_saved_source_are_held_before_io(self):
        row = self.delivery(marked=False)
        with self.assertRaisesRegex(community.CommunityLegacyHold, "legacy_community_send_unproven"):
            self.send(row)
        self.assertEqual(self.calls + self.bot_calls, [])

    def test_community_attempt_implementation_cannot_authorize_a_members_row(self):
        row = self.delivery()
        row.status = "processing"
        row.save()
        self.channel.audience = "members"
        self.channel.save()
        with self.assertRaises(member.MemberDeliveryUnavailable):
            attempts.arm({"id": row.pk}, self.channel, {"version": 1}, implementation=community)
        self.assertEqual(self.calls + self.bot_calls, [])

    def test_accepted_bot_receipt_reconciles_after_ai_withdrawal_without_new_write(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError("synthetic settlement loss")):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        self.assertTrue(row.payload[attempts.ATTEMPT_KEY]["receipt"]["actor_verified"])
        AiConsentRecord.objects.filter(user=self.a.user).update(withdrawn_at=timezone.now())
        self.retry(row)
        self.bot_calls.clear()
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertFalse(any(method in member.WRITES.values() for method, _ in self.bot_calls))

    def test_not_sent_attempt_with_ai_off_cannot_mutate_or_read_reconcile(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        original = self.bot_transport
        def expire(client, method, **kwargs):
            if method == "chat_postMessage":
                raise LeaseLost("synthetic before SDK")
            return original(client, method, **kwargs)
        with patch.object(community._BotClient, "call", new=expire):
            with self.assertRaises(LeaseLost):
                self.send(row)
        row.refresh_from_db()
        self.assertEqual(row.payload[attempts.ATTEMPT_KEY]["status"], "not_sent")
        AiConsentRecord.objects.filter(user=self.a.user).update(withdrawn_at=timezone.now())
        self.retry(row)
        self.bot_calls.clear()
        with self.assertRaises(PublicBridgeConsentRequired):
            self.send(row)
        self.assertEqual(self.bot_calls, [])

    def test_wrong_workspace_disabled_or_converted_members_never_dispatches_bot(self):
        for changed in ({"slack_workspace_id": "TOTHER"}, {"enabled": False}, {"audience": "members"}):
            row = self.delivery()
            CommunityBridgeDelivery.objects.filter(pk=row.pk).update(status="processing")
            with transaction.atomic():
                original = deepcopy(self.channel)
                for key, value in changed.items():
                    setattr(self.channel, key, value)
                self.channel.save()
                with self.assertRaises((member.MemberDeliveryUnavailable, BudgetDeferred)):
                    community._suffix(row.pk, member._identity(original))
            for key in changed:
                setattr(self.channel, key, getattr(original, key))
            self.channel.save()
            row.delete()
        self.assertEqual(self.calls + self.bot_calls, [])

    def test_new_enqueue_marks_only_flag_on_community_and_strips_forged_stamps(self):
        event = {"delivery_type": "create", "source_message_id": "d" * 64, "source_author_id": "1" * 64,
                 "text": "synthetic", "metadata": {community.MEMBER_STAMP: {"actor": {"kind": "bot"}}}}
        result = ingest_inbound_event(source_platform="buzz", receipt_key="synthetic-new", source_channel_id=self.channel.destination_channel_id,
                                      event_type="create", normalized_event=event, raw_payload={})
        row = CommunityBridgeDelivery.objects.get(pk=result["delivery_id"])
        self.assertEqual(row.payload[community.ENQUEUE], {"version": 1, "mapping": member._identity(self.channel)})
        self.assertNotIn(community.MEMBER_STAMP, row.payload["metadata"])
        with override_settings(SLACK_COMMUNITY_SEND_AS_MEMBER=False):
            self.assertIsNone(community.enqueue_marker(self.channel, event))

    def test_new_flag_off_cleanup_still_gets_the_recorded_actor_fence(self):
        row = self.delivery()
        self.send(row)
        event = {"delivery_type": "delete", "source_message_id": row.source_message_id,
                 "source_author_id": "1" * 64, "text": "", "metadata": {}}
        with override_settings(SLACK_COMMUNITY_SEND_AS_MEMBER=False):
            result = ingest_inbound_event(source_platform="buzz", receipt_key="synthetic-cleanup", source_channel_id=self.channel.destination_channel_id,
                                          event_type="delete", normalized_event=event, raw_payload={})
            cleanup = CommunityBridgeDelivery.objects.get(pk=result["delivery_id"])
            self.assertIn(community.ENQUEUE, cleanup.payload)
            self.send(cleanup)
        self.assertEqual(cleanup.payload[community.MEMBER_STAMP]["actor"]["grant_id"], self.a.pk)
        self.assertEqual(self.bot_calls, [])

    def test_coworking_command_marker_is_not_phase5_admission(self):
        from integrations.services.community_bridge.coworking import COWORKING_CHANNEL_ID, BOOK_TODAY_MESSAGE

        self.channel.destination_channel_id = COWORKING_CHANNEL_ID
        event = {"delivery_type": "create", "source_channel_id": COWORKING_CHANNEL_ID,
                 "source_parent_message_id": "", "text": BOOK_TODAY_MESSAGE}
        with override_settings(COMMUNITY_CHAT_ROO_PUBLIC_KEY=""):
            self.assertIsNone(community.enqueue_marker(self.channel, event))

    def test_bot_lease_expiring_after_quota_prevents_actual_sdk_entry(self):
        row = self.delivery()
        row.status, row.lease_token = "processing", uuid.uuid4()
        row.lease_expires_at = timezone.now() + timedelta(seconds=30)
        row.save()
        client = community._BotClient(row, self.channel)
        sdk = Mock()
        actual_sdk_call = sdk.api_call
        sdk.chat_postMessage.side_effect = lambda **kwargs: sdk.api_call("chat.postMessage", json=kwargs)
        def expire(**kwargs):
            row.lease_expires_at = timezone.now() - timedelta(seconds=1)
        from integrations.services.message_sync import slack_client
        from integrations.services.community_bridge.store import _serialize_delivery
        with delivery_context(_serialize_delivery(row)), patch.object(community, "WebClient", return_value=sdk), patch.object(slack_client, "admit_request", side_effect=expire):
            with self.assertRaises(LeaseLost):
                REAL_BOT_CALL(client, "chat_postMessage", channel="CROOM", text="synthetic")
        # api_call was wrapped; its original mock is captured by the wrapper.
        self.assertFalse(client.mutation_started)
        actual_sdk_call.assert_not_called()
        self.assertEqual(sdk.retry_handlers, [])

    def test_identity_command_and_attempt_commit_before_mutation(self):
        row = self.delivery()
        original = self.transport
        def check(authority, conn, method, **kwargs):
            current = CommunityBridgeDelivery.objects.get(pk=row.pk)
            self.assertIn(community.IDENTITY, current.payload)
            if method == "chat_postMessage":
                self.assertIn(community.MEMBER_STAMP, current.payload)
                self.assertEqual(current.payload[attempts.ATTEMPT_KEY]["status"], "armed")
                self.assertEqual(transaction.get_connection().in_atomic_block, True)
            return original(authority, conn, method, **kwargs)
        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=check):
            self.send(row)

    def test_ambiguous_member_attempt_stays_same_and_retry_only_reads(self):
        row = self.delivery()
        original = self.transport
        def timeout(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                kwargs.pop("before_request")()
                raise TimeoutError("synthetic after SDK entry")
            return original(authority, conn, method, **kwargs)
        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=timeout):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        command = deepcopy(row.payload[community.MEMBER_STAMP])
        self.assertEqual(row.payload[attempts.ATTEMPT_KEY]["status"], "uncertain")
        self.retry(row)
        with self.assertRaises(BudgetDeferred):
            self.send(row)
        row.refresh_from_db()
        self.assertEqual(row.payload[community.MEMBER_STAMP], command)
        self.assertEqual(self.mutations + self.bot_calls, [])

    def test_saved_actor_reconsent_change_rejects_before_refresh(self):
        row = self.delivery()
        with patch.object(member, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        self.a.consented_at = timezone.now()
        self.a.save()
        self.retry(row)
        with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
            with self.assertRaises(member.MemberDeliveryUnavailable):
                self.send(row)
            refresh.assert_not_called()

    def test_echo_uses_real_community_command_and_current_sent_text(self):
        row = self.delivery()
        self.send(row)
        link = CommunityBridgeMessageLink.objects.get(source_message_id=row.source_message_id)
        response = ingest_slack_event({"api_app_id": "AUSER", "team_id": "TTEST", "event_id": "EvCOMMUNITY",
            "event": {"type": "message", "channel": "CROOM", "channel_type": "channel", "ts": link.destination_message_id,
                      "user": self.a.slack_user_id, "text": "hello", "client_msg_id": row.payload[community.IDENTITY]["client_msg_id"]}})
        self.assertEqual(response["reason"], "member_slack_echo")
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 1)

    def test_rollback_legacy_receipt_key_still_blocks_older_reaction_remove(self):
        parent = self.delivery()
        self.send(parent)
        add = self.delivery(op="reaction_add", source_id="b" * 64, parent=parent.source_message_id)
        add.payload["text"] = "👍"
        add.save()
        self.send(add)
        parent_link = CommunityBridgeMessageLink.objects.get(source_message_id=parent.source_message_id)
        reaction_link = CommunityBridgeMessageLink.objects.get(source_message_id=add.source_message_id)
        def event(kind, revision, event_id):
            return {"api_app_id": "ABOT", "team_id": "TTEST", "event_id": event_id,
                    "event": {"type": kind, "event_ts": f"{self.now + revision}.000001", "user": self.a.slack_user_id,
                              "reaction": "thumbsup", "item": {"type": "message", "channel": "CROOM", "ts": parent_link.destination_message_id}}}
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER", SLACK_COMMUNITY_SEND_AS_MEMBER=False):
            newer = ingest_slack_event(event("reaction_added", 20, "EvNEWER"))
            older = ingest_slack_event(event("reaction_removed", 10, "EvOLDER"))
        self.assertEqual(newer["reason"], "member_slack_echo")
        self.assertEqual(older["reason"], "superseded_member_reaction")
        reaction_link.refresh_from_db()
        self.assertIsNone(reaction_link.source_deleted_at)
        self.assertEqual(CommunityBridgeDelivery.objects.count(), 2)

    @skipUnlessDBFeature("has_select_for_update")
    def test_owner_device_and_grant_locks_precede_state_channel_and_delivery(self):
        sequences = {}
        def capture(execute, sql, params, many, context):
            if "FOR UPDATE" in sql:
                outer = connection.atomic_blocks[0]
                sequences.setdefault(outer, []).append(sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(capture):
            self.send(self.delivery())
        # Every lower-lock transaction may be a short metadata/attempt tx. In
        # an authority tx, no newly acquired User follows its State/Channel.
        users = get_user_model()._meta.db_table
        devices = CommunityChatDevice._meta.db_table
        lower = {BridgeSyncState._meta.db_table, type(self.channel)._meta.db_table, CommunityBridgeDelivery._meta.db_table}
        authority_sequences = [queries for queries in sequences.values() if any(f'"{users}"' in sql for sql in queries)]
        self.assertTrue(authority_sequences)
        for queries in authority_sequences:
            first_lower = next((i for i, sql in enumerate(queries) if any(f'"{table}"' in sql for table in lower)), len(queries))
            self.assertFalse(any(f'"{users}"' in sql for sql in queries[first_lower:]))
            if first_lower < len(queries):
                self.assertTrue(any(f'"{devices}"' in sql for sql in queries[:first_lower]))


@override_settings(**{**CONFIG, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED": True,
                     "COMMUNITY_CHAT_AI_PROVIDERS": PROVIDERS, "COMMUNITY_CHAT_AI_DISCLOSURE_VERSION": "phase5-test"})
class CommunityConsentTests(CommunityFixture, TransactionTestCase):
    grant_initial_consent = False

    def test_no_link_content_requires_ai_but_cleanup_survives_withdrawal(self):
        CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        with self.assertRaises(PublicBridgeConsentRequired):
            self.send(row)
        self.assertEqual(self.bot_calls, [])
        self.retry(row)
        grant_test_ai_consent(self.a.user)
        self.send(row)
        AiConsentRecord.objects.filter(user=self.a.user).update(withdrawn_at=timezone.now())
        remove = self.delivery(op="delete", source_id=row.source_message_id)
        self.send(remove)
        self.assertEqual(remove.status, "completed")


class CommunityBotProofTests(SimpleTestCase):
    @override_settings(SLACK_BRIDGE_BOT_USER_ID="UBOT", MESSAGE_SYNC_SLACK_APP_ID="ABOT", MESSAGE_SYNC_SLACK_USER_APP_ID="AUSER")
    def test_bot_response_requires_configured_bot_app_not_user_app_or_human(self):
        actor = {"kind": "bot", "slack_user_id": "UBOT"}
        self.assertTrue(attempts.actor_proven({"user": "UBOT", "bot_id": "BBOT", "app_id": "ABOT"}, actor))
        for message in ({"user": "UBOT"}, {"user": "UOTHER", "bot_id": "BBOT", "app_id": "ABOT"},
                        {"user": "UBOT", "bot_id": "BBOT", "app_id": "AUSER"}):
            self.assertFalse(attempts.actor_proven(message, actor))


@tag("postgres-only")
@override_settings(**{**CONFIG, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED": True,
                     "COMMUNITY_CHAT_AI_PROVIDERS": PROVIDERS, "COMMUNITY_CHAT_AI_DISCLOSURE_VERSION": "phase5-test"})
class CommunityAuthorityConcurrencyTests(CommunityFixture, TransactionTestCase):
    grant_initial_consent = False
    @skipUnlessDBFeature("has_select_for_update")
    def test_unlinked_device_revocation_waits_through_bot_send_and_settlement(self):
        self._interleave("device")

    @skipUnlessDBFeature("has_select_for_update")
    def test_unlinked_ai_withdrawal_waits_through_bot_send_and_settlement(self):
        self._interleave("ai")

    @skipUnlessDBFeature("has_select_for_update")
    def test_member_grant_revocation_waits_through_community_send_and_settlement(self):
        self._interleave("grant")

    def _interleave(self, kind):
        grant_test_ai_consent(self.a.user)
        if kind != "grant":
            CommunityBridgeIdentityLink.objects.filter(user=self.a.user).delete()
        row = self.delivery()
        claimed = claim_ready_deliveries(1)[0]
        entered, release, changed = (threading.Event() for _ in range(3))
        pids = Queue()
        errors = []
        original_member = self.transport
        original_bot = self.bot_transport

        def slow_member(authority, conn, method, **kwargs):
            if method == "chat_postMessage":
                kwargs.pop("before_request")()
                entered.set()
                if not release.wait(8):
                    raise AssertionError("synthetic send release missing")
            return original_member(authority, conn, method, **kwargs)

        def slow_bot(client, method, **kwargs):
            if method == "chat_postMessage":
                guard_delivery(client.row)
                client.mutation_started = True
                entered.set()
                if not release.wait(8):
                    raise AssertionError("synthetic send release missing")
            return original_bot(client, method, **kwargs)

        def send():
            close_old_connections()
            try:
                with delivery_context(claimed), patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=slow_member), patch.object(community._BotClient, "call", new=slow_bot):
                    community.execute(claimed)
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        def revoke():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids.put(cursor.fetchone()[0])
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.a.user_id)
                    if kind == "device":
                        CommunityChatDevice.objects.filter(user=self.a.user).update(status="revoked", revoked_at=timezone.now())
                    elif kind == "grant":
                        type(self.a).objects.filter(pk=self.a.pk).update(status="paused")
                    else:
                        AiConsentRecord.objects.filter(user=self.a.user).update(withdrawn_at=timezone.now())
                changed.set()
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        sender = threading.Thread(target=send)
        reducer = threading.Thread(target=revoke)
        sender.start()
        try:
            self.assertTrue(entered.wait(8))
            reducer.start()
            pid = pids.get(timeout=5)
            blocked = False
            end = monotonic() + 5
            while monotonic() < end:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                sleep(0.01)
            self.assertTrue(blocked, "revocation must wait for the real authority transaction")
            self.assertFalse(changed.is_set())
        finally:
            release.set()
            sender.join(10)
            if reducer.ident is not None:
                reducer.join(10)
        self.assertFalse(sender.is_alive())
        self.assertFalse(reducer.is_alive())
        self.assertEqual(errors, [])
        row.refresh_from_db()
        self.assertEqual(row.status, "completed")
        self.assertTrue(changed.is_set())
        next_row = self.delivery(source_id="e" * 64)
        if kind == "grant":
            # A fresh request may definitively choose the bot after pause;
            # recorded original member mutations still cannot switch actors.
            next_row = self.delivery(op="delete", source_id=row.source_message_id)
            CommunityBridgeDelivery.objects.filter(source_message_id="e" * 64).delete()
        with self.assertRaises((member.MemberDeliveryUnavailable, PublicBridgeConsentRequired)):
            self.send(next_row)
