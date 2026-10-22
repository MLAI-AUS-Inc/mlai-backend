"""Synthetic public membership ordering, authority and audience-wake contracts."""

from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from integrations.models import SlackChannelMembership
from integrations.services import slack_channel_membership as membership
from integrations.services import slack_dm_mirror as dm
from integrations.services.community_bridge import member_channels
from integrations.tests_slack_member_access import MemberAccessFixture


class MembershipObservationValidationTests(SimpleTestCase):
    def test_observation_requires_known_source_aware_time_and_bounded_future(self):
        now = timezone.now()
        self.assertTrue(membership._valid_observation(now, "event"))
        for observed, source in ((now, "callback-controlled"), (now.replace(tzinfo=None), "event"),
                                 (None, "event"), (now + timedelta(minutes=6), "message")):
            self.assertFalse(membership._valid_observation(observed, source))


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST",
    SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PublicMembershipDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def joined(self, grant=None, channel="CROOM", **kwargs):
        return membership.mark_joined(grant or self.grant, channel, observed_at=self.now,
                                      public_verified=True, **kwargs)

    def test_public_identity_or_callback_visibility_alone_never_creates_membership(self):
        for channel in ("CROOM", "DROOM", "GROOM"):
            self.assertFalse(membership.mark_joined(self.grant, channel, observed_at=self.now))
        self.assertFalse(SlackChannelMembership.objects.exists())
        self.assertFalse(self.joined(channel="GROOM"))
        self.provider.assert_not_called()

    def test_accepted_join_and_leave_set_pending_in_same_transaction(self):
        self.assertTrue(self.joined())
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        self.channel.audience_sync_pending = False
        self.channel.save(update_fields=("audience_sync_pending",))
        self.assertTrue(membership.mark_left(self.grant, "CROOM", observed_at=self.now + timedelta(seconds=1)))
        row = SlackChannelMembership.objects.get()
        self.assertEqual((row.state, row.left_at), ("left", self.now + timedelta(seconds=1)))
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        self.provider.assert_not_called()

    def test_pending_wake_failure_rolls_back_membership(self):
        with patch.object(member_channels, "mark_member_channels_pending", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.joined()
        self.assertFalse(SlackChannelMembership.objects.exists())

    def test_newer_leave_and_equal_time_leave_defeat_delayed_join(self):
        self.assertTrue(self.joined())
        self.assertTrue(membership.mark_left(self.grant, "CROOM", observed_at=self.now))
        self.assertFalse(self.joined(source="message"))
        self.assertFalse(membership.mark_joined(
            self.grant, "CROOM", observed_at=self.now - timedelta(seconds=1), public_verified=True,
        ))
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")
        self.assertTrue(membership.mark_joined(
            self.grant, "CROOM", observed_at=self.now + timedelta(seconds=1), public_verified=True,
        ))
        self.assertFalse(membership.mark_left(self.grant, "CROOM", observed_at=self.now))

    def test_preconsent_stale_and_other_grant_authority_cannot_add(self):
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.grant.consented_at = self.now + timedelta(seconds=1)
        self.grant.save(update_fields=("consented_at",))
        self.assertFalse(self.joined(authority=authority))
        other_authority = dm._capture_slack_grant_api_authority(self.other, refresh_token=False)
        self.assertFalse(self.joined(authority=other_authority))
        self.assertFalse(SlackChannelMembership.objects.exists())

    def test_proven_pre_room_leave_prevents_delayed_older_join(self):
        self.assertFalse(membership.mark_left(self.grant, "CUNKNOWN", observed_at=self.now))
        self.assertTrue(membership.mark_left(
            self.grant, "CUNKNOWN", observed_at=self.now, public_verified=True,
        ))
        self.assertFalse(membership.mark_joined(
            self.grant, "CUNKNOWN", observed_at=self.now - timedelta(seconds=1), public_verified=True,
        ))
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False):
            self.assertFalse(membership.mark_left(
                self.grant, "COTHER", observed_at=self.now, public_verified=True,
            ))

    def test_additions_obey_every_flag_and_canary_but_existing_reductions_do_not(self):
        for setting in ("MESSAGE_SYNC_ENABLED", "SLACK_MEMBER_CHANNELS_ENABLED", "SLACK_OWNER_INVENTORY_ENABLED"):
            with override_settings(**{setting: False}):
                self.assertFalse(self.joined())
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertFalse(self.joined())
        self.assertTrue(self.joined())
        self.grant.status = "paused"
        self.grant.save(update_fields=("status",))
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False, SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertTrue(membership.mark_left(self.grant, "CROOM", observed_at=self.now + timedelta(seconds=1)))
            self.assertFalse(membership.mark_left(self.grant, "CUNKNOWN", observed_at=self.now))
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")

    def test_incomplete_or_stale_inventory_never_overwrites_later_membership(self):
        self.assertTrue(self.joined())
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        result = membership.reconcile_from_inventory(
            self.grant, [], False, started_at=self.now + timedelta(seconds=1), authority=authority,
        )
        self.assertEqual(result, {"joined": 0, "left": 0})
        self.assertEqual(SlackChannelMembership.objects.get().state, "joined")
        membership.reconcile_from_inventory(self.grant, [], True, started_at=self.now - timedelta(seconds=1), authority=authority)
        self.assertEqual(SlackChannelMembership.objects.get().state, "joined")
        membership.mark_left(self.grant, "CROOM", observed_at=self.now + timedelta(seconds=2))
        membership.reconcile_from_inventory(self.grant, ["CROOM"], True, started_at=self.now, authority=authority)
        self.assertEqual(SlackChannelMembership.objects.get().state, "left")

    def test_complete_inventory_reconciles_only_current_cohort(self):
        self.assertTrue(self.joined())
        self.assertTrue(self.joined(channel="COUTSIDE"))
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        with override_settings(SLACK_MEMBER_CHANNEL_ALLOWED_IDS="CROOM,CNEW"):
            counts = membership.reconcile_from_inventory(
                self.grant, ["CNEW"], True, started_at=self.now + timedelta(seconds=1), authority=authority,
            )
        self.assertEqual(counts, {"joined": 1, "left": 1})
        self.assertEqual(dict(SlackChannelMembership.objects.values_list("slack_channel_id", "state")),
                         {"CROOM": "left", "COUTSIDE": "joined", "CNEW": "joined"})

    def test_consenting_reader_snapshot_ignores_expansion_flags_and_ai_attribution(self):
        self.assertTrue(self.joined())
        self.link.delete()
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False, SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertEqual([grant.pk for grant in membership.consenting_members("TTEST", "CROOM")], [self.grant.pk])
        self.grant.status = "revoked"
        self.grant.save(update_fields=("status",))
        self.assertEqual(membership.consenting_members("TTEST", "CROOM"), [])
