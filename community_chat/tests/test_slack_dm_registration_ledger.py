import hashlib
import threading
import uuid
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
    CommunityBridgePlatform,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services import slack_dm_mirror
from integrations.services import slack_dm_registration_ledger as registration_ledger

SCOPES = [
    "im:read",
    "im:history",
    "im:write",
    "chat:write",
    "users:read",
    "reactions:read",
    "reactions:write",
    "files:read",
    "mpim:read",
    "mpim:history",
    "mpim:write",
]


class SlackDmRegistrationLedgerTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="ledger@example.com")
        self.owner_key = (
            "79be667ef9dcbbac55a06295ce870b070" "29bfcdb2dce28d959f2815b16f81798"
        )
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-ledger",
            scopes=SCOPES,
            external_account_id="TLEDGER",
            provider_metadata={
                "team": {"id": "TLEDGER", "name": "Ledger"},
                "authed_user": {
                    "id": "UOWNER",
                    "scope": ",".join(SCOPES),
                },
            },
        )
        self.grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=self.connection,
            slack_workspace_id="TLEDGER",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="TLEDGER",
            slack_user_id="UOWNER",
            buzz_pubkey=self.owner_key,
            display_name="Owner",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="ledger-test",
            verified_at=timezone.now(),
        )
        self.conversation = SlackDmMirrorConversation.objects.create(
            grant=self.grant,
            slack_workspace_id="TLEDGER",
            slack_conversation_id="DLEDGER",
            participant_slack_ids=["UOWNER", "UOTHER"],
            participant_profiles={
                "UOWNER": {"display_name": "Owner"},
                "UOTHER": {"display_name": "Other"},
            },
        )

    def _prepare_attempt(self):
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            conversation.grant = grant
            request, error = slack_dm_mirror._prepare_owner_conversation_locked(
                conversation,
                force_backfill=False,
                reset_history=False,
                required_owner_public_key=None,
            )
        self.assertIsNone(error)
        self.assertIsNotNone(request)
        return request

    def _make_live_without_adapter(self) -> uuid.UUID:
        shadow_key = "3" * 64
        pubkeys = [self.owner_key, shadow_key]
        participant_hash = hashlib.sha256(
            b"".join(bytes.fromhex(value) for value in sorted(pubkeys))
        ).hexdigest()
        channel_id = uuid.uuid4()
        self.conversation.participant_buzz_pubkeys = sorted(pubkeys)
        self.conversation.participant_identity_map = {
            "UOWNER": self.owner_key,
            "UOTHER": shadow_key,
        }
        self.conversation.participant_hash = participant_hash
        self.conversation.mlai_channel_id = channel_id
        self.conversation.status = SlackDmMirrorConversationStatus.LIVE
        self.conversation.save(
            update_fields=(
                "participant_buzz_pubkeys",
                "participant_identity_map",
                "participant_hash",
                "mlai_channel_id",
                "status",
                "updated_at",
            )
        )
        return channel_id

    def _message_rows(self):
        return SlackDmMirrorDelivery.objects.exclude(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    def test_timeout_persists_exact_attempt_and_retry_adopts_idempotent_result(
        self,
        provision,
    ):
        provision.side_effect = TimeoutError("response lost")

        with self.assertRaises(TimeoutError):
            slack_dm_mirror._provision_owner_conversation(self.conversation)

        attempt = SlackDmMirrorDelivery.objects.get(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX
        )
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_AMBIGUOUS,
        )
        self.assertEqual(attempt.encrypted_text, "")
        self.assertEqual(
            attempt.metadata["consent_generation"],
            self.grant.consented_at.isoformat(),
        )
        self.assertEqual(
            attempt.metadata["participant_pubkeys"],
            sorted(attempt.metadata["participant_pubkeys"]),
        )

        channel_id = uuid.uuid4()
        provision.side_effect = None
        provision.return_value = {"channel_id": str(channel_id)}
        slack_dm_mirror._provision_owner_conversation(self.conversation)

        self.conversation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(self.conversation.mlai_channel_id, channel_id)
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )
        active = SlackDmMirrorDelivery.objects.get(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX,
            metadata__registration_state=registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertNotEqual(active.pk, attempt.pk)

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    def test_stale_phase_two_fence_commits_without_calling_adapter(self, provision):
        request = self._prepare_attempt()
        next_generation = self.grant.consented_at + timedelta(seconds=1)
        SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(
            consented_at=next_generation
        )

        with (
            patch.object(
                slack_dm_mirror,
                "_prepare_owner_conversation_locked",
                return_value=(request, None),
            ),
            patch.object(slack_dm_mirror, "_reconcile_registration_cleanup"),
            patch.object(
                slack_dm_mirror,
                "_registration_cleanup_pending_locked",
                return_value=False,
            ),
            self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError),
        ):
            slack_dm_mirror._provision_owner_conversation(self.conversation)

        provision.assert_not_called()
        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            registration_ledger.registration_state(attempt),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )

    @patch(
        "integrations.services.slack_dm_mirror.BuzzBridgeClient.provision_private_conversation"
    )
    def test_stale_same_intent_caller_cannot_cancel_authoritative_attempt(
        self,
        provision,
    ):
        first_request = self._prepare_attempt()
        second_request = self._prepare_attempt()
        channel_id = str(uuid.uuid4())
        self.assertTrue(
            registration_ledger.finalize_registration_attempt(
                second_request["attempt_id"],
                channel_id=channel_id,
            )
        )

        with (
            patch.object(
                slack_dm_mirror,
                "_prepare_owner_conversation_locked",
                return_value=(first_request, None),
            ),
            self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError),
        ):
            slack_dm_mirror._provision_owner_conversation(self.conversation)

        provision.assert_not_called()
        first = SlackDmMirrorDelivery.objects.get(pk=first_request["attempt_id"])
        second = SlackDmMirrorDelivery.objects.get(pk=second_request["attempt_id"])
        self.conversation.refresh_from_db()
        self.assertEqual(
            registration_ledger.registration_state(first),
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )
        self.assertEqual(
            registration_ledger.registration_state(second),
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertEqual(str(self.conversation.mlai_channel_id), channel_id)
        self.assertEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.LIVE,
        )

    def test_cleanup_post_failure_lease_starts_when_failure_is_observed(self):
        request = self._prepare_attempt()
        attempt_id = request["attempt_id"]
        SlackDmMirrorDelivery.objects.filter(pk=attempt_id).update(
            updated_at=timezone.now() - timedelta(minutes=10)
        )

        registration_ledger._record_registration_cleanup_failure(
            self.grant.pk,
            attempt_id,
            TimeoutError("response lost"),
            operation="resolve",
        )

        attempt = SlackDmMirrorDelivery.objects.get(pk=attempt_id)
        self.assertGreater(
            attempt.available_at,
            timezone.now() + timedelta(minutes=4),
        )

    def test_first_cleanup_lease_is_not_anchored_to_aged_attempt_creation(self):
        request = self._prepare_attempt()
        SlackDmMirrorDelivery.objects.filter(pk=request["attempt_id"]).update(
            updated_at=timezone.now() - timedelta(minutes=10)
        )
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversations = list(
                SlackDmMirrorConversation.objects.select_for_update().filter(
                    grant=grant
                )
            )
            registration_ledger.prepare_registration_cleanup_locked(
                grant,
                conversations,
                reason="process crashed after starting adapter POST",
            )

        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            registration_ledger.registration_state(attempt),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(
            attempt.available_at,
            timezone.now() + timedelta(minutes=4),
        )

    def test_cleanup_scan_does_not_starve_due_grant_behind_future_leases(self):
        now = timezone.now()
        self.grant.status = SlackDmMirrorGrantStatus.REVOKED
        self.grant.revoked_at = now
        self.grant.save(update_fields=("status", "revoked_at", "updated_at"))
        SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id=(
                f"{registration_ledger.REGISTRATION_STATE_PREFIX}{uuid.uuid4().hex}"
            ),
            source_author_id="",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="",
            metadata={
                "registration_control": True,
                "registration_state": (
                    registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING
                ),
            },
            status=CommunityBridgeDeliveryStatus.PENDING,
            available_at=now - timedelta(seconds=1),
        )
        for index in range(10):
            connection_record = ExternalServiceConnection.objects.create(
                user=self.user,
                provider=ExternalServiceProvider.SLACK,
                status=ExternalServiceConnectionStatus.DISCONNECTED,
                external_account_id=f"TFUTURE{index}",
            )
            grant = SlackDmMirrorGrant.objects.create(
                user=self.user,
                connection=connection_record,
                slack_workspace_id=f"TFUTURE{index}",
                slack_user_id=f"UFUTURE{index}",
                status=SlackDmMirrorGrantStatus.REVOKED,
                consented_at=now,
                revoked_at=now,
            )
            conversation = SlackDmMirrorConversation.objects.create(
                grant=grant,
                slack_workspace_id=grant.slack_workspace_id,
                slack_conversation_id=f"DFUTURE{index}",
            )
            SlackDmMirrorDelivery.objects.create(
                conversation=conversation,
                source_platform=CommunityBridgePlatform.BUZZ,
                source_message_id=(
                    f"{registration_ledger.REGISTRATION_STATE_PREFIX}"
                    f"{uuid.uuid4().hex}"
                ),
                source_author_id="",
                operation=CommunityBridgeDeliveryType.CREATE,
                encrypted_text="",
                metadata={
                    "registration_control": True,
                    "registration_state": (
                        registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING
                    ),
                },
                status=CommunityBridgeDeliveryStatus.PENDING,
                available_at=now + timedelta(minutes=5),
            )

        slack_dm_mirror._last_grant_discovery_scan = 0.0
        with patch.object(
            slack_dm_mirror,
            "_reconcile_registration_cleanup",
        ) as reconcile:
            slack_dm_mirror.discover_grants_if_due()

        reconcile.assert_called_once_with(
            self.grant.pk,
            raise_on_pending=False,
        )

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_disconnect_bounds_large_cleanup_and_leaves_every_remainder_retryable(
        self,
        unregister,
    ):
        now = timezone.now()
        channel_ids = [str(uuid.uuid4()) for _ in range(101)]
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(
                pk=self.grant.pk
            )
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            grant.status = SlackDmMirrorGrantStatus.REVOKED
            grant.revoked_at = now
            grant.save(update_fields=("status", "revoked_at", "updated_at"))
            conversation.status = SlackDmMirrorConversationStatus.PAUSED
            conversation.mlai_channel_id = None
            conversation.save(
                update_fields=("status", "mlai_channel_id", "updated_at")
            )
            for channel_id in channel_ids:
                row = registration_ledger.create_registration_row_locked(
                    conversation,
                    grant=grant,
                    state=registration_ledger.REGISTRATION_STATE_ACTIVE,
                    participant_pubkeys=[],
                    callback_author_pubkeys=[],
                    participant_hash=channel_id,
                    conversation_name_value="Retired DM",
                    channel_id=channel_id,
                    provision_attempt=False,
                )
                registration_ledger.mark_registration_cleanup_pending_locked(
                    row,
                    reason="Consent revoked",
                    available_at=now,
                )
            registration_ledger.update_registration_cleanup_summary_locked(grant)

        slack_dm_mirror._finish_grant_registration_revoke(self.grant.pk)

        self.assertEqual(unregister.call_count, 1)
        with transaction.atomic():
            self.assertTrue(
                registration_ledger.registration_cleanup_pending_locked(
                    self.grant.pk
                )
            )
        self.grant.refresh_from_db()
        self.assertEqual(
            self.grant.last_error,
            registration_ledger.PRIVATE_REGISTRATION_REVOCATION_PENDING,
        )

        slack_dm_mirror._reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=False,
            limit=100,
        )

        self.assertEqual(unregister.call_count, 101)
        with transaction.atomic():
            self.assertFalse(
                registration_ledger.registration_cleanup_pending_locked(
                    self.grant.pk
                )
            )
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.last_error, "")

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.provision_private_conversation"
    )
    def test_crashed_provision_is_not_cleaned_before_its_network_lease_expires(
        self,
        provision,
        unregister,
    ):
        request = self._prepare_attempt()
        now = timezone.now()
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            grant.status = SlackDmMirrorGrantStatus.REVOKED
            grant.revoked_at = now
            grant.save(update_fields=("status", "revoked_at", "updated_at"))
            conversation.status = SlackDmMirrorConversationStatus.PAUSED
            conversation.save(update_fields=("status", "updated_at"))
            registration_ledger.prepare_registration_cleanup_locked(
                grant,
                [conversation],
                reason="Consent revoked while registration POST was in flight",
            )

        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(attempt.available_at, now + timedelta(minutes=4))
        registration_ledger.reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=False,
        )
        provision.assert_not_called()
        unregister.assert_not_called()

        first_available_at = attempt.available_at
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            registration_ledger.prepare_registration_cleanup_locked(
                grant,
                [conversation],
                reason="Repeated revoke must preserve the original network lease",
            )
        attempt.refresh_from_db()
        self.assertEqual(attempt.available_at, first_available_at)

        channel_id = uuid.uuid4()
        provision.return_value = {"channel_id": str(channel_id)}
        SlackDmMirrorDelivery.objects.filter(pk=attempt.pk).update(
            available_at=timezone.now()
        )
        registration_ledger.reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=True,
        )

        provision.assert_called_once_with(
            request["participant_pubkeys"],
            callback_author_pubkeys=request["callback_author_pubkeys"],
            conversation_name=request["conversation_name"],
        )
        unregister.assert_called_once_with(str(channel_id))
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.provision_private_conversation"
    )
    def test_delayed_original_post_cannot_resurrect_a_cleaned_attempt(
        self,
        provision,
        unregister,
    ):
        request = self._prepare_attempt()
        channel_id = uuid.uuid4()
        provision.return_value = {"channel_id": str(channel_id)}
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            attempt = SlackDmMirrorDelivery.objects.select_for_update().get(
                pk=request["attempt_id"]
            )
            registration_ledger.mark_registration_cleanup_pending_locked(
                attempt,
                reason="Superseded while the original POST was in flight",
                available_at=timezone.now(),
            )
        SlackDmMirrorDelivery.objects.filter(pk=request["attempt_id"]).update(
            available_at=timezone.now()
        )
        registration_ledger.reconcile_registration_cleanup(
            grant.pk,
            raise_on_pending=True,
        )

        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )
        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                request["attempt_id"],
                channel_id=str(channel_id),
            )
        )

        attempt.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertIn(
            attempt.metadata["registration_state"],
            {
                registration_ledger.REGISTRATION_STATE_CLEANED,
                registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
            },
        )
        self.assertNotEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertNotEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.LIVE,
        )
        self.assertIsNone(self.conversation.mlai_channel_id)
        unregister.assert_called_once_with(str(channel_id))

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_expired_colliding_attempt_is_fenced_before_old_channel_delete(
        self,
        unregister,
    ):
        channel_id = self._make_live_without_adapter()
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            conversation.grant = grant
            registration_ledger.ensure_current_registration_row_locked(
                conversation,
                grant,
            )
            old = SlackDmMirrorDelivery.objects.get(
                conversation=conversation,
                metadata__registration_state=(
                    registration_ledger.REGISTRATION_STATE_ACTIVE
                ),
            )
            conversation.status = SlackDmMirrorConversationStatus.PROVISIONING
            conversation.mlai_channel_id = None
            conversation.save(update_fields=("status", "mlai_channel_id", "updated_at"))
            registration_ledger.mark_registration_cleanup_pending_locked(
                old,
                reason="Old same-semantic registration was superseded",
                channel_id=str(channel_id),
                available_at=timezone.now(),
            )
            newer = registration_ledger.create_registration_row_locked(
                conversation,
                grant=grant,
                state=registration_ledger.REGISTRATION_STATE_PROVISIONING,
                participant_pubkeys=list(conversation.participant_buzz_pubkeys),
                callback_author_pubkeys=[self.owner_key],
                participant_hash=conversation.participant_hash,
                conversation_name_value="Other",
                provision_attempt=True,
            )
        SlackDmMirrorDelivery.objects.filter(pk=newer.pk).update(
            updated_at=timezone.now()
            - timedelta(
                seconds=registration_ledger.REGISTRATION_CLEANUP_LEASE_SECONDS + 1
            )
        )

        claim = registration_ledger._claim_registration_cleanup(self.grant.pk)
        self.assertEqual(claim["row_id"], old.pk)
        self.assertEqual(
            registration_ledger._execute_registration_cleanup(claim),
            "cleaned",
        )
        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                newer.pk,
                channel_id=str(channel_id),
            )
        )

        newer.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertIn(
            newer.metadata["registration_state"],
            {
                registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
                registration_ledger.REGISTRATION_STATE_CLEANED,
            },
        )
        self.assertNotEqual(
            newer.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertNotEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.LIVE,
        )
        unregister.assert_called_once_with(str(channel_id))

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_out_of_order_device_replacement_only_keeps_latest_attempt(
        self,
        unregister,
    ):
        first = self._prepare_attempt()
        replacement_key = "4" * 64
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=replacement_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        second = self._prepare_attempt()
        first_channel = uuid.uuid4()
        second_channel = uuid.uuid4()

        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                first["attempt_id"],
                channel_id=str(first_channel),
            )
        )
        self.assertTrue(
            registration_ledger.finalize_registration_attempt(
                second["attempt_id"],
                channel_id=str(second_channel),
            )
        )
        registration_ledger.reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=True,
        )

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.mlai_channel_id, second_channel)
        self.assertEqual(self.conversation.status, SlackDmMirrorConversationStatus.LIVE)
        unregister.assert_called_once_with(str(first_channel))
        first_row = SlackDmMirrorDelivery.objects.get(pk=first["attempt_id"])
        second_row = SlackDmMirrorDelivery.objects.get(pk=second["attempt_id"])
        self.assertEqual(
            first_row.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )
        self.assertEqual(
            second_row.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertNotEqual(
            first_row.metadata["participant_pubkeys"],
            second_row.metadata["participant_pubkeys"],
        )

    def test_discovery_fences_old_membership_before_profile_network_io(self):
        old_attempt = self._prepare_attempt()
        old_channel = uuid.uuid4()
        self.conversation.status = SlackDmMirrorConversationStatus.LIVE
        self.conversation.mlai_channel_id = old_channel
        self.conversation.save(
            update_fields=("status", "mlai_channel_id", "updated_at")
        )
        callback_results = []

        def profile_lookup_fails_after_callback(*_args, **_kwargs):
            callback_results.append(
                slack_dm_mirror.ingest_slack_dm_event(
                    {
                        "team_id": "TLEDGER",
                        "authorizations": [{"user_id": "UOWNER"}],
                        "event": {
                            "channel": "DLEDGER",
                            "ts": "1787900003.000100",
                            "user": "UNEW",
                            "text": "must not enter the old private channel",
                        },
                    }
                )
            )
            raise RuntimeError("profile lookup failed")

        client = MagicMock()
        with patch(
            "integrations.services.slack_dm_mirror._slack_profile",
            side_effect=profile_lookup_fails_after_callback,
        ):
            with self.assertRaisesRegex(RuntimeError, "profile lookup failed"):
                slack_dm_mirror._discover_conversation(
                    self.grant,
                    client,
                    {"id": "DLEDGER", "user": "UNEW"},
                    profile_cache={},
                    force_backfill=False,
                    reset_history=False,
                )

        self.assertEqual(
            callback_results,
            [{"status": "discovery_queued", "staged": 1}],
        )
        self.assertFalse(self._message_rows().exists())
        self.conversation.refresh_from_db()
        self.assertEqual(
            self.conversation.participant_slack_ids,
            ["UNEW", "UOWNER"],
        )
        self.assertNotEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.LIVE,
        )
        self.assertIsNone(self.conversation.mlai_channel_id)
        returned_channel = uuid.uuid4()
        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                old_attempt["attempt_id"],
                channel_id=str(returned_channel),
            )
        )
        old_row = SlackDmMirrorDelivery.objects.get(pk=old_attempt["attempt_id"])
        self.assertNotEqual(
            old_row.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.provision_private_conversation"
    )
    def test_stale_processing_cleanup_restores_same_current_registration(
        self,
        provision,
        unregister,
    ):
        channel_id = self._make_live_without_adapter()
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(pk=self.grant.pk)
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            conversation.grant = grant
            registration_ledger.ensure_current_registration_row_locked(
                conversation,
                grant,
            )
            stale = registration_ledger.create_registration_row_locked(
                conversation,
                grant=grant,
                state=registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
                participant_pubkeys=list(conversation.participant_buzz_pubkeys),
                callback_author_pubkeys=[self.owner_key],
                participant_hash=conversation.participant_hash,
                conversation_name_value="Other",
                channel_id=str(channel_id),
                provision_attempt=True,
            )
            registration_ledger.save_registration_state_locked(
                stale,
                registration_ledger.REGISTRATION_STATE_CLEANUP_PROCESSING,
                channel_id=str(channel_id),
            )
        SlackDmMirrorDelivery.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now()
            - timedelta(
                seconds=registration_ledger.REGISTRATION_CLEANUP_LEASE_SECONDS + 1
            )
        )
        provision.return_value = {"channel_id": str(channel_id)}

        self.assertIsNone(
            registration_ledger._claim_registration_cleanup(self.grant.pk)
        )
        SlackDmMirrorDelivery.objects.filter(pk=stale.pk).update(
            available_at=timezone.now() - timedelta(seconds=1)
        )
        claim = registration_ledger._claim_registration_cleanup(self.grant.pk)
        self.assertIsNotNone(
            claim,
            list(
                SlackDmMirrorDelivery.objects.values(
                    "id", "status", "metadata", "updated_at"
                )
            ),
        )
        self.assertEqual(
            registration_ledger._execute_registration_cleanup(claim),
            "owned",
        )
        registration_ledger.reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=True,
        )

        stale.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertEqual(
            stale.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANED,
        )
        self.assertEqual(self.conversation.mlai_channel_id, channel_id)
        provision.assert_called_once()
        unregister.assert_not_called()

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_sibling_cleanup_cannot_erase_timed_out_channel_lease(self, unregister):
        channel_id = self._make_live_without_adapter()
        self.grant.status = SlackDmMirrorGrantStatus.REVOKED
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=("status", "revoked_at", "updated_at"))
        self.conversation.status = SlackDmMirrorConversationStatus.PAUSED
        self.conversation.save(update_fields=("status", "updated_at"))
        with transaction.atomic():
            grant = SlackDmMirrorGrant.objects.select_for_update().get(
                pk=self.grant.pk
            )
            conversation = SlackDmMirrorConversation.objects.select_for_update().get(
                pk=self.conversation.pk
            )
            rows = [
                registration_ledger.create_registration_row_locked(
                    conversation,
                    grant=grant,
                    state=registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
                    participant_pubkeys=list(conversation.participant_buzz_pubkeys),
                    callback_author_pubkeys=[self.owner_key],
                    participant_hash=conversation.participant_hash,
                    conversation_name_value="Other",
                    channel_id=str(channel_id),
                    provision_attempt=True,
                )
                for _ in range(2)
            ]
            for row in rows:
                registration_ledger.save_registration_state_locked(
                    row,
                    registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
                    channel_id=str(channel_id),
                    available_at=timezone.now(),
                )

        unregister.side_effect = TimeoutError("DELETE response lost")
        first_claim = registration_ledger._claim_registration_cleanup(self.grant.pk)
        with self.assertRaises(TimeoutError):
            registration_ledger._execute_registration_cleanup(first_claim)

        unregister.side_effect = None
        second_claim = registration_ledger._claim_registration_cleanup(self.grant.pk)
        self.assertIsNotNone(second_claim)
        self.assertEqual(
            registration_ledger._execute_registration_cleanup(second_claim),
            "cleaned",
        )

        timed_out = SlackDmMirrorDelivery.objects.get(pk=first_claim["row_id"])
        self.assertEqual(
            registration_ledger.registration_state(timed_out),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(
            timed_out.available_at,
            timezone.now() + timedelta(minutes=4),
        )
        with self.assertRaises(slack_dm_mirror.SlackDmMirrorError):
            slack_dm_mirror.activate_connection(self.connection)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_reactivation_is_rejected_while_delete_is_in_flight(
        self,
        unregister,
        web_client,
    ):
        self._make_live_without_adapter()
        unregister.side_effect = RuntimeError("adapter unavailable")
        slack_dm_mirror.revoke_grant(self.grant)

        self.connection.status = ExternalServiceConnectionStatus.CONNECTED
        self.connection.access_token = "xoxp-new-consent"
        self.connection.scopes = SCOPES
        self.connection.provider_metadata = {
            "team": {"id": "TLEDGER", "name": "Ledger"},
            "authed_user": {
                "id": "UOWNER",
                "scope": ",".join(SCOPES),
            },
        }
        self.connection.save(
            update_fields=(
                "status",
                "access_token",
                "scopes",
                "provider_metadata",
                "updated_at",
            )
        )
        with self.assertRaises(slack_dm_mirror.SlackDmMirrorError):
            slack_dm_mirror.activate_connection(self.connection)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)

        SlackDmMirrorDelivery.objects.filter(
            source_message_id__startswith=registration_ledger.REGISTRATION_STATE_PREFIX
        ).update(available_at=timezone.now())
        nested_activation_attempted = False

        def try_reactivation_during_delete(channel_id):
            nonlocal nested_activation_attempted
            nested_activation_attempted = True
            with self.assertRaises(slack_dm_mirror.SlackDmMirrorError):
                slack_dm_mirror.activate_connection(self.connection)

        unregister.side_effect = try_reactivation_during_delete
        slack_dm_mirror._reconcile_registration_cleanup(
            self.grant.pk,
            raise_on_pending=True,
        )

        self.assertTrue(nested_activation_attempted)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        previous_generation = self.grant.consented_at

        reactivated = slack_dm_mirror.activate_connection(self.connection)

        self.assertEqual(reactivated.status, SlackDmMirrorGrantStatus.ACTIVE)
        self.assertGreater(reactivated.consented_at, previous_generation)
        self.assertFalse(
            SlackDmMirrorDelivery.objects.filter(
                source_message_id__startswith=(
                    registration_ledger.REGISTRATION_STATE_PREFIX
                ),
                status__in=(
                    CommunityBridgeDeliveryStatus.PENDING,
                    CommunityBridgeDeliveryStatus.PROCESSING,
                ),
            ).exists()
        )
        web_client.return_value.auth_revoke.assert_called_once_with()

    def test_activation_final_lock_fences_new_ambiguous_prior_attempt(self):
        request = self._prepare_attempt()
        original_generation = self.grant.consented_at

        def become_ambiguous_after_preflight(*_args, **_kwargs):
            registration_ledger.record_ambiguous_registration_attempt(
                request["attempt_id"],
                TimeoutError("old POST response was lost"),
            )

        with (
            patch.object(
                slack_dm_mirror,
                "_complete_registration_cleanup_before_activation",
                side_effect=become_ambiguous_after_preflight,
            ),
            self.assertRaises(slack_dm_mirror.SlackDmMirrorError),
        ):
            slack_dm_mirror.activate_connection(self.connection)

        self.grant.refresh_from_db()
        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(self.grant.consented_at, original_generation)
        self.assertEqual(
            registration_ledger.registration_state(attempt),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(
            attempt.available_at,
            timezone.now() + timedelta(minutes=4),
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_identity_repair_rejects_a_revoked_grant(
        self,
        unregister,
        web_client,
    ):
        slack_dm_mirror.revoke_grant(self.grant)

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError):
            slack_dm_mirror.ensure_owner_identity(
                self.grant,
                allow_preferred_fallback=True,
            )

        link = CommunityBridgeIdentityLink.objects.get(
            slack_workspace_id="TLEDGER",
            slack_user_id="UOWNER",
        )
        self.assertIsNotNone(link.revoked_at)
        self.assertEqual(link.buzz_pubkey, self.owner_key)
        web_client.return_value.auth_revoke.assert_called_once_with()

    def test_identity_repair_fences_an_in_flight_old_device_attempt(self):
        request = self._prepare_attempt()
        old_device = CommunityChatDevice.objects.get(
            user=self.user,
            public_key=self.owner_key,
        )
        old_device.status = DeviceBindingStatus.REVOKED
        old_device.revoked_at = timezone.now()
        old_device.save(update_fields=("status", "revoked_at", "updated_at"))
        replacement_key = "5" * 64
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=replacement_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )

        link, repaired, _ = slack_dm_mirror.ensure_owner_identity(
            self.grant,
            authenticated_public_key=replacement_key,
        )
        self.assertTrue(repaired)
        self.assertEqual(link.buzz_pubkey, replacement_key)
        returned_channel = uuid.uuid4()
        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                request["attempt_id"],
                channel_id=str(returned_channel),
            )
        )

        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.conversation.refresh_from_db()
        self.assertNotEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_ACTIVE,
        )
        self.assertNotEqual(
            self.conversation.status,
            SlackDmMirrorConversationStatus.LIVE,
        )
        self.assertIsNone(self.conversation.mlai_channel_id)

    def test_first_activation_cannot_reuse_a_connection_after_disconnect(self):
        fresh_user = get_user_model().objects.create_user(email="fresh@example.com")
        CommunityChatDevice.objects.create(
            user=fresh_user,
            public_key="6" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        fresh_connection = ExternalServiceConnection.objects.create(
            user=fresh_user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-fresh",
            scopes=SCOPES,
            external_account_id="TFRESH",
            provider_metadata={
                "team": {"id": "TFRESH", "name": "Fresh"},
                "authed_user": {"id": "UFRESH", "scope": ",".join(SCOPES)},
            },
        )
        stale_connection = ExternalServiceConnection.objects.get(pk=fresh_connection.pk)

        slack_dm_mirror.revoke_user_grant(fresh_user)

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorError):
            slack_dm_mirror.activate_connection(stale_connection)
        self.assertFalse(SlackDmMirrorGrant.objects.filter(user=fresh_user).exists())

    def test_existing_slack_identity_cannot_be_reassigned_to_another_account(self):
        other_user = get_user_model().objects.create_user(email="other@example.com")
        CommunityChatDevice.objects.create(
            user=other_user,
            public_key="7" * 64,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        other_connection = ExternalServiceConnection.objects.create(
            user=other_user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-other",
            scopes=SCOPES,
            external_account_id="TLEDGER",
            provider_metadata={
                "team": {"id": "TLEDGER", "name": "Ledger"},
                "authed_user": {"id": "UOWNER", "scope": ",".join(SCOPES)},
            },
        )

        with self.assertRaisesRegex(
            slack_dm_mirror.SlackDmMirrorError,
            "already linked",
        ):
            slack_dm_mirror.activate_connection(other_connection)

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.user_id, self.user.pk)
        self.assertEqual(self.grant.connection_id, self.connection.pk)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_global_disconnect_revokes_every_user_slack_grant(self, web_client):
        second_connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-second",
            scopes=SCOPES,
            external_account_id="TSECOND",
            provider_metadata={
                "team": {"id": "TSECOND", "name": "Second"},
                "authed_user": {"id": "USECOND", "scope": ",".join(SCOPES)},
            },
        )
        second_grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=second_connection,
            slack_workspace_id="TSECOND",
            slack_user_id="USECOND",
            consented_at=timezone.now(),
        )

        slack_dm_mirror.revoke_user_grant(self.user)

        self.grant.refresh_from_db()
        second_grant.refresh_from_db()
        self.connection.refresh_from_db()
        second_connection.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(second_grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(
            self.connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(
            second_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(web_client.return_value.auth_revoke.call_count, 2)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_connection_disconnect_cannot_leave_a_hidden_active_grant(self, web_client):
        second_connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-second",
            scopes=SCOPES,
            external_account_id="TSECOND",
            provider_metadata={
                "team": {"id": "TSECOND", "name": "Second"},
                "authed_user": {"id": "USECOND", "scope": ",".join(SCOPES)},
            },
        )
        second_grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=second_connection,
            slack_workspace_id="TSECOND",
            slack_user_id="USECOND",
            consented_at=timezone.now(),
        )

        slack_dm_mirror.revoke_connection_grant(
            self.user,
            self.connection.pk,
        )

        self.grant.refresh_from_db()
        second_grant.refresh_from_db()
        self.connection.refresh_from_db()
        second_connection.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(second_grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(
            self.connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(
            second_connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(web_client.return_value.auth_revoke.call_count, 2)

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_slack_callback_rechecks_grant_after_candidate_lookup(
        self,
        unregister,
        web_client,
    ):
        self._make_live_without_adapter()
        real_select_for_update = SlackDmMirrorGrant.objects.select_for_update
        revoked = False

        def revoke_before_locked_lookup(*args, **kwargs):
            nonlocal revoked
            if not revoked:
                revoked = True
                slack_dm_mirror.revoke_grant(self.grant)
            return real_select_for_update(*args, **kwargs)

        with patch.object(
            SlackDmMirrorGrant.objects,
            "select_for_update",
            side_effect=revoke_before_locked_lookup,
        ):
            result = slack_dm_mirror.ingest_slack_dm_event(
                {
                    "team_id": "TLEDGER",
                    "event": {
                        "channel": "DLEDGER",
                        "ts": "1787900000.000100",
                        "user": "UOTHER",
                        "text": "must not persist after revoke wins",
                    },
                }
            )

        self.assertEqual(result["status"], "ignored")
        self.assertFalse(self._message_rows().exists())
        self.assertTrue(
            all(row.encrypted_text == "" for row in SlackDmMirrorDelivery.objects.all())
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_mlai_callback_rechecks_grant_after_channel_lookup(
        self,
        unregister,
        web_client,
    ):
        channel_id = self._make_live_without_adapter()
        real_select_for_update = SlackDmMirrorGrant.objects.select_for_update
        revoked = False

        def revoke_before_locked_lookup(*args, **kwargs):
            nonlocal revoked
            if not revoked:
                revoked = True
                slack_dm_mirror.revoke_grant(self.grant)
            return real_select_for_update(*args, **kwargs)

        with patch.object(
            SlackDmMirrorGrant.objects,
            "select_for_update",
            side_effect=revoke_before_locked_lookup,
        ):
            result = slack_dm_mirror.ingest_mlai_dm_event(
                {
                    "source_channel_id": str(channel_id),
                    "normalized_event": {
                        "delivery_type": CommunityBridgeDeliveryType.CREATE,
                        "source_message_id": "a" * 64,
                        "source_author_id": self.owner_key,
                        "text": "must not persist after revoke wins",
                    },
                }
            )

        self.assertEqual(result["status"], "ignored")
        self.assertFalse(self._message_rows().exists())
        self.assertTrue(
            all(row.encrypted_text == "" for row in SlackDmMirrorDelivery.objects.all())
        )

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_history_response_after_revoke_cannot_persist_a_body(
        self,
        unregister,
        web_client,
    ):
        self._make_live_without_adapter()

        def revoke_before_history_returns(**kwargs):
            slack_dm_mirror.revoke_grant(self.grant)
            return {
                "messages": [
                    {
                        "ts": "1787900000.000100",
                        "user": "UOTHER",
                        "text": "must not persist after revoke wins",
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            }

        web_client.return_value.conversations_history.side_effect = (
            revoke_before_history_returns
        )

        self.assertEqual(slack_dm_mirror.process_due_history_backfills(), 0)
        self.assertFalse(
            self._message_rows()
            .filter(source_message_id="1787900000.000100")
            .exists()
        )
        self.assertTrue(
            all(row.encrypted_text == "" for row in SlackDmMirrorDelivery.objects.all())
        )

    @patch("integrations.services.slack_dm_mirror._deliver_private")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_failed_stale_delivery_claim_cannot_restore_body_after_revoke(
        self,
        unregister,
        web_client,
        deliver_private,
    ):
        self._make_live_without_adapter()
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id="1787900001.000100",
            source_author_id="UOTHER",
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="must stay erased after revoke",
            metadata={"participant_hash": self.conversation.participant_hash},
            available_at=timezone.now(),
        )

        def revoke_then_fail(_claimed_delivery):
            slack_dm_mirror.revoke_grant(self.grant)
            raise RuntimeError("network response arrived after revoke")

        deliver_private.side_effect = revoke_then_fail

        self.assertEqual(slack_dm_mirror.process_ready_deliveries(limit=1), 0)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        unregister.assert_called_once()
        web_client.return_value.auth_revoke.assert_called_once_with()

    @patch("integrations.services.slack_dm_mirror.WebClient")
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    def test_mlai_delivery_cannot_reach_slack_after_revoke_wins(
        self,
        unregister,
        web_client,
    ):
        self._make_live_without_adapter()
        delivery = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="b" * 64,
            source_author_id=self.owner_key,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="must never reach Slack",
            metadata={"participant_hash": self.conversation.participant_hash},
            available_at=timezone.now(),
        )
        slack_dm_mirror.revoke_grant(self.grant)
        stale_claim = SlackDmMirrorDelivery.objects.select_related(
            "conversation__grant__connection"
        ).get(pk=delivery.pk)

        with self.assertRaises(slack_dm_mirror.SlackDmMirrorAuthorizationError):
            slack_dm_mirror._deliver_private(stale_claim)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(delivery.encrypted_text, "")
        web_client.return_value.chat_postMessage.assert_not_called()
        unregister.assert_called_once()

    def test_device_authority_revoke_fences_a_delayed_post_with_full_lease(self):
        request = self._prepare_attempt()
        from community_chat.device_revocation import revoke_device_authority

        revoked = revoke_device_authority(
            self.user,
            device_id=CommunityChatDevice.objects.get(
                user=self.user,
                public_key=self.owner_key,
            ).pk,
            public_key=self.owner_key,
            reason="lost device",
        )

        self.assertIsNotNone(revoked)
        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            attempt.metadata["registration_state"],
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(
            attempt.available_at,
            timezone.now() + timedelta(minutes=4),
        )
        self.assertFalse(
            registration_ledger.finalize_registration_attempt(
                attempt.pk,
                channel_id=str(uuid.uuid4()),
            )
        )

    def test_device_revoke_finds_old_registration_after_participant_replacement(self):
        request = self._prepare_attempt()
        self.conversation.refresh_from_db()
        replacement_key = "6" * 64
        CommunityChatDevice.objects.create(
            user=self.user,
            public_key=replacement_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        shadow_key = next(
            key
            for key in self.conversation.participant_buzz_pubkeys
            if key != self.owner_key
        )
        replacement_pubkeys = sorted([replacement_key, shadow_key])
        self.conversation.participant_buzz_pubkeys = replacement_pubkeys
        self.conversation.participant_identity_map = {
            "UOWNER": replacement_key,
            "UOTHER": shadow_key,
        }
        self.conversation.participant_hash = hashlib.sha256(
            b"".join(bytes.fromhex(value) for value in replacement_pubkeys)
        ).hexdigest()
        self.conversation.save(
            update_fields=(
                "participant_buzz_pubkeys",
                "participant_identity_map",
                "participant_hash",
                "updated_at",
            )
        )
        from community_chat.device_revocation import revoke_device_authority

        revoked = revoke_device_authority(
            self.user,
            device_id=CommunityChatDevice.objects.get(
                user=self.user,
                public_key=self.owner_key,
            ).pk,
            public_key=self.owner_key,
            reason="replaced device",
        )

        self.assertEqual(
            revoked.registration_cleanup_grant_ids,
            (self.grant.pk,),
        )
        attempt = SlackDmMirrorDelivery.objects.get(pk=request["attempt_id"])
        self.assertEqual(
            registration_ledger.registration_state(attempt),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )

    def test_locked_device_revoke_prefers_active_same_key_over_stale_row_id(self):
        old_device = CommunityChatDevice.objects.get(
            user=self.user,
            public_key=self.owner_key,
        )
        old_device.status = DeviceBindingStatus.REVOKED
        old_device.revoked_at = timezone.now() - timedelta(days=1)
        old_device.save(update_fields=("status", "revoked_at", "updated_at"))
        current_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        from community_chat.device_revocation import revoke_device_authority

        relay_revoke = MagicMock(return_value=("revoked", uuid.uuid4()))
        revoked = revoke_device_authority(
            self.user,
            device_id=old_device.pk,
            public_key=self.owner_key,
            reason="stale caller",
            allow_already_revoked=True,
            revoke_relay_membership_callback=relay_revoke,
        )

        self.assertEqual(revoked.pk, current_device.pk)
        current_device.refresh_from_db()
        self.assertEqual(current_device.status, DeviceBindingStatus.REVOKED)
        relay_revoke.assert_called_once_with(self.owner_key)

    @override_settings(
        COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.provision_private_conversation"
    )
    @patch("community_chat.views.revoke_relay_membership")
    def test_device_delete_clears_queued_body_and_blocks_future_callback(
        self,
        revoke_membership,
        provision,
        unregister,
    ):
        channel_id = self._make_live_without_adapter()
        queued = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="d" * 64,
            source_author_id=self.owner_key,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="erase this queued body",
            metadata={"participant_hash": self.conversation.participant_hash},
            available_at=timezone.now(),
        )
        revoke_membership.return_value = ("revoked", uuid.uuid4())
        provision.return_value = {"channel_id": str(uuid.uuid4())}
        self.client.force_authenticate(user=self.user)

        response = self.client.delete(
            reverse("community_chat_device", args=(self.owner_key,)),
            {"reason": "lost device"},
            format="json",
            HTTP_ORIGIN="https://chat.mlai.au",
        )

        self.assertEqual(response.status_code, 200)
        queued.refresh_from_db()
        self.assertEqual(queued.status, CommunityBridgeDeliveryStatus.DEAD)
        self.assertEqual(queued.encrypted_text, "")
        result = slack_dm_mirror.ingest_mlai_dm_event(
            {
                "source_channel_id": str(channel_id),
                "normalized_event": {
                    "delivery_type": CommunityBridgeDeliveryType.CREATE,
                    "source_message_id": "e" * 64,
                    "source_author_id": self.owner_key,
                    "text": "must not persist after device revoke",
                },
            }
        )
        self.assertEqual(result, {"status": "ignored"})
        self.assertFalse(
            self._message_rows()
            .exclude(pk=queued.pk)
            .filter(encrypted_text__gt="")
            .exists()
        )
        self.assertGreaterEqual(unregister.call_count, 1)

    @override_settings(
        COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    )
    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("community_chat.views.revoke_relay_membership")
    def test_device_delete_succeeds_with_durable_async_cleanup_and_is_idempotent(
        self,
        revoke_membership,
        unregister,
    ):
        self._make_live_without_adapter()
        revoke_membership.return_value = ("revoked", uuid.uuid4())
        unregister.side_effect = RuntimeError("adapter unavailable")
        self.client.force_authenticate(user=self.user)
        url = reverse("community_chat_device", args=(self.owner_key,))

        first_response = self.client.delete(
            url,
            {"reason": "lost device"},
            format="json",
            HTTP_ORIGIN="https://chat.mlai.au",
        )

        self.assertEqual(first_response.status_code, 200)
        device = CommunityChatDevice.objects.get(
            user=self.user,
            public_key=self.owner_key,
        )
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        pending = SlackDmMirrorDelivery.objects.get(
            source_message_id__startswith=(
                registration_ledger.REGISTRATION_STATE_PREFIX
            )
        )
        self.assertEqual(
            registration_ledger.registration_state(pending),
            registration_ledger.REGISTRATION_STATE_CLEANUP_PENDING,
        )
        self.assertGreater(
            pending.available_at,
            timezone.now() + timedelta(minutes=4),
        )

        unregister.side_effect = None
        second_response = self.client.delete(
            url,
            {"reason": "lost device"},
            format="json",
            HTTP_ORIGIN="https://chat.mlai.au",
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.data["relay_status"], "already_revoked")
        self.assertEqual(revoke_membership.call_count, 2)
        revoke_membership.assert_called_with(self.owner_key)
        unregister.assert_called_once()

    @override_settings(
        COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    )
    @patch("community_chat.views.revoke_relay_membership")
    def test_device_delete_prefers_reenrolled_active_binding_over_revoked_history(
        self,
        revoke_membership,
    ):
        old_device = CommunityChatDevice.objects.get(
            user=self.user,
            public_key=self.owner_key,
        )
        old_device.status = DeviceBindingStatus.REVOKED
        old_device.revoked_at = timezone.now() - timedelta(days=1)
        old_device.save(update_fields=("status", "revoked_at", "updated_at"))
        current_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        revoke_membership.return_value = ("revoked", uuid.uuid4())
        self.client.force_authenticate(user=self.user)

        response = self.client.delete(
            reverse("community_chat_device", args=(self.owner_key,)),
            {"reason": "lost replacement"},
            format="json",
            HTTP_ORIGIN="https://chat.mlai.au",
        )

        self.assertEqual(response.status_code, 200)
        current_device.refresh_from_db()
        self.assertEqual(current_device.status, DeviceBindingStatus.REVOKED)
        self.assertIsNotNone(current_device.revoked_at)
        revoke_membership.assert_called_once_with(self.owner_key)


@skipUnless(
    connection.features.has_select_for_update,
    "Requires row-level locks to verify the production delivery lease",
)
class SlackDmDeliveryLeaseTransactionTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="lease@example.com")
        self.owner_key = (
            "79be667ef9dcbbac55a06295ce870b070" "29bfcdb2dce28d959f2815b16f81798"
        )
        shadow_key = "3" * 64
        self.device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.owner_key,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        connection_record = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.SLACK,
            access_token="xoxp-lease",
            scopes=SCOPES,
            external_account_id="TLEASE",
            provider_metadata={
                "team": {"id": "TLEASE", "name": "Lease"},
                "authed_user": {"id": "UOWNER", "scope": ",".join(SCOPES)},
            },
        )
        self.grant = SlackDmMirrorGrant.objects.create(
            user=self.user,
            connection=connection_record,
            slack_workspace_id="TLEASE",
            slack_user_id="UOWNER",
            consented_at=timezone.now(),
        )
        CommunityBridgeIdentityLink.objects.create(
            user=self.user,
            slack_workspace_id="TLEASE",
            slack_user_id="UOWNER",
            buzz_pubkey=self.owner_key,
            display_name="Owner",
            verification_method=(
                CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE
            ),
            verification_reference="lease-test",
            verified_at=timezone.now(),
        )
        participant_pubkeys = sorted([self.owner_key, shadow_key])
        participant_hash = hashlib.sha256(
            b"".join(bytes.fromhex(value) for value in participant_pubkeys)
        ).hexdigest()
        self.conversation = SlackDmMirrorConversation.objects.create(
            grant=self.grant,
            slack_workspace_id="TLEASE",
            slack_conversation_id="DLEASE",
            participant_slack_ids=["UOWNER", "UOTHER"],
            participant_buzz_pubkeys=participant_pubkeys,
            participant_identity_map={
                "UOWNER": self.owner_key,
                "UOTHER": shadow_key,
            },
            participant_hash=participant_hash,
            mlai_channel_id=uuid.uuid4(),
            status=SlackDmMirrorConversationStatus.LIVE,
        )
        self.delivery = SlackDmMirrorDelivery.objects.create(
            conversation=self.conversation,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_message_id="c" * 64,
            source_author_id=self.owner_key,
            operation=CommunityBridgeDeliveryType.CREATE,
            encrypted_text="linearized private body",
            metadata={"participant_hash": participant_hash},
            available_at=timezone.now(),
        )

    @patch(
        "integrations.services.slack_dm_registration_ledger.BuzzBridgeClient.unregister_private_conversation"
    )
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_revoke_waits_for_an_in_flight_slack_body_write(
        self,
        web_client,
        unregister,
    ):
        send_started = threading.Event()
        allow_send_to_finish = threading.Event()
        revoke_returned = threading.Event()
        errors: list[BaseException] = []
        slack_client = MagicMock()

        def blocked_post(**_kwargs):
            send_started.set()
            if not allow_send_to_finish.wait(timeout=5):
                raise RuntimeError("test timed out waiting to release Slack send")
            return {"ts": "1787900002.000100"}

        slack_client.chat_postMessage.side_effect = blocked_post
        web_client.return_value = slack_client

        def drain_delivery():
            close_old_connections()
            try:
                slack_dm_mirror.process_ready_deliveries(limit=1)
            except BaseException as exc:  # surfaced on the test thread below
                errors.append(exc)
            finally:
                close_old_connections()

        def revoke():
            close_old_connections()
            try:
                grant = SlackDmMirrorGrant.objects.get(pk=self.grant.pk)
                slack_dm_mirror.revoke_grant(grant)
            except BaseException as exc:  # surfaced on the test thread below
                errors.append(exc)
            finally:
                revoke_returned.set()
                close_old_connections()

        delivery_thread = threading.Thread(target=drain_delivery)
        delivery_thread.start()
        self.assertTrue(send_started.wait(timeout=5))
        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        self.assertFalse(revoke_returned.wait(timeout=0.2))

        allow_send_to_finish.set()
        delivery_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        self.assertFalse(delivery_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(revoke_returned.is_set())

        self.grant.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.grant.status, SlackDmMirrorGrantStatus.REVOKED)
        self.assertEqual(self.delivery.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(self.delivery.encrypted_text, "")
        slack_client.chat_postMessage.assert_called_once()
        unregister.assert_called_once()

    @override_settings(
        COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    )
    @patch("community_chat.views.revoke_relay_membership")
    @patch("integrations.services.slack_dm_mirror.WebClient")
    def test_device_delete_waits_for_an_in_flight_slack_body_write(
        self,
        web_client,
        revoke_membership,
    ):
        send_started = threading.Event()
        allow_send_to_finish = threading.Event()
        delete_returned = threading.Event()
        errors: list[BaseException] = []
        responses = []
        slack_client = MagicMock()

        def blocked_post(**_kwargs):
            send_started.set()
            if not allow_send_to_finish.wait(timeout=5):
                raise RuntimeError("test timed out waiting to release Slack send")
            return {"ts": "1787900004.000100"}

        slack_client.chat_postMessage.side_effect = blocked_post
        web_client.return_value = slack_client
        revoke_membership.return_value = ("revoked", uuid.uuid4())

        def drain_delivery():
            close_old_connections()
            try:
                slack_dm_mirror.process_ready_deliveries(limit=1)
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        def delete_device():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(
                    user=get_user_model().objects.get(pk=self.user.pk)
                )
                responses.append(
                    client.delete(
                        reverse(
                            "community_chat_device",
                            args=(self.owner_key,),
                        ),
                        {"reason": "lost device"},
                        format="json",
                        HTTP_ORIGIN="https://chat.mlai.au",
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                delete_returned.set()
                close_old_connections()

        delivery_thread = threading.Thread(target=drain_delivery)
        delivery_thread.start()
        self.assertTrue(send_started.wait(timeout=5))
        delete_thread = threading.Thread(target=delete_device)
        delete_thread.start()
        self.assertFalse(delete_returned.wait(timeout=0.2))

        allow_send_to_finish.set()
        delivery_thread.join(timeout=5)
        delete_thread.join(timeout=5)
        self.assertFalse(delivery_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0].status_code, 200)
        self.device.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.device.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(self.delivery.encrypted_text, "")
        slack_client.chat_postMessage.assert_called_once()
