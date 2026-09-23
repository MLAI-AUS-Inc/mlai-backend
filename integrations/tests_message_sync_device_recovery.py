"""A replacement device recovers recent rooms without restarting discovery."""
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import ExternalServiceConnection, SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.device_recovery import recover_recent_conversation
from integrations.services.message_sync.device_recovery import (
    DEVICE_AUDIENCE_HINT, lock_enrollment_recovery_grants, schedule_enrollment_recovery,
)
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_chat_catalog import CATALOG_KEY
from integrations.services import slack_chat_read_state as reads
from integrations.services.community_bridge.buzz import BuzzBridgeError
from integrations.services.slack_dm_registration_ledger import registration_rows_for_grant, registration_state


@override_settings(MESSAGE_SYNC_ENABLED=True, SLACK_DM_MIRROR_SHADOW_SECRET="synthetic-device-recovery")
class DeviceRecoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.started = timezone.now()
        self.grant.history_days = 30
        self.grant.save()
        self.conversation.status = "provisioning"
        self.conversation.mlai_channel_id = None
        self.conversation.save()
        self.catalog(self.conversation, days=1)
        self.authority = dm._capture_slack_grant_api_authority(self.grant)
        dm._save_discovery_checkpoint(
            self.authority, cursor="historical-page-50", seen_channel_ids={"DIOAUTH"},
            failures=[], started_at=self.started,
        )
        self.room_id = str(uuid.uuid4())

    def catalog(self, conversation, *, days, kind="im"):
        metadata = dict(self.connection.provider_metadata)
        metadata[CATALOG_KEY] = {**metadata.get(CATALOG_KEY, {}), conversation.slack_conversation_id: {
            "kind": kind, "latest_message_ts": str(int(self.started.timestamp()) - days * 86400),
        }}
        self.connection.provider_metadata = metadata
        self.connection.save(update_fields=["provider_metadata"])
        self.grant.connection = self.connection

    def source(self, authority, method, **kwargs):
        if method == "conversations_info":
            return {"channel": {"id": kwargs["channel"], "is_im": True, "user": "UOTHER"}}
        if method == "users_info":
            return {"user": {"id": kwargs["user"], "profile": {"display_name": kwargs["user"]}}}
        self.fail(f"Unexpected provider operation: {method}")

    def recover(self):
        return recover_recent_conversation(
            self.grant, self.authority, profile_cache=self.conversation.participant_profiles,
            cycle_started_at=self.started,
        )

    def test_recovery_precedes_directory_and_preserves_its_cursor(self):
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source) as source,
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            self.assertEqual(dm.discover_conversations(self.grant), 1)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room_id)
        self.assertIsNone(self.conversation.history_backfilled_at)
        self.assertNotIn("users_conversations", [call.args[1] for call in source.call_args_list])
        cursor, seen, _, _ = dm._load_discovery_checkpoint(self.authority)
        self.assertEqual(cursor, "historical-page-50")
        self.assertIn("DIOAUTH", seen)

    def test_outside_window_does_not_provision_or_fetch(self):
        self.catalog(self.conversation, days=31)
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()

    def test_unconsented_private_room_cannot_block_recent_dm(self):
        private = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id="TIOAUTH", slack_conversation_id="GPRIVATE",
            status="provisioning", participant_slack_ids=["UOWNER", "UOTHER"],
        )
        self.catalog(private, days=0, kind="private_channel")
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source) as source,
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            self.assertTrue(self.recover())
        self.assertEqual(source.call_args_list[0].kwargs["channel"], "DIOAUTH")
        private.refresh_from_db()
        self.assertIsNone(private.mlai_channel_id)

    def test_revoked_device_never_enters_replacement_room(self):
        revoked = "b" * 64
        CommunityChatDevice.objects.create(user=self.user, public_key=revoked, status="revoked", revoked_at=timezone.now())
        replacement = "c" * 64
        CommunityChatDevice.objects.create(user=self.user, public_key=replacement, status="verified", verified_at=timezone.now())
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id}) as relay):
            self.assertTrue(self.recover())
        self.assertNotIn(revoked, relay.call_args.args[0])
        self.assertIn(replacement, relay.call_args.args[0])
        self.assertIn(self.owner_key, relay.call_args.args[0])

    def test_replacement_room_keeps_same_account_source_read_snapshot(self):
        old_target = reads.ReadTarget(str(uuid.uuid4()), "DIOAUTH", "im")
        snapshot_key = reads._cache_key(self.authority, old_target)
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            self.assertTrue(self.recover())
        targets = reads._targets_for_keys(self.grant, {self.owner_key})
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].channel_id, self.room_id)
        self.assertEqual(reads._cache_key(self.authority, targets[0]), snapshot_key)

    def test_resumed_group_recovery_retains_completed_member_pages(self):
        self.conversation.slack_conversation_id = "GRECOVER"
        self.conversation.save()
        self.catalog(self.conversation, days=1, kind="mpim")
        calls = []
        deferred = False

        def source(authority, method, **kwargs):
            nonlocal deferred
            calls.append(method)
            if method == "conversations_info":
                return {"channel": {"id": "GRECOVER", "is_mpim": True}}
            if method == "conversations_members":
                return {"members": ["UOWNER", "UOTHER", "UTHIRD"]}
            if method == "users_info" and not deferred:
                deferred = True
                raise BudgetDeferred(3)
            return self.source(authority, method, **kwargs)

        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=source),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            with self.assertRaises(BudgetDeferred):
                self.recover()
            self.assertTrue(self.recover())
        self.assertEqual(calls.count("conversations_members"), 1)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")

    def test_revoked_consent_after_provider_response_blocks_registration(self):
        def revoke(*args, **kwargs):
            self.grant.status = "revoked"
            self.grant.revoked_at = timezone.now()
            self.grant.save()
            return self.source(*args, **kwargs)
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=revoke),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation") as relay,
              self.assertRaises(dm.SlackDmMirrorAuthorizationError)):
            self.recover()
        relay.assert_not_called()

    def test_shared_or_archived_source_is_retired_without_registration(self):
        for flags in [{"is_ext_shared": True}, {"is_archived": True}]:
            with self.subTest(flags=flags):
                self.conversation.status = "provisioning"
                self.conversation.save()
                with (patch.object(dm, "_call_slack_with_grant_authority", return_value={
                    "channel": {"id": "DIOAUTH", "is_im": True, "user": "UOTHER", **flags},
                }), patch.object(dm.BuzzBridgeClient, "provision_private_conversation") as relay):
                    self.assertTrue(self.recover())
                relay.assert_not_called()
                self.conversation.refresh_from_db()
                self.assertNotEqual(self.conversation.status, "provisioning")

    def test_first_admission_deferral_keeps_fair_turn_but_provider_429_does_not(self):
        for method, expected in [("conversations.info", True), ("", False)]:
            exc = BudgetDeferred(3, before_request_method=method)
            with patch.object(dm, "_call_slack_with_grant_authority", side_effect=exc), self.assertRaises(BudgetDeferred):
                self.recover()
            self.assertEqual(exc.discovery_admission_deferred, expected)
            self.conversation.refresh_from_db()
            self.assertEqual(self.conversation.status, "provisioning")

    def test_bad_source_response_does_not_block_all_later_recovery(self):
        with patch.object(dm, "_call_slack_with_grant_authority", return_value={"channel": {"id": "DWRONG"}}):
            self.assertTrue(self.recover())
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "error")
        self.assertFalse(self.recover())

    def test_ambiguous_registration_timeout_retries_after_durable_cooldown(self):
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", side_effect=BuzzBridgeError("timeout"))):
            self.assertTrue(self.recover())
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "error")
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()
        SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk).update(
            updated_at=timezone.now() - timedelta(minutes=3),
        )
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source),
              patch.object(dm.BuzzBridgeClient, "unregister_private_conversation"),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            self.assertTrue(self.recover())
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")
        states = [registration_state(row) for row in registration_rows_for_grant(self.grant.pk)]
        self.assertEqual(states.count("active"), 1)
        self.assertNotIn("ambiguous", states)

    def test_repeated_failure_resets_cooldown_and_allows_other_room(self):
        self.conversation.status = "error"
        self.conversation.save()
        SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk).update(
            updated_at=timezone.now() - timedelta(minutes=3),
        )
        with patch.object(dm, "_call_slack_with_grant_authority", return_value={"channel": {"id": "DWRONG"}}):
            self.assertTrue(self.recover())
        self.conversation.refresh_from_db()
        self.assertGreater(self.conversation.updated_at, timezone.now() - timedelta(seconds=10))
        other = SlackDmMirrorConversation.objects.create(
            grant=self.grant, slack_workspace_id="TIOAUTH", slack_conversation_id="DOTHERRECENT",
            status="provisioning", participant_slack_ids=["UOWNER", "UOTHER"],
        )
        self.catalog(other, days=2)
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source) as source,
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room_id})):
            self.assertTrue(self.recover())
        self.assertEqual(source.call_args_list[0].kwargs["channel"], "DOTHERRECENT")

    def test_errored_rooms_outside_window_are_not_retried(self):
        self.catalog(self.conversation, days=31)
        SlackDmMirrorConversation.objects.filter(pk=self.conversation.pk).update(
            status="error", updated_at=timezone.now() - timedelta(minutes=3),
        )
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_STABLE_PRIVATE_ROOMS=True,
                   SLACK_DM_MIRROR_SHADOW_SECRET="synthetic-enrollment-recovery")
class EnrollmentDeviceRecoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.started = timezone.now()
        self.room = str(self.conversation.mlai_channel_id)
        self.new_key = "c" * 64
        self.device = CommunityChatDevice.objects.create(
            user=self.user, public_key=self.new_key, status="verified", verified_at=self.started,
        )
        self.conversation.participant_buzz_pubkeys = [self.owner_key, "b" * 64]
        self.conversation.participant_identity_map = {"UOWNER": self.owner_key, "UOTHER": "b" * 64}
        self.conversation.history_backfilled_at = self.started - timedelta(days=1)
        self.conversation.latest_synced_ts = str(self.started.timestamp())
        self.conversation.save()
        self.grant.last_discovery_at = self.started
        self.grant.save()

    def schedule(self, device=None):
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=self.user.pk)
            schedule_enrollment_recovery(lock_enrollment_recovery_grants(user), device or self.device)
        self.grant.refresh_from_db()
        self.connection.refresh_from_db()
        self.grant.connection = self.connection

    def recover(self):
        self.grant.refresh_from_db()
        self.connection.refresh_from_db()
        self.grant.connection = self.connection
        return recover_recent_conversation(
            self.grant, dm._capture_slack_grant_api_authority(self.grant),
            profile_cache=self.conversation.participant_profiles, cycle_started_at=self.started,
        )

    def source(self, authority, method, **kwargs):
        if method == "conversations_info":
            return {"channel": {"id": kwargs["channel"], "is_im": True, "user": "UOTHER"}}
        if method == "users_info":
            return {"user": {"id": kwargs["user"], "profile": {"display_name": kwargs["user"]}}}
        self.fail(f"Unexpected provider operation: {method}")

    def test_enrollment_wakes_discovery_without_hiding_existing_history(self):
        self.connection.sync_cursor = {"other_product": {"cursor": "keep"}, "message_sync_discovery": {
            "token": "in-flight-lease", "expires": self.started.timestamp() + 120,
            "served": 100, "due": self.started.timestamp() + 300,
        }}
        self.connection.save(update_fields=["sync_cursor"])
        before = self.conversation.history_backfilled_at
        self.schedule()
        self.conversation.refresh_from_db()
        self.assertIsNone(self.grant.last_discovery_at)
        self.assertEqual(self.conversation.status, "live")
        self.assertEqual(str(self.conversation.mlai_channel_id), self.room)
        self.assertEqual(self.conversation.history_backfilled_at, before)
        self.assertNotIn(self.new_key, self.conversation.participant_buzz_pubkeys)
        cursor = self.connection.sync_cursor
        self.assertEqual(cursor["other_product"], {"cursor": "keep"})
        self.assertEqual(cursor["message_sync_discovery"]["token"], "in-flight-lease")
        self.assertEqual(cursor["message_sync_discovery"]["served"], 100)
        self.assertLessEqual(cursor["message_sync_discovery"]["due"], timezone.now().timestamp())
        self.assertEqual(cursor[DEVICE_AUDIENCE_HINT]["public_key"], self.new_key)
        self.schedule()
        self.assertEqual(self.connection.sync_cursor[DEVICE_AUDIENCE_HINT], cursor[DEVICE_AUDIENCE_HINT])

    def test_live_room_is_repaired_before_directory_with_required_new_device(self):
        self.schedule()
        before = self.conversation.history_backfilled_at
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source) as source,
              patch.object(dm.BuzzBridgeClient, "private_delivery_receipts", return_value={}),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room}) as relay):
            self.assertTrue(self.recover())
        self.assertEqual(relay.call_args.kwargs["private_audience"]["channel_id"], self.room)
        self.assertIn(self.new_key, relay.call_args.args[0])
        self.assertIn(self.owner_key, relay.call_args.args[0])
        self.assertNotIn("users_conversations", [call.args[1] for call in source.call_args_list])
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")
        self.assertEqual(self.conversation.history_backfilled_at, before)
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()
        self.connection.refresh_from_db()
        self.assertNotIn(DEVICE_AUDIENCE_HINT, self.connection.sync_cursor)

    def test_selected_device_has_priority_when_account_exceeds_audience_limit(self):
        for index in range(10):
            CommunityChatDevice.objects.create(
                user=self.user, public_key=f"{index + 1:064x}", status="verified", verified_at=timezone.now(),
            )
        self.schedule()
        with (patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.source),
              patch.object(dm.BuzzBridgeClient, "private_delivery_receipts", return_value={}),
              patch.object(dm.BuzzBridgeClient, "provision_private_conversation", return_value={"channel_id": self.room}) as relay):
            self.assertTrue(self.recover())
        self.assertIn(self.new_key, relay.call_args.kwargs["callback_author_pubkeys"])
        self.assertIn(self.owner_key, relay.call_args.kwargs["callback_author_pubkeys"])
        self.assertEqual(len(relay.call_args.kwargs["callback_author_pubkeys"]), 8)

    def test_revoked_hint_never_adds_a_device_or_reads_slack(self):
        self.schedule()
        self.device.status = "revoked"
        self.device.revoked_at = timezone.now()
        self.device.save()
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()
        self.connection.refresh_from_db()
        self.assertNotIn(DEVICE_AUDIENCE_HINT, self.connection.sync_cursor)

    def test_failed_live_recovery_preserves_old_access_and_cools_down(self):
        self.schedule()
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=RuntimeError("synthetic timeout")):
            self.assertTrue(self.recover())
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, "live")
        self.assertEqual(self.conversation.participant_buzz_pubkeys, [self.owner_key, "b" * 64])
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertTrue(self.recover())
        source.assert_not_called()
        self.connection.refresh_from_db()
        self.assertIn(str(self.conversation.pk), self.connection.sync_cursor[DEVICE_AUDIENCE_HINT]["retry_after"])

    @override_settings(SLACK_OWNER_INVENTORY_ENABLED=True)
    def test_recovery_cooldown_yields_to_consented_incomplete_owner_directory(self):
        from integrations.services.slack_owner_inventory import KEY, grant_metadata_consent

        self.schedule()
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=RuntimeError("synthetic timeout")):
            self.assertTrue(self.recover())
        grant_metadata_consent(self.grant)
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertFalse(self.recover())
        source.assert_not_called()
        self.connection.refresh_from_db()
        self.assertIn(str(self.conversation.pk), self.connection.sync_cursor[DEVICE_AUDIENCE_HINT]["retry_after"])

        cursor = dict(self.connection.sync_cursor)
        inventory = dict(cursor[KEY])
        inventory["coverage"] = {
            "im": "complete", "mpim": "complete", "private_channel": "complete",
            "public_channel": "pending",
        }
        cursor[KEY] = inventory
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=("sync_cursor",))
        with patch.object(dm, "_call_slack_with_grant_authority") as source:
            self.assertTrue(self.recover())
        source.assert_not_called()

    def test_confirmation_and_hint_roll_back_together(self):
        self.device.status = "pending"
        self.device.save()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with transaction.atomic():
                user = get_user_model().objects.select_for_update().get(pk=self.user.pk)
                grants = lock_enrollment_recovery_grants(user)
                device = CommunityChatDevice.objects.select_for_update().get(pk=self.device.pk)
                device.status = "verified"
                device.save()
                schedule_enrollment_recovery(grants, device)
                raise RuntimeError("rollback")
        self.device.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertEqual(self.device.status, "pending")
        self.assertNotIn(DEVICE_AUDIENCE_HINT, self.connection.sync_cursor or {})

    def test_foreign_device_cannot_schedule_another_accounts_grant(self):
        other = get_user_model().objects.create_user(email="other-device-owner@example.test")
        foreign_device = CommunityChatDevice.objects.create(
            user=other, public_key="d" * 64, status="verified", verified_at=self.started,
        )
        self.schedule(foreign_device)
        self.assertNotIn(DEVICE_AUDIENCE_HINT, self.connection.sync_cursor or {})
        self.assertEqual(self.grant.last_discovery_at, self.started)

    def test_two_owner_hints_use_existing_fair_discovery_queue(self):
        from integrations.services.message_sync.discovery import claim_discovery, finish_discovery

        other = get_user_model().objects.create_user(email="second-recovery-owner@example.test")
        connection = ExternalServiceConnection.objects.create(
            user=other, provider="slack", access_token="synthetic-other-token",
            scopes=self.connection.scopes, external_account_id="TIOAUTH",
        )
        grant = SlackDmMirrorGrant.objects.create(
            user=other, connection=connection, slack_workspace_id="TIOAUTH", slack_user_id="USECOND",
            consented_at=self.started,
        )
        device = CommunityChatDevice.objects.create(
            user=other, public_key="d" * 64, status="verified", verified_at=self.started,
        )
        self.schedule()
        with transaction.atomic():
            other = get_user_model().objects.select_for_update().get(pk=other.pk)
            schedule_enrollment_recovery(lock_enrollment_recovery_grants(other), device)
        first = claim_discovery(300)
        self.assertIsNotNone(first)
        finish_discovery(first)
        second = claim_discovery(300)
        self.assertIsNotNone(second)
        self.assertEqual({first.grant_id, second.grant_id}, {self.grant.pk, grant.pk})

    @override_settings(COMMUNITY_CHAT_ALLOWED_ORIGINS=["mlaichat://callback"])
    def test_confirm_endpoint_commits_verified_device_and_recovery_together(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from community_chat.views import ConfirmView

        # The HTTP boundary validates an actual secp256k1 public key.
        self.new_key = "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
        self.device.public_key = self.new_key
        self.device.status = "pending"
        self.device.save()
        request = APIRequestFactory().post("/api/v1/community-chat/bootstrap/confirm/", {
            "public_key": self.new_key, "origin": "mlaichat://callback",
        }, format="json")
        force_authenticate(request, self.user)
        with (patch("community_chat.views.get_relay_membership", return_value=SimpleNamespace(is_member=True, role="member")),
              patch("community_chat.views.enforce_bootstrap_limits")):
            response = ConfirmView.as_view(throttle_classes=())(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.device.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertEqual(self.device.status, "verified")
        self.assertEqual(self.connection.sync_cursor[DEVICE_AUDIENCE_HINT]["public_key"], self.new_key)

    def test_paused_and_revoked_grants_do_not_receive_enrollment_hints(self):
        for status in ("paused", "revoked"):
            self.grant.status = status
            self.grant.revoked_at = self.started if status == "revoked" else None
            self.grant.save()
            self.schedule()
            self.assertNotIn(DEVICE_AUDIENCE_HINT, self.connection.sync_cursor or {})

    def test_worker_does_not_clear_a_newer_device_hint(self):
        from integrations.services.message_sync.device_recovery import _update_device_hint

        self.schedule()
        authority = dm._capture_slack_grant_api_authority(self.grant)
        newer = CommunityChatDevice.objects.create(
            user=self.user, public_key="d" * 64, status="verified", verified_at=timezone.now(),
        )
        self.schedule(newer)
        _update_device_hint(authority, self.new_key)
        _update_device_hint(authority, self.new_key, failed_conversation=self.conversation.pk)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[DEVICE_AUDIENCE_HINT], {
            "public_key": newer.public_key, "retry_after": {},
        })

    def test_enrollment_during_directory_page_keeps_next_turn_due(self):
        def directory_response(*args, **kwargs):
            self.schedule()
            return {"channels": [], "response_metadata": {"next_cursor": ""}}

        with (patch("integrations.services.message_sync.device_recovery.recover_recent_conversation", return_value=False),
              patch.object(dm, "_call_slack_with_grant_authority", side_effect=directory_response),
              patch.object(dm, "_retire_ineligible_from_slack_response")):
            dm.discover_conversations(self.grant)
        self.grant.refresh_from_db()
        self.connection.refresh_from_db()
        self.assertIsNone(self.grant.last_discovery_at)
        self.assertEqual(self.connection.sync_cursor[DEVICE_AUDIENCE_HINT]["public_key"], self.new_key)
