"""Successful discovery metadata pages survive provider budget deferrals."""

from unittest.mock import patch

from django.test import TransactionTestCase
from django.utils import timezone

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_discovery_progress import KEY, conversation_progress


def page(members, cursor=""):
    return {"members": members, "response_metadata": {"next_cursor": cursor}}


def user(user_id):
    return {
        "id": user_id,
        "profile": {"display_name": user_id, "image_192": "https://example.invalid/avatar"},
        "email": "not-checkpointed@example.invalid",
    }


class DirectoryProgressTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.authority = dm._capture_slack_grant_api_authority(self.grant)
        self.started = timezone.now()
        self.raw = {"id": "GNEW", "is_mpim": True}

    def scope(self, *, kind="mpim", channel="GNEW"):
        return conversation_progress(self.authority, channel, kind, self.started)

    def members(self, raw=None):
        return dm._conversation_participant_ids(
            self.authority, raw or self.raw, owner_slack_user_id="UOWNER",
        )

    def profile(self, user_id, cache):
        return dm._slack_profile(
            self.authority, user_id, cache, required_scopes=dm.DIRECT_DM_SCOPES,
        )

    def checkpoint(self):
        self.connection.refresh_from_db()
        return self.connection.sync_cursor.get(KEY)

    def test_more_than_200_members_resume_page_two_before_publishing_owner_membership(self):
        first = [f"UMEMBER{index:04}" for index in range(200)]
        raw = {"id": "GNEW", "is_private": True}
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=[
            page(first, "members-page-2"), BudgetDeferred(3), page(["UOWNER"]),
        ]) as source:
            with self.assertRaises(BudgetDeferred), self.scope(kind="private_channel"):
                self.members(raw)
            saved = self.checkpoint()
            self.assertEqual(saved["members"]["cursor"], "members-page-2")
            self.assertFalse(saved["members"]["complete"])
            self.assertNotIn("UOWNER", saved["members"]["ids"])
            self.assertFalse(self.grant.conversations.filter(slack_conversation_id="GNEW").exists())
            with self.scope(kind="private_channel"):
                self.assertEqual(self.members(raw), sorted([*first, "UOWNER"]))
        self.assertEqual(
            [call.kwargs["cursor"] for call in source.call_args_list],
            ["", "members-page-2", "members-page-2"],
        )
        self.assertIsNone(self.checkpoint())

    def test_completed_members_survive_a_later_profile_deferral(self):
        calls = []
        deferred = False

        def source(_authority, method, **kwargs):
            nonlocal deferred
            calls.append((method, kwargs.get("user")))
            if method == "conversations_members":
                return page(["UOWNER", "UOTHER"])
            if kwargs["user"] == "UOTHER" and not deferred:
                deferred = True
                raise BudgetDeferred(3)
            return {"user": user(kwargs["user"])}

        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=source):
            with self.assertRaises(BudgetDeferred), self.scope():
                self.assertEqual(self.members(), ["UOTHER", "UOWNER"])
                cache = {}
                self.profile("UOWNER", cache)
                self.profile("UOTHER", cache)
            self.assertTrue(self.checkpoint()["members"]["complete"])
            with self.scope():
                self.assertEqual(self.members(), ["UOTHER", "UOWNER"])
                cache = {}
                self.profile("UOWNER", cache)
                self.profile("UOTHER", cache)
                self.assertEqual(set(cache), {"UOWNER", "UOTHER"})
        self.assertEqual(calls, [
            ("conversations_members", None), ("users_info", "UOWNER"),
            ("users_info", "UOTHER"), ("users_info", "UOTHER"),
        ])
        self.assertIsNone(self.checkpoint())

    def test_bulk_pages_and_fallback_profiles_resume_without_restarting_completed_bulk(self):
        participant_ids = {"UOWNER", *(f"U{index}" for index in range(1, 8))}
        calls = []
        page_deferred = False
        info_deferred = False

        def source(_authority, method, **kwargs):
            nonlocal page_deferred, info_deferred
            calls.append((method, kwargs.get("cursor", kwargs.get("user"))))
            if method == "users_list":
                if not kwargs["cursor"]:
                    return page([user("UOWNER")], "users-page-2")
                if not page_deferred:
                    page_deferred = True
                    raise BudgetDeferred(3)
                return page([user("U1")])
            if kwargs["user"] == "U3" and not info_deferred:
                info_deferred = True
                raise BudgetDeferred(3)
            return {"user": user(kwargs["user"])}

        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=source):
            with self.assertRaises(BudgetDeferred), self.scope():
                dm._preload_slack_profiles(self.authority, participant_ids, {})
            saved = self.checkpoint()
            self.assertEqual(saved["bulk"]["cursor"], "users-page-2")
            self.assertEqual(set(saved["profiles"]), {"UOWNER"})
            with self.assertRaises(BudgetDeferred), self.scope():
                cache = {}
                dm._preload_slack_profiles(self.authority, participant_ids, cache)
                self.assertEqual(set(cache), {"UOWNER", "U1"})
                self.profile("U2", cache)
                self.profile("U3", cache)
            saved = self.checkpoint()
            self.assertTrue(saved["bulk"]["complete"])
            self.assertEqual(set(saved["profiles"]), {"UOWNER", "U1", "U2"})
            self.assertNotIn("email", saved["profiles"]["UOWNER"])
            with self.scope():
                cache = {}
                dm._preload_slack_profiles(self.authority, participant_ids, cache)
                for participant in sorted(participant_ids):
                    self.profile(participant, cache)
                self.assertEqual(set(cache), participant_ids)
        self.assertEqual(
            [value for method, value in calls if method == "users_list"],
            ["", "users-page-2", "users-page-2"],
        )
        self.assertEqual([value for method, value in calls if method == "users_info"],
                         ["U2", "U3", "U3", "U4", "U5", "U6", "U7"])
        self.assertIsNone(self.checkpoint())

    def test_two_missing_im_profiles_preserve_each_successful_user_lookup(self):
        raw = {"id": "DNEW", "user": "UFIRST", "is_im": True}
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=[
            {"user": user("UFIRST")}, BudgetDeferred(3), {"user": user("UOWNER")},
        ]) as source:
            with self.assertRaises(BudgetDeferred), self.scope(channel="DNEW", kind="im"):
                participants = self.members(raw)
                cache = {}
                dm._preload_slack_profiles(self.authority, set(participants), cache)
                for participant in participants:
                    self.profile(participant, cache)
            self.assertEqual(set(self.checkpoint()["profiles"]), {"UFIRST"})
            with self.scope(channel="DNEW", kind="im"):
                cache = {}
                dm._preload_slack_profiles(self.authority, set(self.members(raw)), cache)
                for participant in self.members(raw):
                    self.profile(participant, cache)
                self.assertEqual(set(cache), {"UFIRST", "UOWNER"})
        self.assertEqual([call.args[1] for call in source.call_args_list], ["users_info"] * 3)
        self.assertEqual([call.kwargs["user"] for call in source.call_args_list],
                         ["UFIRST", "UOWNER", "UOWNER"])
        self.assertIsNone(self.checkpoint())

    def test_known_membership_removal_invalidates_completed_members_but_preserves_profiles(self):
        self.conversation.slack_conversation_id = "GNEW"
        self.conversation.participant_slack_ids = ["UOWNER", "UOTHER", "UREMOVED"]
        self.conversation.save(update_fields=["slack_conversation_id", "participant_slack_ids"])
        with patch.object(dm, "_call_slack_with_grant_authority", side_effect=[
            page(["UOWNER", "UOTHER", "UREMOVED"]), {"user": user("UOWNER")},
            page(["UOWNER", "UOTHER"]),
        ]) as source:
            with self.assertRaises(BudgetDeferred), self.scope():
                self.assertIn("UREMOVED", self.members())
                self.profile("UOWNER", {})
                raise BudgetDeferred(3)
            self.conversation.participant_slack_ids = ["UOWNER", "UOTHER"]
            self.conversation.participant_hash = "b" * 64
            self.conversation.mlai_channel_id = None
            self.conversation.status = "provisioning"
            self.conversation.save(update_fields=[
                "participant_slack_ids", "participant_hash", "mlai_channel_id", "status",
            ])
            with self.scope():
                self.assertEqual(self.members(), ["UOTHER", "UOWNER"])
                self.assertEqual(self.profile("UOWNER", {})["display_name"], "UOWNER")
        self.assertEqual([call.args[1] for call in source.call_args_list],
                         ["conversations_members", "users_info", "conversations_members"])
        self.assertIsNone(self.checkpoint())

    def test_owner_absent_from_complete_member_scan_never_provisions_partial_members(self):
        with (
            patch.object(dm, "_call_slack_with_grant_authority", side_effect=[
                page(["UOTHER"], "members-page-2"), page(["UTHIRD"]),
            ]) as source,
            patch.object(dm, "_store_conversation_membership_intent") as intent,
            patch.object(dm, "_provision_owner_conversation") as provision,
            patch.object(dm, "_retire_ineligible_from_slack_response") as retire,
            self.scope(),
        ):
            result = dm._discover_conversation(
                self.grant, self.authority, self.raw, profile_cache={},
                force_backfill=False, reset_history=False,
            )
            self.assertIsNone(result)
            self.assertEqual(source.call_count, 2)
            intent.assert_not_called()
            provision.assert_not_called()
            retire.assert_called_once()
            self.assertEqual(retire.call_args.kwargs["reason"], dm.SLACK_PARTICIPANTS_INELIGIBLE_REASON)
        self.assertIsNone(self.checkpoint())
