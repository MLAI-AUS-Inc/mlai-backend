"""Community reader entitlement, definitive-zero pages and authority races."""
from datetime import timedelta
import threading
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeDelivery, SlackChannelMembership
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.message_sync import member_history as member
from integrations.services.message_sync.member_callback_authority import CallbackAuthorityChanged, callback_user_fence
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS


CONFIG = {**SETTINGS, "SLACK_COMMUNITY_SOURCE": "members", "MESSAGE_SYNC_SLACK_APP_ID": "ABOT",
          "SLACK_BRIDGE_BOT_USER_ID": "UBOT", "SLACK_BRIDGE_BOT_TOKEN": "synthetic-bot-only"}


@override_settings(**CONFIG)
class CommunityReaderTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.channel.audience = "community"
        self.channel.room_state = {"applied": {"audience": "community"}, "preserve": "unchanged"}
        self.channel.save()
        self.state.public_channel = self.channel
        self.bot_calls = []
        self.bot_hook = None
        self.member_hook = None
        self.bot_page = {"ok": True, "messages": []}
        self.bot_info = {"ok": True, "channel": {"id": "CROOM", "is_channel": True,
                         "is_private": False, "is_member": True}}
        self.member_page = None
        self.member_info = None
        self.webclient_hook = None
        self.sdk = self.BotSDK(self)
        constructor = patch("slack_sdk.WebClient", side_effect=self.make_sdk)
        constructor.start()
        self.addCleanup(constructor.stop)
        budget = patch("integrations.services.message_sync.slack_client.budgeted_client", side_effect=self.budget_client)
        budget.start()
        self.addCleanup(budget.stop)
        # Exercise the actual local revoke/OAuth/privacy lifecycle without
        # permitting its optional remote best-effort token revoke request.
        remote = patch.object(dm, "_revoke_remote_token")
        self.remote_revoke = remote.start()
        self.addCleanup(remote.stop)

    class BotSDK:
        def __init__(self, owner):
            self.owner, self.before_request = owner, None

        def __getattr__(self, method):
            def request(**kwargs):
                if self.before_request:
                    self.before_request()
                self.owner.bot_calls.append((method, kwargs))
                if self.owner.bot_hook:
                    self.owner.bot_hook(method)
                if method == "auth_test":
                    return {"ok": True, "team_id": "TTEST", "user_id": "UBOT", "bot_id": "BBOT", "app_id": "ABOT"}
                if method == "conversations_info":
                    return self.owner.bot_info
                return self.owner.bot_page
            return request

    def make_sdk(self, **kwargs):
        self.assertEqual(kwargs, {"token": "synthetic-bot-only", "timeout": 20})
        if self.webclient_hook:
            self.webclient_hook()
        return self.sdk

    def budget_client(self, client, **kwargs):
        self.assertIs(client, self.sdk)
        self.assertEqual(kwargs["workspace_id"], "TTEST")
        self.sdk.before_request = kwargs["before_request"]
        return self.sdk

    def transport(self, authority, conn, method, **kwargs):
        response = super().transport(authority, conn, method, **kwargs)
        if self.member_hook:
            self.member_hook(method)
        if method == "conversations_info" and self.member_info is not None:
            return self.member_info
        return self.member_page if method != "conversations_info" and self.member_page is not None else response

    def assessment(self):
        return access.assess_readers("TTEST", "CROOM")

    def left(self):
        SlackChannelMembership.objects.update(state="left", observed_at=timezone.now(), left_at=timezone.now())

    def joined(self):
        SlackChannelMembership.objects.update(state="joined", observed_at=timezone.now(), left_at=None)

    def wake(self):
        self.state.jobs.update(due_at=timezone.now(), completed_at=None, lease_token=None, lease_expires_at=None)
        self.state.refresh_from_db()
        self.channel.refresh_from_db()
        self.state.public_channel = self.channel

    def reader_state(self):
        self.channel.refresh_from_db()
        return self.channel.room_state[access.COMMUNITY_READER_STATE]

    def test_readers_need_no_chatwrite_devices_ai_or_attribution_link(self):
        CommunityChatDevice.objects.all().delete()
        for grant in (self.a, self.b):
            grant.connection.scopes = sorted(access.PUBLIC_READER_SCOPES)
            grant.connection.save(update_fields=("scopes",))
        proof = self.assessment()
        self.assertEqual((proof.eligible_count, proof.unknown_count, proof.complete), (2, 0, True))
        self.run_page()
        self.assertEqual([c[1] for c in self.calls], ["conversations_info", "conversations_history", "conversations_info"])
        self.assertEqual(self.bot_calls, [])

    def age_source(self):
        self.channel.slack_seen_at = timezone.now()-timedelta(minutes=10)
        self.channel.save(update_fields=("slack_seen_at",))
        self.state.public_channel = self.channel
        return self.channel.slack_seen_at

    def test_revalidation_hint_is_pure_and_only_approved_aged_public_shape(self):
        self.age_source()
        with self.assertNumQueries(0):
            self.assertTrue(access.community_source_needs_revalidation(self.channel))
        for field, value in (("slack_seen_at", None), ("slack_seen_at", timezone.now()+timedelta(minutes=1)),
                             ("slack_visibility", "private"), ("hold_reason", "slack_connect"),
                             ("audience", "members"), ("room_sync_pending", True),
                             ("slack_archived", True), ("enabled", False),
                             ("destination_channel_id", "")):
            before = getattr(self.channel, field)
            with self.subTest(field=field, value=value):
                setattr(self.channel, field, value)
                with self.assertNumQueries(0):
                    self.assertFalse(access.community_source_needs_revalidation(self.channel))
                setattr(self.channel, field, before)
        for changed in ({"SLACK_MEMBER_CHANNELS_ENABLED": False}, {"SLACK_COMMUNITY_SOURCE": "both"},
                        {"SLACK_MEMBER_CHANNEL_ALLOWED_IDS": "COTHER"}):
            with override_settings(**changed), self.assertNumQueries(0):
                self.assertFalse(access.community_source_needs_revalidation(self.channel))

    def test_stale_readonly_reader_info_renews_before_content_without_posting_authority(self):
        CommunityChatDevice.objects.all().delete()
        for grant in (self.a, self.b):
            grant.connection.scopes = sorted(access.PUBLIC_READER_SCOPES)
            grant.connection.save(update_fields=("scopes",))
        before = self.age_source()
        self.run_page()
        self.channel.refresh_from_db()
        self.assertGreater(self.channel.slack_seen_at, before)
        self.assertLessEqual(self.channel.slack_seen_at, timezone.now())
        self.assertEqual([c[1] for c in self.calls], ["conversations_info", "conversations_history", "conversations_info"])
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.channel.room_state["preserve"], "unchanged")
        self.assertFalse(access.community_source_needs_revalidation(self.channel))

    def test_zero_reader_hold_preserves_aged_proof_and_checkpoint_on_retries(self):
        self.left()
        checkpoint = {"cursor": "SAVED-MEMBER", "member_authority": "prior-authority"}
        self.state.jobs.filter(kind="archive").update(checkpoint=checkpoint)
        for _ in range(2):
            self.wake()
            before = self.age_source()
            self.run_page()
            self.channel.refresh_from_db()
            self.assertEqual(self.channel.slack_seen_at, before)
            self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, checkpoint)
            self.assertEqual(self.reader_state()["fallback_reason"], "no_reader")
            self.assertEqual(self.channel.sync_paused_reason, "no_reader")
            self.assertEqual(self.calls, [])
            self.assertEqual(self.bot_calls, [])

    def test_successful_info_renewal_survives_later_history_quota_deferral(self):
        before = self.age_source()
        self.member_hook = lambda method: (_ for _ in ()).throw(BudgetDeferred(60)) if method == "conversations_history" else None
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.channel.refresh_from_db()
        self.assertGreater(self.channel.slack_seen_at, before)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        payload = {"team_id": "TTEST", "api_app_id": "ABOT", "event": {"type": "message", "channel": "CROOM"}}
        with transaction.atomic(), callback_user_fence(payload), access.community_reader_policy(payload):
            self.assertEqual(access.community_reader_policy_current(self.channel).effective_source, "members")


    def test_stale_reader_info_private_or_connect_closes_before_any_body(self):
        for marker, reason in (("is_private", "converted_to_private"), ("is_ext_shared", "slack_connect")):
            with self.subTest(marker=marker):
                self.wake()
                self.channel.enabled, self.channel.slack_visibility = True, "public"
                self.channel.mirror_decision, self.channel.hold_reason = "mirror", ""
                self.channel.room_sync_pending = False
                self.channel.room_state = {"preserve": "unchanged"}
                self.channel.save()
                before = self.age_source()
                self.calls.clear()
                self.member_info = {"ok": True, "channel": {"id": "CROOM", "is_channel": True,
                                    "is_private": False, "is_member": True, marker: True}}
                with self.assertRaises(BudgetDeferred):
                    self.run_page()
                self.channel.refresh_from_db()
                self.assertEqual([c[1] for c in self.calls], ["conversations_info"])
                self.assertEqual(self.channel.slack_seen_at, before)
                self.assertEqual(self.channel.hold_reason, reason)
                self.assertFalse(self.channel.enabled)
                self.assertEqual(self.bot_calls, [])
                self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_stale_member_private_info_cannot_renew_or_request_content(self):
        before = self.age_source()
        self.member_info = {"ok": True, "channel": {**self.bot_info["channel"], "is_private": True}}
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.slack_seen_at, before)
        self.assertEqual(self.channel.hold_reason, "converted_to_private")
        self.assertEqual([c[1] for c in self.calls], ["conversations_info"])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())


    def test_stale_scroll_demand_schedules_metadata_then_worker_revalidates(self):
        from integrations.services.slack_history_pages import _public_progress
        before = self.age_source()
        result = member.request_history(self.a.user, self.channel, "1" * 64, "a" * 64, _public_progress)
        self.assertTrue(result["pending"])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.slack_seen_at, before)
        self.assertEqual(self.bot_calls, [])
        self.run_page()
        self.channel.refresh_from_db()
        self.assertGreater(self.channel.slack_seen_at, before)


    def test_stale_member_info_uncertainty_never_renews_or_requests_body(self):
        before = self.age_source()
        self.member_info = {"ok": True, "channel": {"id": "CROOM", "is_channel": True, "is_private": False}}
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.slack_seen_at, before)
        self.assertEqual([c[1] for c in self.calls], ["conversations_info"])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())


    def test_unknown_source_proof_or_reader_cannot_use_stale_info_admission(self):
        self.left()
        self.age_source()
        SlackChannelMembership.objects.all().delete()
        self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.wake()
        self.channel.slack_seen_at = None
        self.channel.save(update_fields=("slack_seen_at",))
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.assertEqual(self.bot_calls, [])

    def test_multiple_slack_grants_of_one_owner_count_once(self):
        extra = self.grant(3)
        extra.connection.user = self.a.user
        extra.connection.save(update_fields=("user",))
        extra.user = self.a.user
        extra.save(update_fields=("user",))
        self.assertEqual(len(self.assessment().eligible_grants), 3)
        self.assertEqual(self.assessment().eligible_count, 2)

    def test_cooldown_and_exclusion_are_availability_not_zero(self):
        for grant in (self.a, self.b):
            access.cache.set(access._key(grant, "CROOM", "history"), "unavailable", timeout=60)
        proof = self.assessment()
        self.assertEqual((proof.eligible_count, len(proof.available_grants), proof.complete), (2, 0, True))
        self.assertIsNone(access.reader_for_channel("TTEST", "CROOM", exclude_grant_ids=(self.a.pk, self.b.pk)))
        self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.reader_state()["effective_source"], "members")
        self.assertEqual(self.reader_state()["fallback_reason"], "")
        self.assertEqual(self.channel.sync_paused_reason, "")

    def test_missing_membership_is_unknown_and_never_bot(self):
        SlackChannelMembership.objects.all().delete()
        proof = self.assessment()
        self.assertEqual((proof.eligible_count, proof.unknown_count, proof.complete), (0, 2, False))
        self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.reader_state()["effective_source"], "hold")
        self.assertEqual(self.channel.sync_paused_reason, "")
        self.assertFalse(self.reader_state()["complete"])
        self.assertEqual(self.reader_state()["fallback_reason"], "")

    def test_stale_probe_and_changed_epoch_never_certify_zero(self):
        SlackChannelMembership.objects.update(source="probe", observed_at=timezone.now()-timedelta(hours=1))
        self.assertFalse(self.assessment().complete)
        self.assertEqual(self.assessment().effective_source, "hold")

    def test_expired_or_unreadable_credentials_are_unknown(self):
        for grant in (self.a, self.b):
            grant.connection.token_expires_at = timezone.now()-timedelta(minutes=1)
            grant.connection.save(update_fields=("token_expires_at",))
        self.assertEqual(self.assessment().unknown_count, 2)
        # Candidate selection may refresh before the lower-order page locks.
        self.assertIsNotNone(access.reader_for_channel("TTEST", "CROOM"))
        for grant in (self.a, self.b):
            grant.connection.token_expires_at = None
            grant.connection.save(update_fields=("token_expires_at",))
        with patch.object(dm, "_capture_slack_grant_api_authority", side_effect=dm.SlackDmMirrorCredentialError("synthetic")) as capture:
            self.assertFalse(self.assessment().complete)
            self.assertGreaterEqual(capture.call_count, 2)

    def test_revoked_inactive_or_missing_read_scope_is_definitive_ineligible(self):
        self.a.status = "revoked"
        self.a.save(update_fields=("status",))
        self.b.connection.scopes = ["chat:write"]
        self.b.connection.save(update_fields=("scopes",))
        proof = self.assessment()
        self.assertEqual((proof.eligible_count, proof.complete, proof.effective_source), (0, True, "hold"))

    def test_known_zero_holds_without_any_provider_request(self):
        self.left()
        self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.calls, [])
        state = self.reader_state()
        self.assertEqual((state["effective_source"], state["fallback_reason"]), ("hold", "no_reader"))
        self.assertIsNone(state["fallback_since"])
        self.assertFalse(state["continuity_proven"])
        self.assertEqual(self.channel.room_state["preserve"], "unchanged")
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_unknown_or_cooling_reader_is_not_no_reader_fallback(self):
        self.member_hook = lambda method: (_ for _ in ()).throw(BudgetDeferred(60))
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.reader_state()["fallback_reason"], "")

    def test_three_reader_attempt_failures_never_mean_zero(self):
        self.grant(3)
        self.member_hook = lambda method: (_ for _ in ()).throw(dm.SlackApiError("synthetic", {"ok": False, "error": "channel_not_found"}))
        self.run_page()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.reader_state()["eligible_count"], 3)
        self.assertEqual(self.reader_state()["fallback_reason"], "")
        self.remote_revoke.assert_not_called()

    def test_auth_rejection_revokes_exact_reader_then_recovers_with_other_reader_same_lease(self):
        original_epoch = dm.current_slack_oauth_generation(self.a.user_id)
        def reject(method):
            if self.calls[-1][0] == self.a.pk:
                raise dm.SlackApiError("synthetic", {"ok": False, "error": "token_revoked"})
        self.member_hook = reject
        self.run_page()
        self.a.refresh_from_db()
        self.a.connection.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.status, "revoked")
        self.assertEqual(self.a.connection.access_token, "")
        self.assertGreater(dm.current_slack_oauth_generation(self.a.user_id), original_epoch)
        self.assertEqual(self.b.status, "active")
        self.assertEqual(self.calls[0][:2], (self.a.pk, "conversations_info"))
        self.assertEqual({c[0] for c in self.calls[1:]}, {self.b.pk})
        self.assertEqual([c[1] for c in self.calls[1:]], ["conversations_info", "conversations_history", "conversations_info"])
        self.assertEqual(self.bot_calls, [])
        self.assertTrue(CommunityBridgeDelivery.objects.exists())
        self.remote_revoke.assert_called_once()

    def test_full_missing_scope_revocation_holds_after_all_readers_fail(self):
        self.grant(3)
        self.member_hook = lambda method: (_ for _ in ()).throw(dm.SlackApiError("synthetic", {"ok": False, "error": "missing_scope"}))
        self.run_page()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.remote_revoke.call_count, 3)
        self.assertEqual(self.assessment().eligible_count, 0)
        self.assertTrue(self.assessment().complete)
        self.assertEqual(self.reader_state()["fallback_reason"], "no_reader")
        self.assertEqual(self.bot_calls, [])

    def test_departure_fences_old_lease_then_recovers_with_other_reader(self):
        self._rejected_reader_recovers("not_in_channel")

    def _rejected_reader_recovers(self, code):
        def reject(method):
            if self.calls[-1][0] == self.a.pk:
                raise dm.SlackApiError("synthetic", {"ok": False, "error": code})
        self.member_hook = reject
        with self.assertRaises(LeaseLost):
            self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.wake()
        self.calls.clear()
        self.run_page()
        self.assertEqual({c[0] for c in self.calls}, {self.b.pk})
        self.assertEqual(self.bot_calls, [])

    def test_no_reader_hold_does_not_use_bot_budget(self):
        self.left()
        with patch("integrations.services.message_sync.slack_client.budgeted_client", side_effect=AssertionError("bot budget")):
            self.run_page()
        self.assertEqual(self.reader_state()["fallback_reason"], "no_reader")
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_no_reader_hold_does_not_require_bot_configuration(self):
        self.left()
        with override_settings(SLACK_BRIDGE_BOT_TOKEN="", SLACK_BRIDGE_BOT_USER_ID=""):
            self.run_page()
        self.assertEqual(self.bot_calls, [])
        self.assertEqual(self.reader_state()["effective_source"], "hold")
        self.assertFalse(CommunityBridgeDelivery.objects.exists())

    def test_private_post_read_info_discards_body_and_records_source_reduction(self):
        self.member_info = self.bot_info
        self.member_page = {"ok": True, "messages": [{"ts": f"{self.now-10}.000001", "user": "USOURCE",
                                                  "text": "synthetic discarded body"}]}
        def convert(method):
            if method == "conversations_info" and len([c for c in self.calls if c[1] == method]) == 2:
                self.bot_info["channel"]["is_private"] = True
        self.member_hook = convert
        with self.assertRaises(BudgetDeferred):
            self.run_page()
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.hold_reason, "converted_to_private")
        self.assertEqual(self.reader_state()["effective_source"], "hold")


    def test_expired_lease_cannot_commit_no_reader_hold(self):
        self.left()
        from integrations.services.message_sync.scheduler import claim_job
        lease = claim_job(kinds=["archive"])
        self.state.jobs.filter(kind="archive").update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(LeaseLost):
            member.run_page(lease, self.state, lambda *args, **kwargs: self.fail("held page ran"))
        self.assertEqual(self.bot_calls, [])
        self.assertNotIn(access.COMMUNITY_READER_STATE, self.channel.room_state)

    def test_returning_reader_restarts_foreign_cursor_and_recovers_held_job(self):
        self.left()
        old = {"cursor": "LEGACY-BOT-CURSOR", "member_authority": "legacy-bot-authority"}
        self.state.jobs.filter(kind="archive").update(checkpoint=old)
        self.run_page()
        self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, old)
        self.joined()
        self.wake()
        self.run_page()
        kwargs = next(c[2] for c in self.calls if c[1] == "conversations_history")
        self.assertNotIn("cursor", kwargs)
        state = self.reader_state()
        self.assertEqual(state["effective_source"], "members")
        self.assertEqual(self.channel.sync_paused_reason, "")
        self.assertIsNotNone(state["last_fallback_at"])
        self.assertTrue(CommunityBridgeDelivery.objects.exists())
        self.assertEqual(self.bot_calls, [])

    def test_reader_loss_preserves_member_cursor_without_any_bot_read(self):
        self.member_page = {"ok": True, "messages": [], "response_metadata": {"next_cursor": "MEMBER-CURSOR"}}
        self.run_page()
        old = self.state.jobs.get(kind="archive").checkpoint
        self.left()
        self.wake()
        self.calls.clear()
        self.run_page()
        self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, old)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bot_calls, [])

    def test_both_history_keeps_bot_without_member_selection(self):
        self.bot.side_effect = None
        self.bot.return_value = self.sdk
        with override_settings(SLACK_COMMUNITY_SOURCE="both"), patch.object(access, "reader_for_channel", side_effect=AssertionError("both selected member")):
            self.run_page()
        self.assertEqual([c[0] for c in self.bot_calls], ["conversations_history"])

    def test_disabled_or_excluded_new_lane_keeps_legacy_bot_cursor(self):
        for changed in ({"SLACK_MEMBER_CHANNELS_ENABLED": False}, {"SLACK_MEMBER_CHANNEL_ALLOWED_IDS": "COTHER"}):
            with self.subTest(changed=changed), override_settings(SLACK_COMMUNITY_SOURCE="both", **changed):
                self.wake()
                self.state.jobs.filter(kind="archive").update(checkpoint={
                    "cursor": "LEGACY", "upper_bound": f"{self.now}.999999", "oldest": f"{self.now-100}.000000"})
                self.bot.side_effect = None
                self.bot.return_value = self.sdk
                self.bot_calls.clear()
                self.bot_page = {"ok": True, "messages": [], "response_metadata": {"next_cursor": "NEXT"}}
                self.run_page()
                self.assertEqual(self.bot_calls[0][1]["cursor"], "LEGACY")
                checkpoint = self.state.jobs.get(kind="archive").checkpoint
                self.assertNotIn("member_authority", checkpoint)
                self.assertEqual(checkpoint["cursor"], "NEXT")

    def test_unlinked_community_scroll_reports_unavailable_until_reader_returns(self):
        from integrations.services.slack_history_pages import _public_progress
        from integrations.services.slack_owner_inventory_api import InventoryError
        self.left()
        requester = get_user_model().objects.create_user(email="unlinked-reader@example.invalid")
        CommunityChatDevice.objects.create(user=requester, public_key="c" * 64, status="verified", verified_at=timezone.now())
        old = self.state.jobs.get(kind="archive").checkpoint
        with self.assertRaises(InventoryError):
            member.request_history(requester, self.channel, "c" * 64, "a" * 64, _public_progress)
        self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, old)
        self.joined()
        result = member.request_history(requester, self.channel, "c" * 64, "a" * 64, _public_progress)
        self.assertTrue(result["pending"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bot_calls, [])

    def test_unknown_scroll_reader_never_schedules_bot_or_borrows_cursor(self):
        from integrations.services.slack_history_pages import _public_progress
        from integrations.services.slack_owner_inventory_api import InventoryError
        SlackChannelMembership.objects.all().delete()
        before = self.state.jobs.get(kind="archive").checkpoint
        with self.assertRaises(InventoryError):
            member.request_history(self.a.user, self.channel, "1" * 64, "a" * 64, _public_progress)
        self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, before)
        self.assertEqual(self.bot_calls, [])

    def test_private_connect_or_members_never_take_zero_bot_path(self):
        self.left()
        for field, value in (("slack_visibility", "private"), ("hold_reason", "slack_connect"), ("audience", "members")):
            original = getattr(self.channel, field)
            setattr(self.channel, field, value)
            self.channel.save(update_fields=(field,))
            try:
                if field == "audience":
                    self.run_page()
                else:
                    with self.assertRaises(BudgetDeferred):
                        self.run_page()
                self.assertEqual(self.bot_calls, [])
            finally:
                setattr(self.channel, field, original)
                self.channel.save(update_fields=(field,))
                self.wake()

    def test_new_owner_during_member_info_or_history_discards_page(self):
        for stage in ("conversations_info", "conversations_history"):
            with self.subTest(stage=stage):
                self.wake()
                self.member_hook = lambda method: self.grant(3) if method == stage else None
                with self.assertRaises(CallbackAuthorityChanged):
                    self.run_page()
                self.assertFalse(CommunityBridgeDelivery.objects.exists())
                self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, {})
                self.assertEqual(self.bot_calls, [])

    def test_only_monitor_chain_can_extend_contiguous_fallback_or_stability(self):
        from integrations.services.community_bridge.community_source import MonitoringContinuity
        from integrations.services.community_bridge.community_source import codec

        self.left()
        proof = self.assessment()
        instant = timezone.now()
        def record(proof, at, continuous=True, token="a" * 64):
            with patch.object(access.timezone, "now", return_value=at):
                access.record_community_reader_state(self.channel, proof,
                    continuity=MonitoringContinuity(token, codec.stamp(at), continuous))
            return self.reader_state()
        first = record(proof, instant, False)["fallback_since"]
        second = record(proof, instant+timedelta(seconds=10))
        self.assertEqual(second["fallback_since"], first)
        self.assertTrue(second["continuity_proven"])
        # Callback without monitoring cannot extend the checked coverage time.
        checked = second["monitor_checked_ns"]
        with patch.object(access.timezone, "now", return_value=instant+timedelta(seconds=20)):
            access.record_community_reader_state(self.channel, proof)
        self.assertEqual(self.reader_state()["monitor_checked_ns"], checked)
        self.assertEqual(self.reader_state()["fallback_since"], first)
        reset = record(proof, instant+timedelta(seconds=201))
        self.assertNotEqual(reset["fallback_since"], first)
        changed_owner = record(proof, instant+timedelta(seconds=211), False, "b" * 64)
        self.assertNotEqual(changed_owner["fallback_since"], reset["fallback_since"])
        self.assertFalse(changed_owner["continuity_proven"])
        self.assertEqual(self.channel.room_state["preserve"], "unchanged")

    def test_brief_history_fallback_and_recovery_clear_prospective_epoch_between_ticks(self):
        from integrations.services.community_bridge import community_source as source
        from integrations.services.community_bridge import community_source_codec as codec

        # Synthetic persisted epoch exercises only the local transition hook;
        # it does not claim a real seven-day comparison or fourteen-day wait.
        now = timezone.now()
        stamp = codec.stamp(now)
        rooms = [source.room_identity(self.channel)] + [
            {"row_id": self.channel.pk+n, "workspace_id": "TTEST", "channel_id": f"CSYNTHETIC{n}",
             "destination_id": str(uuid.UUID(int=n))} for n in range(1,15)]
        value = dict(version=1, id=str(uuid.uuid4()), cohort=rooms,
            cohort_hash=codec.digest(b"mlai:community-source-cohort:v1\0", rooms),
            config="a"*64, source_binding="b"*64, mode="members", start_ns=stamp-1,
            last_ns=stamp, owner="c"*64, invalid_reason="", revision=1, closed_ns=stamp,
            members_since_ns=stamp-1, last_fallback_ns=0, report={}, monitor_config="d"*64,
            monitor_reason="", monitor_started_ns=stamp-1, comparison_covered_until_ns=stamp)
        self.assertIsNotNone(source.epoch(value))
        self.channel.room_state = {**self.channel.room_state, codec.EPOCH_KEY: value}
        self.channel.save(update_fields=("room_state",))
        self.left()
        with transaction.atomic():
            locked = type(self.channel).objects.select_for_update().get(pk=self.channel.pk)
            access.record_community_reader_state(locked, self.assessment())
        self.channel.refresh_from_db()
        fallback = source.epoch(self.channel.room_state[codec.EPOCH_KEY])
        self.assertEqual(fallback["members_since_ns"], 0)
        self.assertGreaterEqual(fallback["last_fallback_ns"], stamp)
        self.assertEqual(fallback["monitor_reason"], "no_reader")
        self.assertEqual(self.channel.room_state[access.COMMUNITY_READER_STATE]["fallback_reason"], "no_reader")
        self.joined()
        with transaction.atomic():
            locked = type(self.channel).objects.select_for_update().get(pk=self.channel.pk)
            access.record_community_reader_state(locked, self.assessment())
        self.channel.refresh_from_db()
        recovered = source.epoch(self.channel.room_state[codec.EPOCH_KEY])
        self.assertEqual(recovered["members_since_ns"], 0)
        self.assertEqual(recovered["last_fallback_ns"], fallback["last_fallback_ns"])
        self.assertEqual(self.channel.room_state[access.COMMUNITY_READER_STATE]["effective_source"], "members")
        # Only the heartbeat can start a fresh prospective window afterward.
        self.assertEqual(self.channel.room_state["preserve"], "unchanged")

    def test_mode_restart_and_missing_monitor_never_preserve_stable_duration(self):
        from integrations.services.community_bridge.community_source import MonitoringContinuity
        from integrations.services.community_bridge.community_source import codec

        proof = self.assessment()
        now = timezone.now()
        access.record_community_reader_state(self.channel, proof,
            continuity=MonitoringContinuity("a" * 64, codec.stamp(now), False))
        first = self.reader_state()["stable_since"]
        with override_settings(SLACK_COMMUNITY_SOURCE="bot"):
            access.record_community_reader_state(self.channel, self.assessment())
        access.record_community_reader_state(self.channel, proof)
        self.assertIsNone(self.reader_state()["stable_since"])
        with patch.object(access.timezone, "now", return_value=now+timedelta(seconds=200)):
            access.record_community_reader_state(self.channel, proof,
                continuity=MonitoringContinuity("a" * 64, codec.stamp(now+timedelta(seconds=200)), True))
        self.assertNotEqual(self.reader_state()["stable_since"], first)

    def test_callback_policy_has_no_payload_manufactured_zero_authority(self):
        self.left()
        payload = {"team_id": "TTEST", "api_app_id": "ABOT", "event": {"type": "message", "channel": "CROOM"}}
        self.assertIsNone(access.community_reader_policy_current(self.channel))
        with transaction.atomic(), callback_user_fence(payload), access.community_reader_policy(payload):
            proof = access.community_reader_policy_current(self.channel)
            self.assertEqual((proof.complete, proof.eligible_count), (True, 0))
        with override_settings(SLACK_COMMUNITY_SOURCE="bot"):
            self.assertEqual(access.community_reader_user_ids(payload), set())

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_known_owner_join_waits_through_zero_reader_page_commit(self):
        self.left()
        reading, attempted, joined = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def change():
            close_old_connections()
            try:
                self.assertTrue(reading.wait(5))
                attempted.set()
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.a.user_id)
                    SlackChannelMembership.objects.filter(grant_id=self.a.pk).update(state="joined", left_at=None, observed_at=timezone.now())
                joined.set()
            except Exception as error:
                errors.append(error)
            finally:
                close_old_connections()
        def during(method):
            if method == "conversations_history":
                reading.set()
                self.assertTrue(attempted.wait(5))
                self.assertFalse(joined.wait(.2))
        original_finish = member.finish_job
        def finish(*args, **kwargs):
            during("conversations_history")
            return original_finish(*args, **kwargs)
        finish_patch = patch.object(member, "finish_job", side_effect=finish)
        finish_patch.start()
        self.addCleanup(finish_patch.stop)
        thread = threading.Thread(target=change)
        thread.start()
        try:
            self.run_page()
        finally:
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(joined.is_set())
        self.assertEqual(self.assessment().eligible_count, 1)


    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_committed_new_owner_during_member_read_discards_whole_page(self):
        reading, committed = threading.Event(), threading.Event()
        errors = []
        def add():
            close_old_connections()
            try:
                self.assertTrue(reading.wait(5))
                self.grant(3)
                committed.set()
            except Exception as error:
                errors.append(error)
            finally:
                close_old_connections()
        def during(method):
            if method == "conversations_history":
                reading.set()
                self.assertTrue(committed.wait(5))
        self.member_hook = during
        thread = threading.Thread(target=add)
        thread.start()
        try:
            with self.assertRaises(CallbackAuthorityChanged):
                self.run_page()
        finally:
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.assessment().eligible_count, 3)
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.assertEqual(self.state.jobs.get(kind="archive").checkpoint, {})
