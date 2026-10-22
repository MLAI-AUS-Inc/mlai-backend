"""No-I/O public-consent, scope and failover boundaries."""

from contextlib import ExitStack, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from integrations.models import SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_access as access
from integrations.services import slack_owner_inventory as inventory
from integrations.services.community_bridge import member_channels as channels
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_chat_catalog import (
    ALL_HISTORY_CONSENT, PRIVATE_CHANNEL_CONSENT, PUBLIC_CHANNEL_SCOPES,
    public_channels_enabled,
)


def grant_fixture():
    connection = SimpleNamespace(
        pk=9, user_id=1, provider="slack", status="connected",
        access_token="synthetic-member-token", token_expires_at=None,
        scopes=sorted(PUBLIC_CHANNEL_SCOPES), external_account_id="TTEST",
        provider_metadata={"team": {"id": "TTEST"}, "authed_user": {"id": "UTEST"}},
    )
    return SimpleNamespace(
        pk=7, user_id=1, user=SimpleNamespace(pk=1, is_active=True),
        connection_id=9, connection=connection, consented_at=timezone.now(),
        status="active", revoked_at=None, slack_workspace_id="TTEST", slack_user_id="UTEST",
        consent_version=SlackDmMirrorGrant.CONSENT_VERSION,
    )


class PublicMemberConsentTests(SimpleTestCase):
    def setUp(self):
        self.grant = grant_fixture()
        epoch = patch("integrations.services.slack_oauth_authority.current_slack_oauth_generation", return_value=0)
        self.epoch = epoch.start()
        self.addCleanup(epoch.stop)
        # The read-only policy resolves the same current OAuth epoch through
        # slack_dm_mirror's imported alias. Keep both real DB seams forbidden.
        alias = patch.object(dm, "current_slack_oauth_generation", self.epoch)
        alias.start()
        self.addCleanup(alias.stop)

    def test_existing_accepted_consents_need_no_new_version_or_refresh(self):
        with patch.object(dm, "_refresh_slack_grant_token_if_due", side_effect=AssertionError("No refresh")):
            for version in (SlackDmMirrorGrant.CONSENT_VERSION, PRIVATE_CHANNEL_CONSENT, ALL_HISTORY_CONSENT):
                self.grant.consent_version = version
                self.assertTrue(public_channels_enabled(self.grant))
        for version in ("", "unknown", "slack-chat-v6-public-channels"):
            self.grant.consent_version = version
            self.assertFalse(public_channels_enabled(self.grant))

    def test_missing_malformed_or_mixed_scopes_fail_closed(self):
        for scopes in ([], ["channels:read", "channels:history"], "channels:read,channels:history,chat:write",
                       {"channels:read": True}, [*PUBLIC_CHANNEL_SCOPES, 42], None):
            self.grant.connection.scopes = scopes
            self.assertFalse(public_channels_enabled(self.grant))
        self.grant.connection.scopes = [f" {scope} " for scope in PUBLIC_CHANNEL_SCOPES] + ["chat:write"]
        self.assertTrue(public_channels_enabled(self.grant))

    def test_inactive_revoked_wrong_identity_and_oauth_epoch_are_ineligible(self):
        for target, attribute, value in (
            (self.grant, "status", "paused"), (self.grant, "revoked_at", timezone.now()),
            (self.grant.user, "is_active", False), (self.grant.connection, "user_id", 2),
            (self.grant.connection, "status", "disconnected"), (self.grant.connection, "provider", "github"),
            (self.grant, "slack_user_id", "UWRONG"), (self.grant, "slack_workspace_id", "TWRONG"),
        ):
            original = getattr(target, attribute)
            setattr(target, attribute, value)
            self.assertFalse(public_channels_enabled(self.grant))
            setattr(target, attribute, original)
        self.epoch.return_value = 1
        self.assertFalse(public_channels_enabled(self.grant))

    def test_stale_authority_cannot_reuse_a_changed_scope_or_consent(self):
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.grant.consent_version = PRIVATE_CHANNEL_CONSENT
        self.assertFalse(public_channels_enabled(self.grant, authority=authority))

    def test_inventory_extension_is_flag_gated_and_preserves_old_metadata_consent(self):
        authority = object()
        with patch.object(inventory, "has_metadata_consent", return_value=False), patch(
            "integrations.services.slack_chat_catalog.public_channels_enabled", return_value=True,
        ) as eligibility:
            with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False):
                self.assertFalse(inventory.public_inventory_allowed(self.grant, self.grant.connection, authority))
                eligibility.assert_not_called()
            with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=True):
                self.assertTrue(inventory.public_inventory_allowed(self.grant, self.grant.connection, authority))
        with override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False), patch.object(
            inventory, "has_metadata_consent", return_value=True,
        ):
            self.assertTrue(inventory.public_inventory_allowed(self.grant, self.grant.connection, authority))


class MemberInfoReaderHintSelectionTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = timezone.now()
        self.observed = self.now - timedelta(seconds=30)
        self.grants = [grant_fixture() for _ in range(3)]
        for pk, grant in enumerate(self.grants, 1):
            grant.pk = pk
            grant.consented_at = self.now - timedelta(minutes=1)
        self.arbitrary, self.hinted, self.joined = self.grants
        self.hint = channels.InfoReaderHint(self.hinted.pk, self.observed)

        self.grant_query = MagicMock()
        self.grant_query.select_related.return_value = self.grant_query
        self.grant_query.__iter__.side_effect = lambda: iter(self.grants)
        self.grant_query.first.return_value = self.hinted
        self.grant_filter = self.stack.enter_context(patch.object(
            access.SlackDmMirrorGrant.objects, "filter", return_value=self.grant_query,
        ))
        self.member_query = MagicMock()
        self.member_query.order_by.return_value = []
        self.stack.enter_context(patch.object(
            access.SlackChannelMembership.objects, "filter", return_value=self.member_query,
        ))
        self.channel_query = MagicMock()
        self.channel_query.values_list.return_value.first.return_value = None
        self.stack.enter_context(patch.object(
            access.CommunityBridgeChannel.objects, "filter", return_value=self.channel_query,
        ))
        self.stack.enter_context(patch.object(access, "enabled", return_value=True))
        self.scope = self.stack.enter_context(patch.object(channels, "member_channel_in_scope", return_value=True))
        self.eligible = self.stack.enter_context(patch.object(access, "public_reader_enabled", return_value=True))
        self.floor = self.stack.enter_context(patch(
            "integrations.services.slack_member_membership_epoch.membership_floor",
            return_value=self.now - timedelta(minutes=1),
        ))
        self.stack.enter_context(patch.object(access, "_key", side_effect=lambda grant, channel, purpose: (
            grant.pk, channel, purpose,
        )))
        self.cached = self.stack.enter_context(patch.object(access.cache, "get", return_value=None))
        self.stack.enter_context(patch.object(access, "_membership_state", side_effect=lambda grant, row: (
            row.state if row is not None else None
        )))
        self.provider = self.stack.enter_context(patch.object(dm, "_call_slack_with_grant_authority"))
        self.authority_lock = self.stack.enter_context(patch.object(dm, "_lock_slack_grant_api_authority"))

    def tearDown(self):
        self.provider.assert_not_called()
        self.authority_lock.assert_not_called()

    def select(self, **kwargs):
        return access.reader_for_channel("TTEST", "CTEST", purpose="info", info_reader_hint=self.hint, **kwargs)

    def test_unknown_channel_hint_beats_arbitrary_lower_id_without_joining(self):
        self.assertIs(self.select(), self.hinted)
        self.member_query.order_by.assert_called_once_with("-observed_at", "grant_id")

    def test_current_joined_pointer_and_observations_keep_priority_over_hint(self):
        self.member_query.order_by.return_value = [
            SimpleNamespace(grant_id=self.joined.pk, state="joined", observed_at=self.now),
            SimpleNamespace(grant_id=self.arbitrary.pk, state="joined", observed_at=self.observed),
        ]
        self.channel_query.values_list.return_value.first.return_value = self.arbitrary.pk
        self.assertIs(self.select(), self.arbitrary)
        self.channel_query.values_list.return_value.first.return_value = None
        self.assertIs(self.select(), self.joined)
        self.assertIs(self.select(exclude_grant_ids={self.joined.pk, self.arbitrary.pk}), self.hinted)

    def test_history_ignores_hint_and_requires_its_own_joined_observation(self):
        with patch.object(access, "_current_info_hint", side_effect=AssertionError("INFO hints cannot authorize history")):
            self.assertIsNone(access.reader_for_channel("TTEST", "CTEST", purpose="history", info_reader_hint=self.hint))
            self.member_query.order_by.return_value = [
                SimpleNamespace(grant_id=self.joined.pk, state="joined", observed_at=self.now),
            ]
            self.assertIs(access.reader_for_channel(
                "TTEST", "CTEST", purpose="history", info_reader_hint=self.hint,
            ), self.joined)

    def test_stale_missing_ineligible_and_excluded_hints_fall_back_to_current_reader(self):
        self.floor.return_value = self.observed + timedelta(microseconds=1)
        self.assertIs(self.select(), self.arbitrary)
        self.floor.return_value = self.now - timedelta(minutes=1)
        self.eligible.side_effect = lambda grant, **kwargs: grant.pk != self.hinted.pk
        self.assertIs(self.select(), self.arbitrary)
        self.eligible.side_effect = None
        self.assertIs(self.select(exclude_grant_ids={self.hinted.pk}), self.arbitrary)
        self.grants.remove(self.hinted)
        self.assertIs(self.select(), self.arbitrary)

    def test_separate_hint_resolution_binds_current_workspace_and_grant_eligibility(self):
        self.assertIs(access.hinted_info_reader("TTEST", "CTEST", self.hint), self.hinted)
        self.grant_filter.assert_called_once_with(
            pk=self.hinted.pk, slack_workspace_id="TTEST", status="active", revoked_at__isnull=True,
            user__is_active=True,
        )
        self.eligible.return_value = False
        self.assertIsNone(access.hinted_info_reader("TTEST", "CTEST", self.hint))
        self.eligible.return_value = True
        self.grant_query.first.return_value = None
        self.assertIsNone(access.hinted_info_reader("TTEST", "CTEST", self.hint))

    def test_separate_hint_resolution_rejects_stale_or_out_of_scope_authority(self):
        self.floor.return_value = self.observed + timedelta(microseconds=1)
        self.assertIsNone(access.hinted_info_reader("TTEST", "CTEST", self.hint))
        self.floor.return_value = None
        self.assertIsNone(access.hinted_info_reader("TTEST", "CTEST", self.hint))
        self.scope.return_value = False
        self.grant_filter.reset_mock()
        self.assertIsNone(access.hinted_info_reader("TTEST", "CTEST", self.hint))
        self.grant_filter.assert_not_called()

    def test_forged_or_malformed_hints_do_not_become_current_preferences(self):
        for hint in (None, SimpleNamespace(grant_id=self.hinted.pk, observed_at=self.observed),
                     channels.InfoReaderHint(self.arbitrary.pk, self.observed),
                     channels.InfoReaderHint(self.hinted.pk, None),
                     channels.InfoReaderHint(self.hinted.pk, "not-a-timestamp"),
                     channels.InfoReaderHint(self.hinted.pk, self.observed.replace(tzinfo=None)),
                     channels.InfoReaderHint(self.hinted.pk, self.now + timedelta(minutes=6))):
            with self.subTest(hint=hint):
                self.assertFalse(access._current_info_hint(self.hinted, hint))

    def test_cooling_hint_can_be_detected_without_provider_or_authority_locks(self):
        key = (self.hinted.pk, "CTEST", "info")
        self.cached.side_effect = lambda candidate: candidate == key
        self.assertTrue(access.info_reader_deferred(self.hinted, "CTEST"))
        self.assertFalse(access.info_reader_deferred(self.arbitrary, "CTEST"))


@override_settings(SLACK_MEMBER_CHANNELS_ENABLED=True, MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True)
class MemberClientScopeTests(SimpleTestCase):
    def setUp(self):
        PublicMemberConsentTests.setUp(self)
        self.grant._member_posting_public_key = "a" * 64
        self.client = access.member_client(self.grant)

    def test_missing_scope_never_enters_a_provider_call_or_authority_lock(self):
        with patch.object(dm, "_call_slack_with_grant_authority") as provider, patch.object(
            dm, "_lock_slack_grant_api_authority",
        ) as lock:
            with self.assertRaises(access.MemberAccessDenied):
                self.client.reactions_add(channel="CTEST", name="ok", timestamp="1")
            provider.assert_not_called()
            lock.assert_not_called()

    def test_public_boundary_rejects_private_conversation_and_workspace_ids(self):
        with patch.object(dm, "_call_slack_with_grant_authority") as provider, patch.object(
            dm, "_lock_slack_grant_api_authority",
        ) as lock:
            for channel in ("DTEST", "GTEST", "UTEST"):
                with self.assertRaises(access.MemberAccessDenied):
                    self.client.conversations_history(channel=channel)
                self.assertIsNone(access.reader_for_channel("TTEST", channel))
                self.assertIsNone(access.poster_for_author("a" * 64, "TTEST", channel))
            self.assertIsNone(access.reader_for_channel("XTEST", "CTEST"))
            self.assertIsNone(access.poster_for_author("a" * 64, "XTEST", "CTEST"))
            self.grant.slack_workspace_id = "XTEST"
            self.grant.connection.external_account_id = "XTEST"
            self.grant.connection.provider_metadata["team"]["id"] = "XTEST"
            with patch.object(dm, "_refresh_slack_grant_token_if_due") as refresh:
                with self.assertRaises(access.MemberAccessDenied):
                    access.member_client(self.grant).conversations_info(channel="CTEST")
                refresh.assert_not_called()
            provider.assert_not_called()
            lock.assert_not_called()

    def test_history_and_writes_require_joined_membership_at_call_time(self):
        for method in ("conversations_history", "chat_postMessage"):
            with self.subTest(method=method), patch.object(access.transaction, "atomic", nullcontext), patch.object(
                dm, "_lock_slack_grant_api_authority", return_value=(self.grant, self.grant.connection),
            ), patch.object(access, "_membership", return_value=None), patch.object(
                access, "_poster_identity_current", return_value=True,
            ), patch.object(dm, "_call_slack_with_grant_authority") as provider:
                with self.assertRaises(access.MemberAccessDenied):
                    getattr(self.client, method)(channel="CTEST")
                provider.assert_not_called()

    def test_posting_rechecks_the_verified_author_identity(self):
        with patch.object(access.transaction, "atomic", nullcontext), patch.object(
            dm, "_lock_slack_grant_api_authority", return_value=(self.grant, self.grant.connection),
        ), patch.object(access, "_poster_identity_current", return_value=False), patch.object(
            dm, "_call_slack_with_grant_authority",
        ) as provider:
            with self.assertRaises(access.MemberAccessDenied):
                self.client.chat_postMessage(channel="CTEST", text="synthetic")
            provider.assert_not_called()
        self.assertNotIn("synthetic-member-token", repr(self.client))

    def test_info_rejects_private_shared_and_malformed_responses(self):
        for raw in ({"id": "CTEST", "is_channel": True, "is_private": True},
                    {"id": "CTEST", "is_channel": True, "is_private": False, "is_ext_shared": True},
                    {"id": "CWRONG", "is_channel": True, "is_private": False}, None):
            with self.subTest(raw=raw), patch.object(access.transaction, "atomic", nullcontext), patch.object(
                dm, "_lock_slack_grant_api_authority", return_value=(self.grant, self.grant.connection),
            ), patch.object(dm, "_call_slack_with_grant_authority", return_value={"channel": raw}):
                with self.assertRaises(access.MemberAccessDenied):
                    self.client.conversations_info(channel="CTEST")

    def test_revoked_authority_never_calls_provider(self):
        with patch.object(access.transaction, "atomic", nullcontext), patch.object(
            dm, "_lock_slack_grant_api_authority", side_effect=dm.SlackDmMirrorAuthorizationError("changed"),
        ), patch.object(dm, "_call_slack_with_grant_authority") as provider:
            with self.assertRaises(dm.SlackDmMirrorAuthorizationError):
                self.client.conversations_info(channel="CTEST")
            provider.assert_not_called()

    def test_budget_deferral_is_not_a_failed_reader(self):
        with patch.object(access.cache, "set") as cache_write, patch.object(dm, "revoke_user_grant") as revoke:
            access.record_reader_failure(
                self.grant, BudgetDeferred(1), channel_id="CTEST", authority=self.client._authority,
            )
            cache_write.assert_not_called()
            revoke.assert_not_called()

    def test_provider_missing_scope_uses_existing_full_revoke_boundary(self):
        error = SlackApiError("synthetic", {"error": "missing_scope"})
        with patch.object(access.transaction, "atomic", nullcontext), patch.object(
            dm, "_lock_slack_grant_api_authority", return_value=(self.grant, self.grant.connection),
        ), patch.object(dm, "revoke_user_grant") as revoke:
            access.record_reader_failure(
                self.grant, error, channel_id="CTEST", authority=self.client._authority,
            )
        revoke.assert_called_once_with(self.grant.user)

    @override_settings(SLACK_OWNER_INVENTORY_ENABLED=False)
    def test_inventory_recovery_switch_also_stops_member_provider_activity(self):
        self.assertIsNone(access.reader_for_channel("TTEST", "CTEST"))
        self.assertIsNone(access.poster_for_author("a" * 64, "TTEST", "CTEST"))
        with self.assertRaises(access.MemberAccessDenied):
            self.client.conversations_info(channel="CTEST")
        with self.assertRaises(access.MemberAccessDenied):
            access.member_client(self.grant)

    @override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False)
    def test_disabled_lane_never_selects_or_creates_a_client(self):
        self.assertIsNone(access.reader_for_channel("TTEST", "CTEST"))
        self.assertIsNone(access.poster_for_author("a" * 64, "TTEST", "CTEST"))
        with self.assertRaises(access.MemberAccessDenied):
            access.member_client(self.grant)
