"""Existing Slack consent survives while interrupted membership proof expires."""

from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from integrations.models import SlackChannelMembership
from integrations.services import slack_channel_membership as membership
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_access as access
from integrations.services import slack_member_membership_epoch as epoch
from integrations.tests_slack_member_access import MemberAccessFixture


class MemberMembershipFloorTests(SimpleTestCase):
    def test_floor_is_at_least_existing_consent_without_new_consent_version(self):
        now = timezone.now()
        grant = SimpleNamespace(consented_at=now, connection=SimpleNamespace(sync_cursor={}))
        self.assertEqual(epoch.membership_floor(grant), now)
        grant.connection.sync_cursor[epoch.KEY] = {"not_before": (now - timedelta(days=1)).isoformat()}
        self.assertEqual(epoch.membership_floor(grant), now)
        grant.connection.sync_cursor[epoch.KEY] = {"not_before": (now + timedelta(seconds=1)).isoformat()}
        self.assertEqual(epoch.membership_floor(grant), now + timedelta(seconds=1))

    def test_corrupt_or_naive_barrier_denies_proof_instead_of_restoring_old_membership(self):
        now = timezone.now()
        for raw in (None, "bad", {}, {"not_before": "invalid"}, {"not_before": "2026-99-99T00:00:00Z"},
                    {"not_before": now.replace(tzinfo=None).isoformat()}):
            grant = SimpleNamespace(consented_at=now, connection=SimpleNamespace(sync_cursor={epoch.KEY: raw}))
            self.assertIsNone(epoch.membership_floor(grant))


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST",
    SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MemberMembershipEpochDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.grant.connection.scopes = sorted(set(self.grant.connection.scopes) | dm.DIRECT_DM_SCOPES)
        self.grant.connection.save(update_fields=("scopes",))
        self.observed = timezone.now()
        self.assertTrue(membership.mark_joined(self.grant, "CROOM", observed_at=self.observed, public_verified=True))

    def refresh(self):
        self.grant.refresh_from_db()
        self.grant.connection.refresh_from_db()

    def test_actual_pause_resume_requires_fresh_membership_without_changing_consent(self):
        consent = self.grant.consented_at
        version = self.grant.consent_version
        dm.pause_grant(self.grant)
        dm.resume_grant(self.grant)
        self.refresh()
        self.assertEqual((self.grant.consented_at, self.grant.consent_version), (consent, version))
        self.assertGreater(epoch.membership_floor(self.grant), self.observed)
        self.assertEqual(membership.consenting_members("TTEST", "CROOM"), [])
        self.assertIsNone(access.reader_for_channel("TTEST", "CROOM"))
        self.assertFalse(membership.mark_joined(self.grant, "CROOM", observed_at=self.observed,
                                               public_verified=True))
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.assertEqual(membership.reconcile_from_inventory(
            self.grant, ["CROOM"], True, started_at=self.observed, authority=authority,
        ), {"joined": 0, "left": 0})
        fresh = max(timezone.now(), epoch.membership_floor(self.grant) + timedelta(seconds=1))
        self.assertEqual(membership.reconcile_from_inventory(
            self.grant, ["CROOM"], True, started_at=fresh, authority=authority,
        ), {"joined": 1, "left": 0})
        self.assertEqual([grant.pk for grant in membership.consenting_members("TTEST", "CROOM")], [self.grant.pk])
        self.provider.assert_not_called()

    def test_barrier_is_monotonic_preserves_other_cursor_state_and_invalidates_future_old_proof(self):
        future = timezone.now() + timedelta(minutes=1)
        SlackChannelMembership.objects.filter(grant=self.grant).update(observed_at=future)
        self.grant.connection.sync_cursor = {"unrelated_metadata": {"revision": 7}}
        self.grant.connection.save(update_fields=("sync_cursor",))
        with transaction.atomic():
            epoch.invalidate_memberships_locked(self.grant.user_id)
        self.refresh()
        first = epoch.membership_floor(self.grant)
        self.assertGreater(first, future)
        self.assertEqual(self.grant.connection.sync_cursor["unrelated_metadata"], {"revision": 7})
        with transaction.atomic():
            epoch.invalidate_memberships_locked(self.grant.user_id)
        self.refresh()
        self.assertGreater(epoch.membership_floor(self.grant), first)
        self.assertIsNone(access._membership_state(self.grant, SlackChannelMembership.objects.get(grant=self.grant)))

    def test_old_floor_does_not_block_a_known_source_leave_reduction(self):
        with transaction.atomic():
            epoch.invalidate_memberships_locked(self.grant.user_id)
        self.assertTrue(membership.mark_left(self.grant, "CROOM", observed_at=self.observed))
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.grant).state, "left")

    def test_private_only_empty_connection_is_inert_but_existing_epoch_is_retained(self):
        SlackChannelMembership.objects.filter(grant=self.grant).delete()
        self.grant.connection.scopes = sorted(dm.DIRECT_DM_SCOPES)
        self.grant.connection.sync_cursor = {}
        self.grant.connection.save(update_fields=("scopes", "sync_cursor"))
        with transaction.atomic():
            self.assertEqual(epoch.invalidate_memberships_locked(self.grant.user_id), 0)
        self.refresh()
        self.assertEqual(self.grant.connection.sync_cursor, {})
        self.grant.connection.sync_cursor = {epoch.KEY: {"not_before": self.observed.isoformat()}}
        self.grant.connection.save(update_fields=("sync_cursor",))
        with transaction.atomic():
            self.assertEqual(epoch.invalidate_memberships_locked(self.grant.user_id), 1)
        self.refresh()
        self.assertGreater(epoch.membership_floor(self.grant), self.observed)

    def test_public_scope_first_proof_is_fenced_even_across_flag_off_pause_resume(self):
        SlackChannelMembership.objects.filter(grant=self.grant).delete()
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False):
            dm.pause_grant(self.grant)
            dm.resume_grant(self.grant)
        self.refresh()
        self.assertGreater(epoch.membership_floor(self.grant), self.observed)
        self.assertFalse(membership.mark_joined(self.grant, "CROOM", observed_at=self.observed,
                                               public_verified=True))
        self.assertEqual(membership.consenting_members("TTEST", "CROOM"), [])

    def test_admin_save_keeps_private_only_empty_cursor_inert(self):
        from django.contrib import admin
        from integrations.admin import ExternalServiceConnectionAdmin

        SlackChannelMembership.objects.filter(grant=self.grant).delete()
        connection = self.grant.connection
        connection.scopes = sorted(dm.DIRECT_DM_SCOPES)
        connection.sync_cursor = {}
        connection.save(update_fields=("scopes", "sync_cursor"))
        model_admin = ExternalServiceConnectionAdmin(type(connection), admin.site)
        model_admin.save_model(None, connection, None, change=True)
        connection.refresh_from_db()
        self.assertEqual(connection.sync_cursor, {})
