"""Device changes retain the room, source checkpoint and relay receipts."""
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import SlackDmMirrorDelivery
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_dm_registration_ledger import finalize_registration_attempt
from integrations.services.community_bridge.buzz import BuzzBridgeClient, BuzzBridgeError


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_STABLE_PRIVATE_ROOMS=True,
                   SLACK_DM_MIRROR_SHADOW_SECRET="synthetic-audience-test")
class StableDeviceAudienceTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.room = str(self.conversation.mlai_channel_id)
        self.conversation.history_backfilled_at = timezone.now()
        self.conversation.latest_synced_ts = "1700000000.000001"
        self.conversation.save()
        self.delivered = self.row("1700000000.000001", status="completed", attempts=1,
                                  metadata={"destination_message_id": "a" * 64})
        self.ambiguous = self.row("1700000000.000002", attempts=1)
        self.pending = self.row("1700000000.000003")
        self.checkpoint = self.row("history-checkpoint", metadata={"history_scan_state": True,
                                  "cursor": "page-12", "source_floor": "1697408000.000001"})

    def row(self, source, **kwargs):
        return SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation, source_platform="slack", source_message_id=source,
            source_author_id="UOTHER", operation="create", available_at=timezone.now(), **kwargs,
        )

    def prepare(self, reset=False):
        with transaction.atomic():
            return dm._prepare_owner_conversation_locked(
                self.conversation, force_backfill=False, reset_history=reset,
                required_owner_public_key=None,
            )

    def test_device_change_preserves_history_and_reconciles_lost_ack(self):
        request, error = self.prepare()
        self.assertIsNone(error)
        self.assertEqual(request["private_audience"]["channel_id"], self.room)
        with patch.object(BuzzBridgeClient, "private_delivery_receipts", return_value={
            str(self.ambiguous.pk): {"message_id": "b" * 64, "parent_message_id": "a" * 64},
        }) as receipts:
            self.assertTrue(finalize_registration_attempt(request["attempt_id"], channel_id=self.room))
        receipts.assert_called_once_with(self.room, [str(self.ambiguous.pk)])
        for row in [self.delivered, self.ambiguous, self.pending, self.checkpoint]:
            row.refresh_from_db()
            self.assertEqual(row.metadata["participant_hash"], self.conversation.participant_hash)
        self.assertEqual(self.delivered.metadata["destination_message_id"], "a" * 64)
        self.assertEqual(self.ambiguous.status, "completed")
        self.assertEqual(self.ambiguous.metadata["destination_message_id"], "b" * 64)
        self.assertEqual(self.pending.status, "pending")
        self.assertEqual(self.checkpoint.metadata["cursor"], "page-12")
        self.conversation.refresh_from_db()
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertIsNotNone(self.conversation.history_backfilled_at)
        self.assertEqual(self.conversation.latest_synced_ts, "1700000000.000001")

    def test_receipt_failure_never_promotes_unreconciled_room(self):
        request, _ = self.prepare()
        with patch.object(BuzzBridgeClient, "private_delivery_receipts", side_effect=BuzzBridgeError("offline")):
            with self.assertRaises(BuzzBridgeError):
                finalize_registration_attempt(request["attempt_id"], channel_id=self.room)
        self.conversation.refresh_from_db()
        self.ambiguous.refresh_from_db()
        self.assertEqual(self.conversation.status, "provisioning")
        self.assertEqual(self.ambiguous.status, "pending")

    def test_explicit_history_reset_does_not_use_device_only_transition(self):
        request, _ = self.prepare(reset=True)
        self.assertNotIn("private_audience", request)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.history_backfilled_at)

    def test_identity_recovery_retains_canonical_room_and_source_bounds(self):
        CommunityChatDevice.objects.filter(user=self.user, public_key=self.owner_key).update(
            status="revoked", revoked_at=timezone.now())
        new_key = "c" * 64
        CommunityChatDevice.objects.create(user=self.user, public_key=new_key, status="verified", verified_at=timezone.now())
        _, changed, _ = dm.ensure_owner_identity(self.grant, authenticated_public_key=new_key)
        self.assertTrue(changed)
        self.conversation.refresh_from_db()
        self.delivered.refresh_from_db()
        self.checkpoint.refresh_from_db()
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.conversation.status, "provisioning")
        self.assertIsNotNone(self.conversation.history_backfilled_at)
        self.assertEqual(self.delivered.status, "completed")
        self.assertEqual(self.checkpoint.metadata["cursor"], "page-12")

    def test_revocation_fences_access_without_erasing_surviving_account_history(self):
        from community_chat.device_revocation import revoke_device_authority
        self.conversation.participant_buzz_pubkeys = [self.owner_key, "b" * 64]
        self.conversation.participant_identity_map = {"UOWNER": self.owner_key, "UOTHER": "b" * 64}
        self.conversation.save()
        device = CommunityChatDevice.objects.get(user=self.user, public_key=self.owner_key)
        result = revoke_device_authority(
            self.user, device_id=device.pk, public_key=self.owner_key, reason="synthetic replacement",
            revoke_relay_membership_callback=lambda _: ("revoked", {}),
        )
        self.assertEqual(result.status, "revoked")
        self.conversation.refresh_from_db()
        self.delivered.refresh_from_db()
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.conversation.status, "provisioning")
        self.assertIsNotNone(self.conversation.history_backfilled_at)
        self.assertEqual(self.delivered.status, "completed")
        with self.assertRaises(dm.SlackDmMirrorError):
            dm.ensure_owner_identity(self.grant, authenticated_public_key=self.owner_key)
