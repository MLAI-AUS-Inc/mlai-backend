"""Synthetic DB fixtures for public member selection and live privacy fences."""

from datetime import timedelta
import threading
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.models import CommunityChatDevice
from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink, ExternalServiceConnection,
    SlackChannelMembership, SlackDmMirrorGrant, SlackOwnerConversationInventory,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_access as access
from integrations.services import slack_owner_inventory as inventory
from integrations.services.slack_chat_catalog import PUBLIC_CHANNEL_SCOPES


class MemberAccessFixture:
    def setUp(self):
        access.cache.clear()
        self.grant = self.make_grant(1)
        self.other = self.make_grant(2)
        self.channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="CROOM",
            destination_platform="buzz", destination_channel_id="synthetic-room",
        )
        self.device = CommunityChatDevice.objects.create(
            user=self.grant.user, public_key="a" * 64, status="verified", verified_at=timezone.now(),
        )
        self.link = CommunityBridgeIdentityLink.objects.create(
            user=self.grant.user, slack_workspace_id="TTEST", slack_user_id=self.grant.slack_user_id,
            buzz_pubkey="b" * 64, display_name="Synthetic member", verification_method="account_challenge",
            verification_reference="member-access-fixture", verified_at=timezone.now(),
        )
        provider = patch.object(dm, "_call_slack_with_grant_authority", return_value=self.info())
        self.provider = provider.start()
        self.addCleanup(provider.stop)

    def make_grant(self, number):
        user = get_user_model().objects.create_user(email=f"member-{number}@example.invalid")
        connection = ExternalServiceConnection.objects.create(
            user=user, provider="slack", external_account_id="TTEST", status="connected",
            access_token=f"synthetic-member-token-{number}", scopes=sorted(PUBLIC_CHANNEL_SCOPES),
            provider_metadata={"team": {"id": "TTEST"}, "authed_user": {"id": f"UMEMBER{number}"}},
        )
        return SlackDmMirrorGrant.objects.create(
            user=user, connection=connection, slack_workspace_id="TTEST", slack_user_id=f"UMEMBER{number}",
            consented_at=timezone.now() - timedelta(minutes=1),
        )

    def join(self, grant, **changes):
        return SlackChannelMembership.objects.create(
            grant=grant, slack_workspace_id="TTEST",
            **{"slack_channel_id": "CROOM", "state": "joined", "source": "event", "observed_at": timezone.now(), **changes},
        )

    def info(self, *, member=True, **changes):
        return {"ok": True, "channel": {
            "id": "CROOM", "is_channel": True, "is_private": False, "is_member": member, **changes,
        }}

    def poster(self):
        return access.poster_for_author(self.device.public_key, "TTEST", "CROOM")


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MemberAccessDatabaseTests(MemberAccessFixture, TestCase):
    def test_reader_prefers_current_pointer_then_observed_members_without_attribution_link(self):
        self.join(self.grant, observed_at=timezone.now() - timedelta(seconds=10))
        self.join(self.other)
        self.channel.reader_grant = self.grant
        self.channel.save(update_fields=("reader_grant",))
        self.link.delete()
        self.assertEqual(access.reader_for_channel("TTEST", "CROOM").pk, self.grant.pk)
        self.assertEqual(access.reader_for_channel("TTEST", "CROOM", exclude_grant_ids={self.grant.pk}).pk, self.other.pk)
        self.provider.assert_not_called()

    def test_reader_skips_revoked_wrong_identity_missing_scope_and_stale_consent(self):
        self.join(self.grant)
        self.join(self.other)
        for attribute, value in (("revoked_at", timezone.now()), ("status", "paused")):
            original = getattr(self.grant, attribute)
            SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(**{attribute: value})
            self.assertEqual(access.reader_for_channel("TTEST", "CROOM").pk, self.other.pk)
            SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(**{attribute: original})
        self.grant.connection.scopes = ["channels:read"]
        self.grant.connection.save(update_fields=("scopes",))
        self.assertEqual(access.reader_for_channel("TTEST", "CROOM").pk, self.other.pk)
        self.other.connection.provider_metadata = {"team": {"id": "TWRONG"}, "authed_user": {"id": "UMEMBER2"}}
        self.other.connection.save(update_fields=("provider_metadata",))
        self.assertIsNone(access.reader_for_channel("TTEST", "CROOM"))
        self.provider.assert_not_called()

    def test_info_fallback_never_becomes_unjoined_history_authority(self):
        self.assertIsNone(access.reader_for_channel("TTEST", "CROOM", purpose="history"))
        reader = access.reader_for_channel("TTEST", "CROOM", purpose="info")
        self.assertIsNotNone(reader)
        client = access.member_client(reader)
        self.assertEqual(client.conversations_info(channel="CROOM")["channel"]["id"], "CROOM")
        self.provider.reset_mock()
        with self.assertRaises(access.MemberAccessDenied):
            client.conversations_history(channel="CROOM")
        self.provider.assert_not_called()

    def test_reader_failure_excludes_only_failed_source_and_redacts_details(self):
        self.join(self.grant)
        self.join(self.other)
        self.channel.reader_grant = self.grant
        self.channel.save(update_fields=("reader_grant",))
        access.record_reader_failure(
            self.grant, SlackApiError("body must not persist", {"error": "untrusted_private_value"}),
            channel_id="CROOM", authority=dm._capture_slack_grant_api_authority(self.grant, refresh_token=False),
        )
        self.grant.refresh_from_db()
        self.channel.refresh_from_db()
        self.assertEqual(self.grant.last_error, "member_access:request_failed")
        self.assertIsNone(self.channel.reader_grant_id)
        self.assertEqual(access.reader_for_channel("TTEST", "CROOM").pk, self.other.pk)
        self.assertIsNotNone(access.reader_for_channel("TTEST", "CROOM", purpose="info"))

    def test_stale_provider_error_cannot_revoke_or_change_reconsented_authority(self):
        self.join(self.grant)
        original_handler = access.record_reader_failure
        for error_code in ("token_revoked", "not_in_channel"):
            with self.subTest(error=error_code):
                self.grant.refresh_from_db()
                self.channel.reader_grant = self.grant
                self.channel.save(update_fields=("reader_grant",))
                client = access.member_client(self.grant)
                self.provider.side_effect = SlackApiError("synthetic", {"error": error_code})

                def reconsent_before_handling(*args, **kwargs):
                    # The failing provider transaction has released its fence.
                    # A new login/consent wins before old-error bookkeeping.
                    self.grant.consented_at = timezone.now()
                    self.grant.last_error = "new-authority"
                    self.grant.save(update_fields=("consented_at", "last_error"))
                    self.grant.connection.access_token = f"synthetic-renewed-{error_code}"
                    self.grant.connection.save(update_fields=("access_token",))
                    original_handler(*args, **kwargs)

                with patch.object(access, "record_reader_failure", side_effect=reconsent_before_handling), patch.object(
                    dm, "revoke_user_grant", side_effect=AssertionError("Stale error must not revoke"),
                ), patch.object(access.cache, "set") as cache_write:
                    with self.assertRaises(SlackApiError):
                        client.conversations_info(channel="CROOM")
                    cache_write.assert_not_called()
                self.grant.refresh_from_db()
                self.grant.connection.refresh_from_db()
                self.channel.refresh_from_db()
                self.assertEqual(self.grant.status, "active")
                self.assertIsNone(self.grant.revoked_at)
                self.assertEqual(self.grant.last_error, "new-authority")
                self.assertEqual(self.grant.connection.status, "connected")
                self.assertEqual(self.grant.connection.access_token, f"synthetic-renewed-{error_code}")
                self.assertEqual(self.channel.reader_grant_id, self.grant.pk)

    def test_current_provider_auth_error_runs_existing_full_privacy_revoke(self):
        self.join(self.grant)
        self.provider.side_effect = SlackApiError("synthetic", {"error": "token_revoked"})
        with patch.object(dm, "_revoke_remote_token"), patch.object(dm, "_finish_grant_registration_revoke"):
            with self.assertRaises(SlackApiError):
                access.member_client(self.grant).conversations_info(channel="CROOM")
        self.grant.refresh_from_db()
        self.grant.connection.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.grant.status, "revoked")
        self.assertIsNotNone(self.grant.revoked_at)
        self.assertEqual(self.grant.connection.status, "disconnected")
        self.assertFalse(self.grant.connection.access_token)
        self.assertIsNotNone(self.link.revoked_at)

    def test_private_membership_rows_never_authorize_public_client_methods(self):
        for channel_id in ("DROOM", "GROOM"):
            self.join(self.grant, slack_channel_id=channel_id)
            self.assertIsNone(access.reader_for_channel("TTEST", channel_id))
            self.assertIsNone(access.poster_for_author(self.device.public_key, "TTEST", channel_id))
            with self.assertRaises(access.MemberAccessDenied):
                access.member_client(self.grant).conversations_history(channel=channel_id)
        self.provider.assert_not_called()

    def test_known_joined_poster_uses_own_account_link_and_no_probe(self):
        self.join(self.grant)
        result = self.poster()
        self.assertEqual(result.pk, self.grant.pk)
        self.assertEqual(result._member_posting_public_key, self.device.public_key)
        self.provider.assert_not_called()
        access.member_client(result).chat_postMessage(channel="CROOM", text="synthetic")
        self.assertEqual(self.provider.call_args.args[1], "chat_postMessage")

    def test_unknown_membership_is_probed_once_and_cached_for_ten_minutes(self):
        self.assertEqual(self.poster().pk, self.grant.pk)
        self.assertEqual(self.poster().pk, self.grant.pk)
        self.assertEqual(self.provider.call_count, 1)
        membership = self.grant.public_channel_memberships.get(slack_channel_id="CROOM")
        self.assertEqual((membership.state, membership.source), ("joined", "probe"))
        membership.state, membership.source = "left", "event"
        membership.observed_at = timezone.now()
        membership.save()
        self.assertIsNone(self.poster())
        self.assertEqual(self.provider.call_count, 1)

    def test_negative_probe_and_known_leave_cannot_post(self):
        self.provider.return_value = self.info(member=False)
        self.assertIsNone(self.poster())
        self.assertIsNone(self.poster())
        self.assertEqual(self.provider.call_count, 1)
        row = self.grant.public_channel_memberships.get(slack_channel_id="CROOM")
        self.assertEqual(row.state, "left")
        self.assertIsNotNone(row.left_at)

    def test_expired_probe_and_oauth_epoch_changes_require_a_new_probe(self):
        self.assertIsNotNone(self.poster())
        self.grant.public_channel_memberships.update(observed_at=timezone.now() - timedelta(minutes=11))
        self.assertIsNotNone(self.poster())
        self.assertEqual(self.provider.call_count, 2)
        metadata = dict(self.grant.connection.provider_metadata)
        metadata["mlai_slack_oauth_generation"] = 1
        self.grant.connection.provider_metadata = metadata
        self.grant.connection.save(update_fields=("provider_metadata",))
        self.provider.return_value = self.info(member=False)
        self.assertIsNone(self.poster())
        self.assertEqual(self.provider.call_count, 3)

    def test_old_consent_membership_does_not_authorize_history(self):
        self.join(self.grant, observed_at=self.grant.consented_at - timedelta(seconds=1))
        self.assertIsNone(access.reader_for_channel("TTEST", "CROOM"))

    def test_missing_scope_inactive_device_or_missing_link_never_probes(self):
        self.grant.connection.scopes = ["channels:read", "channels:history"]
        self.grant.connection.save(update_fields=("scopes",))
        self.assertIsNone(self.poster())
        self.grant.connection.scopes = sorted(PUBLIC_CHANNEL_SCOPES)
        self.grant.connection.save(update_fields=("scopes",))
        self.device.status = "revoked"
        self.device.save(update_fields=("status",))
        self.assertIsNone(self.poster())
        self.device.status = "verified"
        self.device.save(update_fields=("status",))
        self.link.revoked_at = timezone.now()
        self.link.save(update_fields=("revoked_at",))
        self.assertIsNone(self.poster())
        self.provider.assert_not_called()

    def test_revocation_during_probe_cannot_publish_a_positive_membership(self):
        def revoke(*args, **kwargs):
            SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(status="revoked", revoked_at=timezone.now())
            return self.info()
        self.provider.side_effect = revoke
        self.assertIsNone(self.poster())
        self.assertFalse(SlackChannelMembership.objects.filter(grant=self.grant).exists())

    def test_device_revocation_during_probe_cannot_publish_a_positive_membership(self):
        def revoke(*args, **kwargs):
            CommunityChatDevice.objects.filter(pk=self.device.pk).update(status="revoked", revoked_at=timezone.now())
            return self.info()
        self.provider.side_effect = revoke
        self.assertIsNone(self.poster())
        self.assertFalse(SlackChannelMembership.objects.filter(grant=self.grant).exists())

    def test_selected_poster_rechecks_device_and_membership_before_actual_write(self):
        row = self.join(self.grant)
        selected = self.poster()
        row.state = "left"
        row.save(update_fields=("state",))
        with self.assertRaises(access.MemberAccessDenied):
            access.member_client(selected).chat_postMessage(channel="CROOM", text="synthetic")
        row.state = "joined"
        row.save(update_fields=("state",))
        self.device.status = "revoked"
        self.device.save(update_fields=("status",))
        with self.assertRaises(access.MemberAccessDenied):
            access.member_client(selected).chat_postMessage(channel="CROOM", text="synthetic")
        self.provider.assert_not_called()

    def test_private_or_shared_probe_never_creates_membership(self):
        for flags in ({"is_private": True}, {"is_ext_shared": True}, {"id": "CWRONG"}):
            self.provider.return_value = self.info(**flags)
            self.assertIsNone(self.poster())
            self.assertFalse(SlackChannelMembership.objects.filter(grant=self.grant).exists())


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PostingIdentityLockTests(MemberAccessFixture, TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_link_only_revocation_waits_for_the_inflight_member_write(self):
        self.join(self.grant)
        selected = self.poster()
        provider_entered, release_provider, revoke_started = (threading.Event() for _ in range(3))
        errors, revoke_pid = [], []

        def provider(*args, **kwargs):
            provider_entered.set()
            if not release_provider.wait(10):
                raise AssertionError("Timed out waiting for the test write")
            return {"ok": True}

        def write():
            close_old_connections()
            try:
                access.member_client(selected).chat_postMessage(channel="CROOM", text="synthetic")
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        def revoke_link():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    revoke_pid.append(cursor.fetchone()[0])
                revoke_started.set()
                # Match the existing management command's link-only writer.
                CommunityBridgeIdentityLink.objects.filter(pk=self.link.pk).update(revoked_at=timezone.now())
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        self.provider.side_effect = provider
        writer = threading.Thread(target=write)
        revoker = threading.Thread(target=revoke_link)
        writer.start()
        try:
            self.assertTrue(provider_entered.wait(10))
            revoker.start()
            self.assertTrue(revoke_started.wait(10))
            deadline, blocked = time.monotonic() + 5, False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [revoke_pid[0]])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                time.sleep(0.02)
            self.assertTrue(blocked, "Link revocation must wait for the posting identity row fence")
        finally:
            release_provider.set()
            writer.join(10)
            if revoker.ident is not None:
                revoker.join(10)
        self.assertFalse(writer.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(errors, [])
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.revoked_at)
        self.provider.reset_mock()
        self.assertIsNone(self.poster())
        with self.assertRaises(access.MemberAccessDenied):
            access.member_client(selected).chat_postMessage(channel="CROOM", text="synthetic")
        self.provider.assert_not_called()


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True,
    SLACK_OWNER_INVENTORY_ENABLED=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PublicInventoryConsentDatabaseTests(MemberAccessFixture, TestCase):

    def collect(self):
        self.provider.return_value = {
            "ok": True, "channels": [self.info()["channel"]], "response_metadata": {},
        }
        authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        inventory.collect_public_page(authority)

    def test_enabled_member_lane_uses_existing_consent_without_metadata_opt_in(self):
        self.collect()
        self.assertTrue(SlackOwnerConversationInventory.objects.filter(grant=self.grant, kind="public_channel").exists())
        self.assertFalse(SlackChannelMembership.objects.filter(grant=self.grant).exists())
        self.assertEqual(self.provider.call_args.args[1], "users_conversations")
        self.assertEqual(self.provider.call_args.kwargs["required_scopes"], {"channels:read"})

    @override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False)
    def test_flag_off_keeps_existing_metadata_consent_requirement(self):
        self.grant.connection.scopes = [*PUBLIC_CHANNEL_SCOPES, "im:read"]
        self.grant.connection.save(update_fields=("scopes",))
        self.collect()
        self.provider.assert_not_called()
        self.assertFalse(SlackOwnerConversationInventory.objects.filter(grant=self.grant).exists())

    @override_settings(SLACK_MEMBER_CHANNELS_ENABLED=False)
    def test_flag_off_existing_metadata_opt_in_retains_public_inventory(self):
        self.grant.connection.scopes = ["im:read", "channels:read"]
        self.grant.connection.save(update_fields=("scopes",))
        inventory.grant_metadata_consent(self.grant)
        self.collect()
        self.assertTrue(SlackOwnerConversationInventory.objects.filter(grant=self.grant, kind="public_channel").exists())
