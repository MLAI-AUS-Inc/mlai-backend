"""Definitive public-member failures retain request-time ordering and authority."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from integrations.models import SlackChannelMembership
from integrations.services import slack_channel_membership as membership
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_access as access
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_oauth_authority import SLACK_OAUTH_GENERATION_KEY
from integrations.tests_slack_member_access import MemberAccessFixture


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True, MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST",
    SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MemberDepartureFailureDatabaseTests(MemberAccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.join(self.grant)
        self.channel.reader_grant = self.grant
        self.channel.audience_sync_pending = False
        self.channel.save(update_fields=("reader_grant", "audience_sync_pending"))

    def authority(self):
        return dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)

    def failure(self, code, **kwargs):
        access.record_reader_failure(
            self.grant, SlackApiError("synthetic", {"error": code}),
            channel_id="CROOM", authority=self.authority(), **kwargs,
        )

    def test_actual_call_uses_original_request_time_and_commits_pending_reduction(self):
        provider_times = []

        def departed(*args, **kwargs):
            provider_times.append(timezone.now())
            raise SlackApiError("synthetic", {"error": "not_in_channel"})

        self.provider.side_effect = departed
        before = timezone.now()
        with self.captureOnCommitCallbacks(execute=False) as callbacks, patch.object(
            access, "record_reader_failure", wraps=access.record_reader_failure,
        ) as record:
            with self.assertRaises(SlackApiError):
                access.member_client(self.grant).conversations_history(channel="CROOM", limit=1)
        started = record.call_args.kwargs["observed_at"]
        self.assertLessEqual(before, started)
        self.assertLessEqual(started, provider_times[0])
        row = SlackChannelMembership.objects.get(grant=self.grant, slack_channel_id="CROOM")
        self.assertEqual((row.state, row.source, row.observed_at, row.left_at),
                         ("left", "probe", started, started))
        self.assertEqual(access.cache.get(access._key(self.grant, "CROOM", "membership")), "left")
        self.assertEqual(access._membership_state(self.grant, row), "left")
        self.assertEqual(membership.consenting_members("TTEST", "CROOM"), [])
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertIsNone(self.channel.reader_grant_id)
        self.assertTrue(callbacks)  # Retained on-commit cleanup; no adapter is called here.

    def test_later_join_beats_delayed_failure_without_overwriting_reader_diagnostics(self):
        started = timezone.now()
        newer = started + timedelta(seconds=1)
        SlackChannelMembership.objects.filter(grant=self.grant).update(observed_at=newer)
        self.grant.last_error = "newer-proof"
        self.grant.save(update_fields=("last_error",))
        with patch.object(access.cache, "set") as cache_write:
            self.failure("not_in_channel", observed_at=started)
        cache_write.assert_not_called()
        row = SlackChannelMembership.objects.get(grant=self.grant)
        self.assertEqual((row.state, row.source, row.observed_at), ("joined", "event", newer))
        self.channel.refresh_from_db()
        self.grant.refresh_from_db()
        self.assertEqual(self.channel.reader_grant_id, self.grant.pk)
        self.assertFalse(self.channel.audience_sync_pending)
        self.assertEqual(self.grant.last_error, "newer-proof")

    def test_stale_consent_or_oauth_failure_has_no_membership_cache_or_pointer_mutation(self):
        original_consent = self.grant.consented_at
        original_metadata = dict(self.grant.connection.provider_metadata)
        for transition in ("consent", "oauth"):
            with self.subTest(transition=transition):
                authority = self.authority()
                started = timezone.now()
                if transition == "consent":
                    self.grant.consented_at = started
                    self.grant.save(update_fields=("consented_at",))
                else:
                    self.grant.connection.provider_metadata = {
                        **original_metadata, SLACK_OAUTH_GENERATION_KEY: authority.oauth_generation + 1,
                    }
                    self.grant.connection.save(update_fields=("provider_metadata",))
                with patch.object(access.cache, "set") as cache_write:
                    access.record_reader_failure(
                        self.grant, SlackApiError("synthetic", {"error": "not_in_channel"}),
                        channel_id="CROOM", authority=authority, observed_at=started,
                    )
                cache_write.assert_not_called()
                self.assertEqual(SlackChannelMembership.objects.get(grant=self.grant).state, "joined")
                self.channel.refresh_from_db()
                self.assertFalse(self.channel.audience_sync_pending)
                self.assertEqual(self.channel.reader_grant_id, self.grant.pk)
                self.grant.consented_at = original_consent
                self.grant.save(update_fields=("consented_at",))
                self.grant.connection.provider_metadata = original_metadata
                self.grant.connection.save(update_fields=("provider_metadata",))

    def test_channel_not_found_is_ambiguous_and_does_not_reduce_membership(self):
        self.failure("channel_not_found", observed_at=timezone.now())
        row = SlackChannelMembership.objects.get(grant=self.grant)
        self.assertEqual((row.state, row.source), ("joined", "event"))
        self.assertEqual([grant.pk for grant in membership.consenting_members("TTEST", "CROOM")], [self.grant.pk])
        self.assertIsNone(access.cache.get(access._key(self.grant, "CROOM", "membership")))
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.audience_sync_pending)

    def test_budget_deferral_never_records_departure_or_other_failure(self):
        with patch.object(access.cache, "set") as cache_write:
            access.record_reader_failure(
                self.grant, BudgetDeferred(1), channel_id="CROOM",
                authority=self.authority(), observed_at=timezone.now(),
            )
        cache_write.assert_not_called()
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.grant).state, "joined")
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.audience_sync_pending)
        self.assertEqual(self.channel.reader_grant_id, self.grant.pk)

    def test_definitive_failure_without_existing_observation_does_not_discover_membership(self):
        SlackChannelMembership.objects.filter(grant=self.grant).delete()
        self.failure("not_in_channel", observed_at=timezone.now())
        self.assertFalse(SlackChannelMembership.objects.exists())
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.audience_sync_pending)

    def test_failure_without_original_request_time_cannot_mint_departure(self):
        self.failure("not_in_channel")
        self.assertEqual(SlackChannelMembership.objects.get(grant=self.grant).state, "joined")
        self.assertIsNone(access.cache.get(access._key(self.grant, "CROOM", "membership")))
