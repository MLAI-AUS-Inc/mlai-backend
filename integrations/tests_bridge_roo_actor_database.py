"""Canonical database handoff regressions; all provider sends remain mocked."""

from datetime import datetime, timedelta, timezone as datetime_timezone
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink
from integrations.services import slack_dm_mirror as dm
from integrations.services.community_bridge.member_attempts import command_hash
from integrations.services.community_bridge.member_delivery import _binding
from integrations.services.community_bridge.native_mentions import native_mention_bindings, native_recipient_targets
from integrations.services.community_bridge.roo_actor import BridgeActorError, resolve_roo_actor
from integrations.tests_slack_member_access import MemberAccessFixture


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True,
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST", SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    SLACK_BRIDGE_BOT_USER_ID="UBRIDGE", COMMUNITY_CHAT_RELAY_URL="https://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TTEST", COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
    COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class BridgeRooActorDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.channel.audience = "community"
        self.channel.save(update_fields=("audience",))
        self.join(self.grant)
        self.recipient = get_user_model().objects.create_user(email="chat-only@example.invalid", full_name="Chat Member")
        self.recipient_device = CommunityChatDevice.objects.create(
            user=self.recipient, public_key="c" * 64, status="verified", verified_at=timezone.now(),
        )
        self.poster = _binding(dm._capture_slack_grant_api_authority(self.grant, refresh_token=False), self.device.public_key)
        wire = {"kwargs": {"channel": "CROOM", "client_msg_id": "synthetic-operation"}, "parent": "", "text_hash": "text-hash"}
        command = {"actor": self.poster, "wire": wire}
        fact = {
            "version": 1, "channel": "CROOM", "ts": "1790416956.199559", "operation": "create",
            "actor_verified": True, "outcome": "accepted", "text_hash": "text-hash", "client_msg_id": "synthetic-operation",
        }
        self.payload = {
            "source_author_id": self.device.public_key, "text": "@Roo please give @Chat Member 4 points",
            "metadata": {"slack_mention_tags": [["slack-mention", "UROO", "Roo"]],
                         "mlai_mention_tags": [["mlai-mention", self.recipient_device.public_key, "Chat Member"]],
                         "mlai_mention_bindings": [{"public_key": self.recipient_device.public_key,
                                                    "profile_id": str(self.recipient.community_chat_profile_id)}]},
            "_community_delivery_v1": command,
            "_member_network_attempt_v1": {"status": "acknowledged", "command_hash": command_hash(command), "receipt": fact},
        }
        self.delivery = CommunityBridgeDelivery.objects.create(
            channel=self.channel, source_platform="buzz", target_platform="slack", delivery_type="create", status="completed",
            source_channel_id=self.channel.destination_channel_id, source_message_id="e" * 64,
            target_channel_id="CROOM", payload=self.payload, available_at=timezone.now(), completed_at=timezone.now(),
        )
        self.message = CommunityBridgeMessageLink.objects.create(
            channel=self.channel, source_platform="buzz", destination_platform="slack",
            source_channel_id=self.channel.destination_channel_id, source_message_id="e" * 64,
            source_author_id=self.device.public_key, destination_channel_id="CROOM", destination_message_id=fact["ts"],
            source_payload=self.payload, destination_payload={"poster": self.poster, "text_hash": "text-hash"},
        )
        self.context = {
            "workspace_id": "TTEST", "channel_id": "CROOM", "message_id": fact["ts"],
            "thread_ts": fact["ts"], "bridge_user_id": self.grant.slack_user_id,
        }

    def test_member_command_resolves_canonical_chat_only_recipient_without_slack(self):
        result = resolve_roo_actor(**self.context)
        self.assertIsNone(self.recipient.slack_id)
        self.assertEqual(result["user_id"], self.grant.slack_user_id)
        self.assertEqual(result["posting_mode"], "member")
        self.assertEqual(result["actor_profile_id"], str(self.grant.user.community_chat_profile_id))
        self.assertEqual(result["recipient_targets"], [{
            "kind": "mlai_profile", "profile_id": str(self.recipient.community_chat_profile_id), "display_name": "Chat Member",
            "source_labels": ["Chat Member"],
        }])
        self.provider.assert_not_called()

    def test_recipient_revoked_after_send_is_omitted_and_routing_keys_do_not_replace_it(self):
        self.recipient_device.revoked_at = timezone.now()
        self.recipient_device.save(update_fields=("revoked_at",))
        self.message.source_payload["metadata"]["p_tags"] = [["p", self.device.public_key]]
        self.message.save(update_fields=("source_payload",))
        self.assertEqual(resolve_roo_actor(**self.context)["recipient_targets"], [])

    def test_reenrolled_key_cannot_switch_a_pending_award_to_another_account(self):
        self.assertEqual(resolve_roo_actor(**self.context)["recipient_targets"][0]["profile_id"],
                         str(self.recipient.community_chat_profile_id))
        CommunityChatDevice.objects.filter(pk=self.recipient_device.pk).update(status="revoked", revoked_at=timezone.now())
        other = get_user_model().objects.create_user(email="replacement@example.invalid", full_name="Replacement")
        CommunityChatDevice.objects.create(user=other, public_key=self.recipient_device.public_key,
                                           status="verified", verified_at=timezone.now())
        result = resolve_roo_actor(**self.context)
        self.assertEqual(result["recipient_targets"], [])
        self.assertEqual(result["source_labels"], ["Chat Member"])

    def test_delayed_callback_snapshots_historical_owner_and_rejects_current_rebinding(self):
        now = timezone.now()
        CommunityChatDevice.objects.filter(pk=self.recipient_device.pk).update(
            created_at=now - timedelta(seconds=40), verified_at=now - timedelta(seconds=35),
            status="revoked", revoked_at=now - timedelta(seconds=10),
        )
        other = get_user_model().objects.create_user(email="delayed-replacement@example.invalid")
        replacement = CommunityChatDevice.objects.create(user=other, public_key=self.recipient_device.public_key,
            status="verified", verified_at=now - timedelta(seconds=5))
        CommunityChatDevice.objects.filter(pk=replacement.pk).update(created_at=now - timedelta(seconds=6))
        tags = self.payload["metadata"]["mlai_mention_tags"]
        bindings = native_mention_bindings(workspace_id="TTEST", text=self.payload["text"], tags=tags,
            source_created_at=int((now - timedelta(seconds=20)).timestamp()))
        self.assertEqual(bindings, [{"public_key": self.recipient_device.public_key,
                                     "profile_id": str(self.recipient.community_chat_profile_id)}])
        self.assertEqual(native_recipient_targets(workspace_id="TTEST", text=self.payload["text"],
                                                 tags=tags, bindings=bindings), [])

    def test_same_second_enrollment_is_accepted_but_ambiguous_owners_are_not(self):
        source_time = int(timezone.now().timestamp()) - 100
        start = datetime.fromtimestamp(source_time, tz=datetime_timezone.utc)
        CommunityChatDevice.objects.filter(pk=self.recipient_device.pk).update(
            created_at=start + timedelta(milliseconds=100), verified_at=start + timedelta(milliseconds=200),
        )
        tags = self.payload["metadata"]["mlai_mention_tags"]
        original = native_mention_bindings(workspace_id="TTEST", text=self.payload["text"], tags=tags,
                                          source_created_at=source_time)
        self.assertEqual(original[0]["profile_id"], str(self.recipient.community_chat_profile_id))
        CommunityChatDevice.objects.filter(pk=self.recipient_device.pk).update(
            status="revoked", revoked_at=start + timedelta(milliseconds=500),
        )
        other = get_user_model().objects.create_user(email="ambiguous-replacement@example.invalid")
        replacement = CommunityChatDevice.objects.create(user=other, public_key=self.recipient_device.public_key,
            status="verified", verified_at=start + timedelta(milliseconds=800))
        CommunityChatDevice.objects.filter(pk=replacement.pk).update(created_at=start + timedelta(milliseconds=700))
        self.assertEqual(native_mention_bindings(workspace_id="TTEST", text=self.payload["text"], tags=tags,
                                                source_created_at=source_time), [])

    def test_acknowledged_bot_fallback_keeps_human_authority_and_native_recipient(self):
        poster = {
            "kind": "bot", "user_id": self.grant.user_id, "public_key": self.device.public_key,
            "workspace_id": "TTEST", "slack_user_id": "UBRIDGE",
        }
        command = self.delivery.payload["_community_delivery_v1"]
        command["actor"] = poster
        self.delivery.payload["_member_network_attempt_v1"]["command_hash"] = command_hash(command)
        self.delivery.save(update_fields=("payload",))
        self.message.destination_payload["poster"] = poster
        self.message.save(update_fields=("destination_payload",))
        result = resolve_roo_actor(**{**self.context, "bridge_user_id": "UBRIDGE"})
        self.assertEqual(result["posting_mode"], "bot")
        self.assertEqual(result["user_id"], self.grant.slack_user_id)
        self.assertEqual(result["recipient_targets"][0]["profile_id"], str(self.recipient.community_chat_profile_id))
        self.provider.assert_not_called()

    def test_current_actor_device_and_grant_revocation_fail_closed(self):
        self.device.revoked_at = timezone.now()
        self.device.save(update_fields=("revoked_at",))
        with self.assertRaises(BridgeActorError) as error:
            resolve_roo_actor(**self.context)
        self.assertEqual(error.exception.code, "bridge_actor_unverified")
        self.device.revoked_at = None
        self.device.save(update_fields=("revoked_at",))
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=("revoked_at",))
        with self.assertRaises(BridgeActorError) as error:
            resolve_roo_actor(**self.context)
        self.assertEqual(error.exception.code, "bridge_actor_unverified")

    def test_reconsent_cannot_reuse_original_member_poster_epoch(self):
        self.grant.consented_at = timezone.now()
        self.grant.save(update_fields=("consented_at",))
        with self.assertRaises(BridgeActorError) as error:
            resolve_roo_actor(**self.context)
        self.assertEqual(error.exception.code, "bridge_actor_unverified")

    def test_completed_link_without_acknowledged_attempt_never_delegates_member(self):
        self.delivery.payload["_member_network_attempt_v1"]["status"] = "uncertain"
        self.delivery.save(update_fields=("payload",))
        with self.assertRaises(BridgeActorError) as error:
            resolve_roo_actor(**self.context)
        self.assertEqual(error.exception.code, "bridge_actor_unverified")
