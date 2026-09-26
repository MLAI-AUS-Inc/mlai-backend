"""Directory checkpoints never bypass consent, lease or complete-membership fences."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.discovery import (
    KEY as LEASE_KEY, claim_discovery, discovery_context,
)
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost
from integrations.services.slack_discovery_progress import KEY, conversation_progress


class DirectoryAuthorityTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.authority = dm._capture_slack_grant_api_authority(self.grant)
        self.started = timezone.now()

    def scope(self, **kwargs):
        return conversation_progress(
            kwargs.get("authority", self.authority), kwargs.get("channel", "GTEST"),
            kwargs.get("kind", "mpim"), kwargs.get("started", self.started),
        )

    def stage(self):
        with self.assertRaises(BudgetDeferred), self.scope() as progress:
            progress.save_members({"UOWNER"}, "page2", {"page2"}, timezone.now().timestamp())
            progress.save_profiles({"UOWNER": {"display_name": "Owner", "avatar_url": ""}})
            raise BudgetDeferred(3)

    def test_scopes_do_not_mix_channels_kinds_cycles_or_history_windows(self):
        for change in (
            {"channel": "GOTHER"}, {"kind": "private_channel"},
            {"started": self.started + timedelta(seconds=1)},
        ):
            with self.subTest(change=change):
                self.stage()
                with self.scope(**change) as progress:
                    self.assertEqual(progress.members()["ids"], [])
                    self.assertEqual(progress.profiles(), {})
        self.stage()
        self.grant.history_days = 7
        self.grant.save(update_fields=["history_days"])
        with self.scope() as progress:
            self.assertEqual(progress.profiles(), {})
            self.assertEqual(progress.members()["ids"], [])

    def test_expired_membership_preserves_sanitized_profile_progress(self):
        self.stage()
        self.connection.refresh_from_db()
        self.connection.sync_cursor[KEY]["members"]["started_at"] -= 3601
        self.connection.save(update_fields=["sync_cursor"])
        with self.scope() as progress:
            self.assertEqual(progress.members()["ids"], [])
            self.assertEqual(progress.profiles()["UOWNER"]["display_name"], "Owner")

    def test_completed_membership_ages_independently_of_profiles(self):
        with self.assertRaises(BudgetDeferred), self.scope() as progress:
            progress.save_members({"UOWNER", "UOTHER"}, "", set(), timezone.now().timestamp())
            progress.save_profiles({"UOWNER": {"display_name": "Owner", "avatar_url": ""}})
            raise BudgetDeferred(3)
        self.connection.refresh_from_db()
        self.connection.sync_cursor[KEY]["members"]["completed_at"] -= 301
        self.connection.save(update_fields=["sync_cursor"])
        with self.scope() as progress:
            self.assertEqual(progress.members()["ids"], [])
            self.assertIn("UOWNER", progress.profiles())

    def test_revoked_grant_cannot_load_or_save_checkpoint(self):
        self.stage()
        self.grant.status = "revoked"
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=["status", "revoked_at"])
        with self.assertRaises(dm.SlackDmMirrorAuthorizationError), self.scope():
            self.fail("revoked checkpoint was loaded")

    def test_changed_consent_and_token_invalidate_prior_progress(self):
        self.stage()
        self.grant.consented_at += timedelta(seconds=1)
        self.grant.save(update_fields=["consented_at"])
        with self.assertRaises(dm.SlackDmMirrorAuthorizationError), self.scope():
            self.fail("old consent was accepted")
        current = dm._capture_slack_grant_api_authority(self.grant)
        with self.scope(authority=current) as progress:
            self.assertEqual(progress.members()["ids"], [])
        self.connection.access_token = "synthetic-replaced"
        self.connection.save(update_fields=["access_token"])
        with self.assertRaises(dm.SlackDmMirrorAuthorizationError), self.scope(authority=current):
            self.fail("old token was accepted")

    def test_late_lease_cannot_persist_successful_page(self):
        lease = claim_discovery(300)
        with discovery_context(lease), self.assertRaises(LeaseLost), self.scope() as progress:
            self.connection.refresh_from_db()
            self.connection.sync_cursor[LEASE_KEY]["expires"] = 0
            self.connection.save(update_fields=["sync_cursor"])
            progress.save_profiles({"UOTHER": {"display_name": "Late", "avatar_url": ""}})
        self.connection.refresh_from_db()
        self.assertNotIn(KEY, self.connection.sync_cursor)

    def test_concurrent_membership_change_blocks_inflight_intent_and_checkpoint(self):
        for action in ("checkpoint", "intent"):
            with self.subTest(action=action), self.assertRaises(dm.SlackDmMirrorAuthorizationError), self.scope(
                channel=self.conversation.slack_conversation_id,
            ) as progress:
                self.conversation.participant_slack_ids = ["UOWNER", "UNEW"]
                self.conversation.save(update_fields=["participant_slack_ids"])
                if action == "checkpoint":
                    progress.save_profiles({"UOTHER": {"display_name": "Old", "avatar_url": ""}})
                else:
                    dm._store_conversation_membership_intent(
                        self.grant.pk, authority=self.authority,
                        required_scopes=dm.DIRECT_DM_SCOPES,
                        slack_conversation_id=self.conversation.slack_conversation_id,
                        participant_slack_ids=["UOWNER", "UOTHER"],
                    )
            self.conversation.refresh_from_db()
            self.assertEqual(self.conversation.participant_slack_ids, ["UOWNER", "UNEW"])
            self.conversation.participant_slack_ids = ["UOWNER", "UOTHER"]
            self.conversation.save(update_fields=["participant_slack_ids"])

    def test_partial_membership_is_not_published_and_cycle_survives_restart(self):
        raw = {"id": "GTEST", "is_mpim": True, "latest": f"{int(timezone.now().timestamp())}.000001"}
        def source(_authority, method, **kwargs):
            if method == "users_conversations":
                return {"channels": [raw], "response_metadata": {"next_cursor": ""}}
            if method == "conversations_members" and not kwargs["cursor"]:
                return {"members": ["UOTHER"], "response_metadata": {"next_cursor": "page2"}}
            raise BudgetDeferred(3)
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=source), patch.object(
            dm, "_provision_owner_conversation",
        ) as provision, self.assertRaises(BudgetDeferred):
            dm.discover_conversations(self.grant)
        provision.assert_not_called()
        self.assertFalse(self.grant.conversations.filter(slack_conversation_id="GTEST").exists())
        self.connection.refresh_from_db()
        checkpoint = self.connection.sync_cursor[dm.DISCOVERY_CHECKPOINT_KEY]
        progress = self.connection.sync_cursor[KEY]
        self.assertEqual(checkpoint["started_at"], progress["cycle_started_at"])
        self.assertNotIn("GTEST", checkpoint["seen_channel_ids"])

    def test_directory_resumes_a_new_dm_and_provisions_only_after_both_profiles(self):
        self.conversation.participant_profiles = {}
        self.conversation.save(update_fields=["participant_profiles"])
        raw = {"id": "DNEW", "is_im": True, "user": "UFIRST",
               "latest": f"{int(timezone.now().timestamp())}.000001"}
        looked_up = []
        deferred = False

        def source(_authority, method, **kwargs):
            nonlocal deferred
            if method == "users_conversations":
                return {"channels": [raw], "response_metadata": {"next_cursor": ""}}
            self.assertEqual(method, "users_info")
            looked_up.append(kwargs["user"])
            if kwargs["user"] == "UOWNER" and not deferred:
                deferred = True
                raise BudgetDeferred(3)
            return {"user": {"id": kwargs["user"], "name": "Synthetic"}}

        with (
            patch.object(dm, "_call_slack_with_grant_authority", side_effect=source),
            patch.object(dm, "_provision_owner_conversation") as provision,
            patch.object(dm, "_retire_ineligible_from_slack_response"),
        ):
            with self.assertRaises(BudgetDeferred):
                dm.discover_conversations(self.grant)
            provision.assert_not_called()
            self.assertEqual(dm.discover_conversations(self.grant), 1)
        provision.assert_called_once()
        self.assertEqual(looked_up, ["UFIRST", "UOWNER", "UOWNER"])
        conversation = self.grant.conversations.get(slack_conversation_id="DNEW")
        self.assertEqual(set(conversation.participant_profiles), {"UFIRST", "UOWNER"})
        self.connection.refresh_from_db()
        self.assertNotIn(KEY, self.connection.sync_cursor)
