"""A replacement device recovers recent rooms without restarting discovery."""
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import SlackDmMirrorConversation
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.device_recovery import recover_recent_conversation
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
