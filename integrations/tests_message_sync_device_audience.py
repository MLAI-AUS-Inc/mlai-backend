"""Device changes retain the room, source checkpoint and relay receipts."""
from unittest.mock import patch

from django.db import transaction
from django.db.models import Exists
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import BridgeSyncState, SlackDmMirrorConversation, SlackDmMirrorDelivery
from integrations.services.message_sync.private_coverage import current_coverage_rows
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_dm_registration_ledger import finalize_registration_attempt
from integrations.services import slack_dm_registration_ledger as ledger
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

    def coverage(self, **overrides):
        proof = {"import_contract_version": 2, "channel_id": self.room,
                 "participant_hash": self.conversation.participant_hash,
                 "classification": "accessible_range", "oldest": "1697408000.000001",
                 "latest": "1700000000.000001", "checked_at": timezone.now().isoformat(),
                 **overrides}
        return BridgeSyncState.objects.create(
            private_conversation=self.conversation, workspace_id="TIOAUTH", source_channel_id="DIOAUTH",
            verified_ranges={"archive": proof}, status="current",
        )

    def finish(self, request):
        with patch.object(BuzzBridgeClient, "private_delivery_receipts", return_value={}):
            self.assertTrue(finalize_registration_attempt(request["attempt_id"], channel_id=self.room))

    def test_device_transition_retains_current_coverage_without_claiming_new_history(self):
        state = self.coverage()
        before = dict(state.verified_ranges["archive"])
        request, _ = self.prepare()
        self.finish(request)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"], {**before, "participant_hash": self.conversation.participant_hash})
        self.assertTrue(SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk).filter(Exists(current_coverage_rows())).exists())

    def test_retry_recovers_frozen_coverage_after_preparation_changed_audience(self):
        state = self.coverage()
        original = dict(state.verified_ranges["archive"])
        first, _ = self.prepare()
        second, _ = self.prepare()
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(second["private_audience"]["coverage_proof"], original)
        self.finish(second)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["archive"]["participant_hash"], self.conversation.participant_hash)

    def test_unrelated_coverage_is_not_promoted_by_device_change(self):
        state = self.coverage(participant_hash="unrelated-audience")
        before = dict(state.verified_ranges)
        request, _ = self.prepare()
        self.assertNotIn("coverage_proof", request["private_audience"])
        self.finish(request)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges, before)
        self.assertFalse(SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk).filter(Exists(current_coverage_rows())).exists())

    def test_coverage_replaced_during_relay_io_is_not_overwritten(self):
        state = self.coverage()
        request, _ = self.prepare()
        replacement = {"archive": {"classification": "source_limited", "checked_at": "new-proof"}}
        state.verified_ranges = replacement
        state.save(update_fields=["verified_ranges"])
        self.finish(request)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges, replacement)

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

    def prepare_replacement(self):
        """Exercise the same revoke and owner-rebind boundaries as a new login."""
        from community_chat.device_revocation import revoke_device_authority

        self.conversation.participant_buzz_pubkeys = [self.owner_key, "b" * 64]
        self.conversation.participant_identity_map = {"UOWNER": self.owner_key, "UOTHER": "b" * 64}
        self.conversation.latest_synced_ts = str(timezone.now().timestamp())
        self.conversation.save()
        new_key = "c" * 64
        CommunityChatDevice.objects.create(
            user=self.user, public_key=new_key, status="verified", verified_at=timezone.now(),
        )
        device = CommunityChatDevice.objects.get(user=self.user, public_key=self.owner_key)
        revoke_device_authority(
            self.user, device_id=device.pk, public_key=self.owner_key,
            reason="synthetic replacement", revoke_relay_membership_callback=lambda _: ("revoked", {}),
        )
        dm.ensure_owner_identity(self.grant, authenticated_public_key=new_key)
        self.conversation.refresh_from_db()
        return new_key

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    @patch.object(BuzzBridgeClient, "private_delivery_receipts", return_value={})
    def test_replacement_cleanup_then_reprovision_preserves_room_and_history(self, receipts, unregister):
        from integrations.services.slack_chat_catalog import catalog_conversations, catalog_payload

        state = self.coverage()
        before = dict(state.verified_ranges["archive"])
        new_key = self.prepare_replacement()
        ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        unregister.assert_called_once_with(self.room)
        self.conversation.refresh_from_db()
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.conversation.status, "provisioning")
        self.assertEqual(catalog_payload(catalog_conversations(
            SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk),
        ), new_key), [])  # New device has no room authority before the CAS.

        with patch.object(BuzzBridgeClient, "provision_private_conversation", return_value={
            "channel_id": self.room,
        }) as provision:
            dm._provision_owner_conversation(self.conversation, required_owner_public_key=new_key)

        self.assertEqual(provision.call_args.kwargs["private_audience"]["channel_id"], self.room)
        self.assertNotIn(self.owner_key, provision.call_args.args[0])
        self.assertIn(new_key, provision.call_args.args[0])
        self.conversation.refresh_from_db()
        self.delivered.refresh_from_db()
        self.checkpoint.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.delivered.metadata["destination_message_id"], "a" * 64)
        self.assertEqual(self.delivered.status, "completed")
        self.assertEqual(self.checkpoint.metadata["cursor"], "page-12")
        self.assertEqual(state.verified_ranges["archive"], {
            **before, "participant_hash": self.conversation.participant_hash,
        })
        catalog = catalog_payload(catalog_conversations(
            SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk),
        ), new_key)
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog[0]["ready_for_display"])

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    def test_cleanup_retains_room_for_retry_after_recovery_error(self, unregister):
        self.prepare_replacement()
        self.conversation.status = "error"
        self.conversation.save(update_fields=["status"])
        ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        self.conversation.refresh_from_db()
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.conversation.status, "error")

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    def test_disabled_stable_protocol_retains_legacy_cleanup(self, unregister):
        self.prepare_replacement()
        with self.settings(MESSAGE_SYNC_STABLE_PRIVATE_ROOMS=False):
            ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.mlai_channel_id)

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    def test_revoked_consent_clears_recovery_pointer(self, unregister):
        self.prepare_replacement()
        self.grant.status = "revoked"
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=["status", "revoked_at"])
        ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.mlai_channel_id)

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    def test_source_retirement_still_erases_recoverable_room(self, unregister):
        self.prepare_replacement()
        dm._retire_ineligible_conversation(
            self.grant.pk, self.conversation.slack_conversation_id,
            reason="synthetic source membership removed",
        )
        ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        self.conversation.refresh_from_db()
        self.delivered.refresh_from_db()
        self.assertIsNone(self.conversation.mlai_channel_id)
        self.assertEqual(self.conversation.status, "paused")
        self.assertEqual(self.conversation.participant_buzz_pubkeys, [])
        self.assertEqual(self.delivered.status, "dead")

    @patch.object(BuzzBridgeClient, "unregister_private_conversation")
    def test_late_provision_cannot_publish_after_consent_is_revoked(self, unregister):
        self.prepare_replacement()
        ledger.reconcile_registration_cleanup(self.grant.pk, raise_on_pending=True)
        self.conversation.refresh_from_db()
        request, _ = self.prepare()
        self.assertEqual(request["private_audience"]["channel_id"], self.room)
        self.grant.status = "revoked"
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=["status", "revoked_at"])
        self.assertFalse(finalize_registration_attempt(request["attempt_id"], channel_id=self.room))
        self.conversation.refresh_from_db()
        self.assertNotEqual(self.conversation.status, "live")

    def publish(self):
        from integrations.services.message_sync.publication import record_publication_locked
        with transaction.atomic():
            self.assertTrue(record_publication_locked(self.conversation))

    def test_publication_follows_successful_device_cas_during_background_refresh(self):
        from integrations.services.slack_chat_catalog import _publication_key, catalog_conversations, catalog_payload

        state = self.coverage()
        self.conversation.latest_synced_ts = str(timezone.now().timestamp())
        self.conversation.save()
        self.publish()
        state.refresh_from_db()
        original = dict(state.verified_ranges["publication"])
        self.conversation.history_backfilled_at = None
        self.conversation.save()
        state.verified_ranges["archive"] = {"classification": "incomplete"}
        state.save()
        request, _ = self.prepare()
        self.assertEqual(request["private_audience"]["publication_proof"], original)
        self.finish(request)
        self.conversation.refresh_from_db()
        state.refresh_from_db()
        self.assertIsNone(self.conversation.history_backfilled_at)
        self.assertEqual(state.verified_ranges["publication"]["published_at"], original["published_at"])
        self.assertEqual(state.verified_ranges["publication"]["scope"], _publication_key(self.conversation))
        entries = catalog_payload(catalog_conversations(SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk)), self.owner_key)
        self.assertTrue(entries[0]["ready_for_display"])

    def test_retry_carries_only_the_frozen_current_publication(self):
        state = self.coverage()
        self.publish()
        state.refresh_from_db()
        original = dict(state.verified_ranges["publication"])
        first, _ = self.prepare()
        second, _ = self.prepare()
        self.assertEqual(second["private_audience"]["publication_proof"], original)
        self.finish(second)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["publication"]["published_at"], original["published_at"])

    def test_reset_winning_before_cas_does_not_resurrect_publication(self):
        from integrations.services.message_sync.publication import invalidate_publication_locked

        state = self.coverage()
        self.publish()
        request, _ = self.prepare()
        with transaction.atomic():
            invalidate_publication_locked(self.conversation)
            self.conversation.history_backfilled_at = None
            self.conversation.save()
        self.finish(request)
        state.refresh_from_db()
        self.assertNotIn("publication", state.verified_ranges)

    def test_same_epoch_consent_change_cannot_carry_frozen_publication(self):
        from integrations.services.message_sync.publication import publication_for_transition, rebind_publication_locked

        state = self.coverage()
        self.publish()
        state.refresh_from_db()
        original = dict(state.verified_ranges["publication"])
        request, _ = self.prepare()
        registration = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.grant.consent_version = "changed-consent"
        self.grant.save()
        self.conversation.grant = self.grant
        with transaction.atomic():
            self.assertIsNone(publication_for_transition(self.conversation))
            rebind_publication_locked(self.conversation, registration)
        state.refresh_from_db()
        self.assertEqual(state.verified_ranges["publication"], original)
