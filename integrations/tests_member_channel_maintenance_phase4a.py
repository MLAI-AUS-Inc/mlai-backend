"""Known-source reductions, fenced refresh and durable authority transitions."""

from datetime import timedelta
from unittest.mock import patch
import uuid

from django.contrib import admin
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.admin_authority import MemberAudienceAuthorityAdminMixin
from integrations.models import CommunityBridgeChannel, CommunityBridgeChannelRefresh, SlackChannelMembership
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_access as access
from integrations.services.community_bridge import member_channels as channels
from integrations.services.community_bridge import member_channel_events as router
from integrations.services.community_bridge.buzz import BuzzBridgeError
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.tests_slack_member_access import MemberAccessFixture


@override_settings(MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
                   SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST",
                   SLACK_MEMBER_CHANNEL_ALLOWED_IDS="")
class MemberChannelMaintenanceTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.channel.destination_channel_id = str(uuid.uuid4())
        self.channel.room_state = {"applied": {"audience": "members"}}
        self.channel.slack_seen_at = timezone.now()
        self.channel.save()
        self.initial_channel_count = CommunityBridgeChannel.objects.count()

    def refresh(self, channel_id="CROOM"):
        channels.queue_refresh("TTEST", channel_id, reason="test")
        return self.process_queued()

    def process_queued(self):
        with patch.object(channels, "sync_room", return_value="synced") as room, patch(
            "integrations.services.community_bridge.member_audiences.sync_audience", return_value="applied",
        ) as audience:
            result = channels.process_refreshes_once()
        return result, room, audience

    def queue_actor(self, channel_id, *, event_type="message", grant=None):
        grant = grant or self.other
        observed = timezone.now()
        with override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID="AUSER"):
            result = router.route_member_public_event({
                "type": "event_callback", "api_app_id": "AUSER", "team_id": "TTEST",
                "event_time": int(observed.timestamp()), "event": {
                    "type": event_type, "channel": channel_id, "user": grant.slack_user_id,
                    "event_ts": f"{int(observed.timestamp())}.{observed.microsecond:06d}",
                },
            })
        self.assertIsNone(result)
        return CommunityBridgeChannelRefresh.objects.get(slack_channel_id=channel_id)

    def test_unknown_live_actor_converges_without_unrelated_reader_or_inventory(self):
        self.assertLess(self.grant.pk, self.other.pk)
        for kind, channel_id in (("message", "CMESSAGE"), ("member_joined_channel", "CJOIN")):
            with self.subTest(kind=kind):
                queued = self.queue_actor(channel_id, event_type=kind)
                self.assertEqual(channels.info_reader_hint(queued.reason).grant_id, self.other.pk)
                self.assertFalse(SlackChannelMembership.objects.filter(slack_channel_id=channel_id).exists())
                self.provider.reset_mock()
                self.provider.side_effect = lambda authority, method, **kwargs: self.info(
                    member=authority.grant_id == self.other.pk, id=channel_id,
                )
                completed, room, audience = self.refresh(channel_id)
                self.assertEqual(completed, 1)
                self.assertEqual([call.args[0].grant_id for call in self.provider.call_args_list], [self.other.pk])
                row = CommunityBridgeChannel.objects.get(slack_channel_id=channel_id)
                self.assertEqual((row.reader_grant_id, row.enabled, row.destination_channel_id),
                                 (self.other.pk, False, ""))
                self.assertTrue(row.audience_sync_pending)
                self.assertEqual(SlackChannelMembership.objects.get(slack_channel_id=channel_id).grant_id, self.other.pk)
                self.assertFalse(CommunityBridgeChannelRefresh.objects.filter(slack_channel_id=channel_id).exists())
                room.assert_called_once_with(row.pk)
                audience.assert_called_once_with(row.pk)

    def test_known_reader_keeps_priority_but_cannot_consume_untried_actor_hint(self):
        for a_member, channel_id in ((False, "CSTALE"), (True, "CKNOWN")):
            with self.subTest(a_member=a_member):
                self.join(self.grant, slack_channel_id=channel_id)
                self.queue_actor(channel_id)
                self.provider.reset_mock()
                self.provider.side_effect = lambda authority, method, **kwargs: self.info(
                    member=a_member if authority.grant_id == self.grant.pk else True, id=channel_id,
                )
                completed, _, _ = self.process_queued()
                self.assertEqual(completed, 1)
                self.assertEqual([call.args[0].grant_id for call in self.provider.call_args_list],
                                 [self.grant.pk, self.other.pk])
                self.assertEqual(SlackChannelMembership.objects.get(grant=self.grant, slack_channel_id=channel_id).state,
                                 "joined" if a_member else "left")
                self.assertEqual(SlackChannelMembership.objects.get(grant=self.other, slack_channel_id=channel_id).state, "joined")
                self.assertFalse(CommunityBridgeChannelRefresh.objects.filter(slack_channel_id=channel_id).exists())

    def test_cooling_actor_hint_is_durable_and_does_not_probe_arbitrary_reader(self):
        queued = self.queue_actor("CCOOLING")
        with patch.object(access, "info_reader_deferred", return_value=True):
            completed, room, audience = self.process_queued()
        self.assertEqual(completed, 0)
        queued.refresh_from_db()
        self.assertEqual((queued.reason, queued.attempts, queued.last_error_code),
                         (channels.info_reader_hint_reason(self.other.pk, channels.info_reader_hint(queued.reason).observed_at),
                          0, "info_reader_deferred"))
        self.provider.assert_not_called()
        room.assert_not_called()
        audience.assert_not_called()

    def test_budget_or_transient_failure_retains_exact_hint_without_abandonment(self):
        for error, channel_id in ((BudgetDeferred(90), "CBUDGET"),
                                  (dm.SlackApiError("synthetic", {"error": "request_timeout"}), "CTRANSIENT")):
            with self.subTest(error=type(error).__name__):
                queued = self.queue_actor(channel_id)
                original = queued.reason
                queued.attempts = channels.REFRESH_MAX_ATTEMPTS - 1
                queued.save(update_fields=("attempts",))
                self.provider.side_effect = error
                self.assertEqual(self.process_queued()[0], 0)
                queued.refresh_from_db()
                self.assertEqual((queued.reason, queued.attempts), (original, channels.REFRESH_MAX_ATTEMPTS - 1))
                self.assertGreater(queued.available_at, timezone.now())
                self.assertFalse(SlackChannelMembership.objects.filter(slack_channel_id=channel_id).exists())

    def test_paused_stale_or_malformed_hint_falls_back_without_inventing_membership(self):
        for mode, channel_id in (("paused", "CPAUSED"), ("stale", "COLDEPOCH"), ("malformed", "CMALFORMED")):
            with self.subTest(mode=mode):
                reason = channels.info_reader_hint_reason(self.other.pk, timezone.now())
                if mode == "paused":
                    self.other.status = "paused"
                    self.other.save(update_fields=("status",))
                elif mode == "stale":
                    reason = channels.info_reader_hint_reason(self.other.pk, self.other.consented_at - timedelta(microseconds=1))
                else:
                    reason = "i:01:1"
                channels.queue_refresh("TTEST", channel_id, reason=reason)
                self.provider.reset_mock()
                self.provider.side_effect = None
                self.provider.return_value = self.info(member=False, id=channel_id)
                self.assertEqual(self.process_queued()[0], 0)
                self.assertEqual([call.args[0].grant_id for call in self.provider.call_args_list], [self.grant.pk])
                self.assertFalse(CommunityBridgeChannel.objects.filter(slack_channel_id=channel_id).exists())
                self.assertFalse(SlackChannelMembership.objects.filter(grant=self.other, slack_channel_id=channel_id).exists())
                self.other.status = "active"
                self.other.save(update_fields=("status",))

    def test_hinted_private_or_shared_info_never_creates_public_room(self):
        for changes, channel_id in (({"is_private": True}, "CPRIVATE"), ({"is_shared": True}, "CSHARED")):
            with self.subTest(changes=changes):
                self.queue_actor(channel_id)
                self.provider.side_effect = None
                self.provider.return_value = self.info(id=channel_id, **changes)
                self.assertEqual(self.process_queued()[0], 1)
                self.assertFalse(CommunityBridgeChannel.objects.filter(slack_channel_id=channel_id).exists())
                self.assertFalse(SlackChannelMembership.objects.filter(slack_channel_id=channel_id).exists())

    def test_hint_does_not_bypass_disabled_flag_or_canary_scope(self):
        queued = self.queue_actor("CHINTSCOPE")
        for settings in ({"MESSAGE_SYNC_ENABLED": False}, {"SLACK_MEMBER_CHANNEL_ALLOWED_IDS": "COTHER"}):
            with override_settings(**settings):
                self.assertEqual(self.process_queued()[0], 0)
        self.provider.assert_not_called()
        self.assertFalse(SlackChannelMembership.objects.filter(slack_channel_id="CHINTSCOPE").exists())

    def test_refresh_uses_only_fenced_user_info_and_records_proven_membership(self):
        completed, room, audience = self.refresh()
        self.assertEqual(completed, 1)
        self.assertEqual(SlackChannelMembership.objects.get().state, "joined")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.reader_grant_id, self.grant.pk)
        self.assertTrue(self.channel.audience_sync_pending)
        room.assert_called_once_with(self.channel.pk)
        audience.assert_called_once_with(self.channel.pk)
        self.assertEqual(self.provider.call_args.args[1], "conversations_info")
        self.assertFalse(CommunityBridgeChannelRefresh.objects.exists())

    def test_unknown_info_reader_is_not_proof_of_membership_or_new_room(self):
        self.provider.return_value = self.info(member=False, id="CUNKNOWN")
        completed, room, audience = self.refresh("CUNKNOWN")
        self.assertEqual(completed, 0)
        self.assertFalse(CommunityBridgeChannel.objects.filter(slack_channel_id="CUNKNOWN").exists())
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")
        room.assert_not_called()
        audience.assert_not_called()

    def test_unknown_proven_join_creates_disabled_empty_members_room_before_sync(self):
        self.provider.return_value = self.info(id="CNEW")
        completed, room, audience = self.refresh("CNEW")
        self.assertEqual(completed, 1)
        row = CommunityBridgeChannel.objects.get(slack_channel_id="CNEW")
        self.assertEqual(row.audience, "members")
        self.assertFalse(row.enabled)
        self.assertEqual(row.destination_channel_id, "")
        self.assertTrue(row.audience_sync_pending)
        room.assert_called_once_with(row.pk)
        audience.assert_called_once_with(row.pk)

    def test_flag_off_and_outside_canary_never_probe_or_create(self):
        channels.queue_refresh("TTEST", "CUNKNOWN", reason="test")
        with override_settings(MESSAGE_SYNC_ENABLED=False):
            self.assertEqual(channels.process_refreshes_once(), 0)
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertEqual(channels.process_refreshes_once(), 0)
        self.provider.assert_not_called()
        self.assertEqual(CommunityBridgeChannel.objects.count(), self.initial_channel_count)

    def test_private_conversion_closes_known_community_bot_lane_without_audience_conversion(self):
        self.channel.audience = "community"
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.save()
        self.provider.return_value = self.info(is_private=True)
        completed, room, audience = self.refresh()
        self.assertEqual(completed, 1)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertEqual((self.channel.audience, self.channel.slack_visibility), ("community", "private"))
        self.assertEqual(self.channel.hold_reason, "converted_to_private")
        self.assertFalse(self.channel.audience_sync_pending)
        self.assertTrue(channels._desired_room(self.channel)["archived"])
        self.assertFalse(SlackChannelMembership.objects.exists())

    def test_incomplete_or_wrong_info_privacy_keeps_existing_room_closed_to_new_keys(self):
        self.provider.return_value = {"ok": True, "channel": {"id": "CROOM", "is_channel": True}}
        completed, room, audience = self.refresh()
        self.assertEqual(completed, 0)
        room.assert_not_called()
        audience.assert_not_called()
        self.assertEqual(CommunityBridgeChannelRefresh.objects.get().attempts, 1)
        self.assertFalse(SlackChannelMembership.objects.exists())

    def test_reduction_controls_work_off_flags_and_never_discover_unknown_channels(self):
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False, MESSAGE_SYNC_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False, SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertFalse(channels.mark_source_reduction("TTEST", "CUNKNOWN", reason="deleted"))
            with patch(
                "integrations.services.community_bridge.member_audiences.sync_audience", return_value="applied",
            ) as audience, self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(channels.mark_source_reduction("TTEST", "CROOM", reason="slack_connect"))
                self.channel.refresh_from_db()
                self.assertFalse(self.channel.enabled)
            audience.assert_called_once_with(self.channel.pk, reduction_only=True)
        self.assertEqual(CommunityBridgeChannel.objects.count(), self.initial_channel_count)
        self.assertTrue(channels._desired_room(self.channel)["archived"])
        self.provider.assert_not_called()

    def test_member_left_wakes_without_holding_entire_room(self):
        self.assertTrue(channels.mark_source_reduction("TTEST", "CROOM", reason="member_left"))
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertFalse(self.channel.enabled)
        self.assertEqual(self.channel.hold_reason, "")

    def test_archive_control_preserves_operator_mirror_intent_for_later_verified_unarchive(self):
        self.assertTrue(channels.mark_source_reduction("TTEST", "CROOM", reason="archived"))
        self.channel.refresh_from_db()
        self.assertEqual((self.channel.mirror_decision, self.channel.hold_reason), ("mirror", ""))
        self.assertTrue(self.channel.slack_archived)

    def test_known_community_archive_runs_off_flags_without_provision_or_metadata_expansion(self):
        self.channel.audience = "community"
        self.channel.destination_channel_name = "existing-room"
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.save()
        channels.mark_source_reduction("TTEST", "CROOM", reason="converted_to_private")
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False), patch.object(
            channels.BuzzBridgeClient, "upsert_public_channel", return_value={
                "channel_id": self.channel.destination_channel_id, "created": False, "changed": ["archived"],
            },
        ) as adapter:
            self.assertEqual(channels.process_room_reductions_once(), 1)
        self.assertEqual(adapter.call_args.kwargs["audience"], "community")
        self.assertTrue(adapter.call_args.kwargs["archived"])
        self.assertEqual(adapter.call_args.kwargs["name"], "existing-room")
        self.assertIsNone(adapter.call_args.kwargs["about"])
        self.assertIsNone(adapter.call_args.kwargs["topic"])
        self.provider.assert_not_called()

    def test_older_control_cannot_override_newer_authoritative_public_snapshot(self):
        self.assertFalse(channels.mark_source_reduction(
            "TTEST", "CROOM", reason="converted_to_private",
            event_observed_at=self.channel.slack_seen_at - timedelta(seconds=1),
        ))
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.enabled)
        self.assertEqual(self.channel.slack_visibility, "public")

    def test_private_provider_response_rechecks_original_authority_before_closing_source(self):
        def reconsent_during_info(*args, **kwargs):
            self.grant.consented_at = timezone.now()
            self.grant.save(update_fields=("consented_at",))
            return self.info(is_private=True)

        self.provider.side_effect = reconsent_during_info
        completed, room, audience = self.refresh()
        self.assertEqual(completed, 0)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.slack_visibility, "public")
        self.assertTrue(self.channel.enabled)
        room.assert_not_called()
        audience.assert_not_called()

    def test_failed_existing_room_archive_rotates_without_clearing_pending(self):
        self.channel.audience = "community"
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.slack_visibility = "private"
        self.channel.room_sync_pending = True
        self.channel.save()
        other = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="COTHER", audience="community",
            destination_platform="buzz", destination_channel_id=str(uuid.uuid4()),
            room_state={"applied": {"audience": "community"}},
            slack_visibility="private", room_sync_pending=True,
        )
        with patch.object(channels.BuzzBridgeClient, "upsert_public_channel", side_effect=BuzzBridgeError("synthetic")) as adapter:
            self.assertEqual(channels.process_room_reductions_once(limit=1), 0)
            self.assertEqual(channels.process_room_reductions_once(limit=1), 0)
        self.assertEqual([call.kwargs["channel_id"] for call in adapter.call_args_list],
                         [self.channel.destination_channel_id, other.destination_channel_id])
        self.channel.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(self.channel.room_sync_pending and other.room_sync_pending)

    def test_admin_hold_wakes_reduction_and_cannot_delete_recoverable_mapping(self):
        from django.core.exceptions import ValidationError
        from integrations.admin import CommunityBridgeChannelAdmin

        model_admin = CommunityBridgeChannelAdmin(CommunityBridgeChannel, admin.site)
        with patch.object(model_admin, "message_user"), patch(
            "integrations.services.community_bridge.member_audiences.sync_audience", return_value="applied",
        ) as audience, self.captureOnCommitCallbacks(execute=True):
            model_admin.hold_channels(None, CommunityBridgeChannel.objects.filter(pk=self.channel.pk))
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertTrue(self.channel.audience_sync_pending)
        audience.assert_called_once_with(self.channel.pk, reduction_only=True)
        self.assertFalse(model_admin.has_delete_permission(None, self.channel))
        with self.assertRaises(ValidationError):
            model_admin.delete_model(None, self.channel)
        with self.assertRaises(ValidationError):
            model_admin.delete_queryset(None, CommunityBridgeChannel.objects.filter(pk=self.channel.pk))
        self.assertTrue(CommunityBridgeChannel.objects.filter(pk=self.channel.pk).exists())

    def test_admin_cannot_manufacture_owner_consent_or_verified_device_authority(self):
        from integrations.admin import ExternalServiceConnectionAdmin, SlackDmMirrorGrantAdmin
        from community_chat.admin import CommunityChatDeviceAdmin

        grant_admin = SlackDmMirrorGrantAdmin(type(self.grant), admin.site)
        self.assertFalse(grant_admin.has_add_permission(None))
        self.assertTrue({"user", "connection", "consent_version", "history_days", "status"}.issubset(grant_admin.readonly_fields))
        device_admin = CommunityChatDeviceAdmin(type(self.device), admin.site)
        self.assertFalse(device_admin.has_add_permission(None))
        self.assertTrue({"user", "public_key", "status"}.issubset(device_admin.readonly_fields))
        connection_admin = ExternalServiceConnectionAdmin(type(self.grant.connection), admin.site)
        self.assertTrue({"sync_cursor", "scopes", "provider_metadata"}.issubset(
            connection_admin.get_readonly_fields(None, self.grant.connection),
        ))

    def test_stale_reduction_observation_does_not_overwrite_newer_control(self):
        now = timezone.now()
        self.assertTrue(channels.mark_source_reduction("TTEST", "CROOM", reason="deleted", event_observed_at=now))
        self.assertFalse(channels.mark_source_reduction("TTEST", "CROOM", reason="archived",
                                                       event_observed_at=now - timedelta(seconds=1)))
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.slack_deleted)
        self.assertFalse(self.channel.slack_archived)

    def test_legacy_logical_members_actual_community_never_gets_audience_pending(self):
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.save()
        self.assertEqual(channels.mark_member_channels_pending(self.grant.user_id, reason="test"), 0)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.audience_sync_pending)

    def test_pause_commits_pending_and_attempts_only_subtraction_after_commit(self):
        with patch(
            "integrations.services.community_bridge.member_audiences.sync_audience", return_value="retry",
        ) as audience, self.captureOnCommitCallbacks(execute=True):
            dm.pause_grant(self.grant)
        self.grant.refresh_from_db()
        self.channel.refresh_from_db()
        self.assertEqual(self.grant.status, "paused")
        self.assertTrue(self.channel.audience_sync_pending)
        audience.assert_called_once_with(self.channel.pk, reduction_only=True)
        self.provider.assert_not_called()

    def test_cleanup_outage_preserves_local_denial_and_pending(self):
        with patch(
            "integrations.services.community_bridge.member_audiences.sync_audience", side_effect=RuntimeError("synthetic"),
        ), self.captureOnCommitCallbacks(execute=True):
            dm.pause_grant(self.grant)
        self.grant.refresh_from_db()
        self.channel.refresh_from_db()
        self.assertEqual(self.grant.status, "paused")
        self.assertTrue(self.channel.audience_sync_pending)

    def test_admin_grant_cascade_captures_destination_before_membership_disappears(self):
        self.join(self.grant)

        class GrantAdmin(MemberAudienceAuthorityAdminMixin, admin.ModelAdmin):
            pass

        with patch(
            "integrations.services.community_bridge.member_audiences.sync_audience", return_value="applied",
        ) as audience, self.captureOnCommitCallbacks(execute=True):
            GrantAdmin(type(self.grant), admin.site).delete_model(None, self.grant)
        self.assertFalse(SlackChannelMembership.objects.exists())
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        audience.assert_called_once_with(self.channel.pk, reduction_only=True)
