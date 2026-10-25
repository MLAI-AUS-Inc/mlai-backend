"""Member directory reads and cached metadata retain real consent boundaries."""

from unittest.mock import patch
import threading
from queue import Queue
from time import monotonic, sleep

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction

from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone

from integrations.models import SlackChannelMembership
from integrations.services import slack_directory_authority as authority
from integrations.services import slack_dm_mirror as mirror
from integrations.services import slack_mention_directory as directory
from integrations.services import slack_workspace_users as snapshots
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS


@override_settings(**SETTINGS)
class MemberDirectoryTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        for grant in (self.a, self.b):
            grant.connection.scopes += ["users:read"]
            grant.connection.save()
        # Existing audience acknowledgements remain valid: users:read changes
        # directory permission, not the set of verified device keys.
        approved = patch.object(directory, "require_community_access")
        approved.start()
        self.addCleanup(approved.stop)

    def transport(self, selected, connection, method, **kwargs):
        self.calls.append((selected.grant_id, method, kwargs))
        if method == "users_list":
            return {"ok": True, "members": [{"id": "UOTHER", "team_id": "TTEST", "name": "Person"}]}
        if method == "conversations_info":
            return {"ok": True, "channel": {"id": "CROOM", "is_private": False,
                    "is_channel": True, "is_member": True}}
        if method == "conversations_members":
            return {"ok": True, "members": ["UOTHER"], "response_metadata": {"next_cursor": ""}}
        raise AssertionError("Unexpected directory provider operation")

    def search(self, user=None):
        return directory.search_workspace_mentions(user or self.a.user,
            channel_id=self.channel.destination_channel_id, query="person")

    def test_warm_reads_with_member_and_complete_cache_survives_bot_token_change(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.assertEqual([(g, m) for g, m, _ in self.calls], [(self.a.pk, "users_list")])
        with override_settings(SLACK_BRIDGE_BOT_TOKEN="different-synthetic-bot"):
            self.assertEqual(snapshots.cached_workspace_users("TTEST")[0]["slack_user_id"], "UOTHER")
        self.bot.assert_not_called()

    def test_no_reader_never_returns_previously_cached_directory(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        type(self.a).objects.update(status="paused")
        self.calls.clear()
        self.assertIsNone(snapshots.cached_workspace_users("TTEST"))
        self.assertEqual(snapshots.warm_workspace_directory_once(), 0)
        self.assertEqual(self.calls, [])

    def test_new_credentials_or_consent_cannot_reuse_old_snapshot(self):
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.a.connection.access_token = "synthetic-rotated-token"
        self.a.connection.save()
        self.assertIsNone(snapshots.cached_workspace_users("TTEST"))
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.a.consented_at = timezone.now()
        self.a.save()
        self.assertIsNone(snapshots.cached_workspace_users("TTEST"))

    def test_revocation_after_selection_prevents_provider_and_cache_hit(self):
        selected = authority.capture_reader("TTEST")
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        type(self.a).objects.filter(pk=self.a.pk).update(status="paused")
        self.calls.clear()
        self.assertIsNone(snapshots.cached_workspace_snapshot("TTEST", authority=selected))
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            with authority.fence(selected) as (_, connection):
                authority.read(selected, connection, "users_list")
        self.assertEqual(self.calls, [])

    def test_directory_source_needs_no_chat_write_but_requires_users_read(self):
        for grant in (self.a, self.b):
            grant.connection.scopes.remove("chat:write")
            grant.connection.save()
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        for grant in (self.a, self.b):
            grant.connection.scopes.remove("users:read")
            grant.connection.save()
        self.assertEqual(snapshots.warm_workspace_directory_once(), 0)
        self.assertIsNone(snapshots.cached_workspace_users("TTEST"))

    def test_members_room_reads_only_owners_current_joined_authority(self):
        result = self.search()
        self.assertEqual(result["users"][0]["slack_user_id"], "UOTHER")
        self.assertTrue(result["users"][0]["is_member"])
        self.assertEqual({g for g, _, _ in self.calls}, {self.a.pk})
        SlackChannelMembership.objects.filter(grant=self.a).update(state="left")
        self.calls.clear()
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            self.search()
        self.assertEqual(self.calls, [])

    def test_members_room_device_revocation_blocks_cached_metadata(self):
        self.search()
        from community_chat.models import CommunityChatDevice
        CommunityChatDevice.objects.filter(user=self.a.user).update(status="revoked", revoked_at=timezone.now())
        self.calls.clear()
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            self.search()
        self.assertEqual(self.calls, [])

    def test_public_names_remain_available_without_requesters_slack_grant(self):
        self.channel.audience = "community"
        self.channel.save()
        type(self.a).objects.filter(pk=self.a.pk).update(status="paused")
        result = self.search()
        self.assertTrue(result["users"][0]["is_member"])
        self.assertEqual({g for g, _, _ in self.calls}, {self.b.pk})

    def test_no_joined_reader_does_not_claim_channel_membership(self):
        self.channel.audience = "community"
        self.channel.save()
        SlackChannelMembership.objects.update(state="left")
        result = self.search()
        self.assertIsNone(result["users"][0]["is_member"])
        self.assertFalse(result["users"][0]["native_only"])
        self.assertEqual({method for _, method, _ in self.calls}, {"users_list"})

    def test_source_closed_before_provider_response_cannot_reveal_members(self):
        self.channel.audience = "community"
        self.channel.save()
        self.search()  # Fill the membership cache before changing the source.
        for changes in ({"is_private": True}, {"is_ext_shared": True}, {"is_archived": True}):
            with patch.object(mirror, "_invoke_slack_with_locked_connection", return_value={
                "ok": True, "channel": {"id": "CROOM", "is_channel": True,
                    "is_private": False, "is_member": True, **changes},
            }) as provider:
                with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
                    self.search()
                self.assertEqual([call.args[2] for call in provider.call_args_list], ["conversations_info"])

    def test_failed_directory_reader_does_not_block_another_members_reader(self):
        with patch.object(mirror, "_invoke_slack_with_locked_connection", side_effect=mirror.SlackApiError(
            "synthetic", response={"ok": False, "error": "internal_error"})), self.assertRaises(mirror.SlackApiError):
            snapshots.warm_workspace_directory_once()
        self.calls.clear()
        self.assertEqual(snapshots.warm_workspace_directory_once(), 1)
        self.assertEqual({g for g, _, _ in self.calls}, {self.b.pk})

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_reader_revocation_waits_for_directory_provider_call(self):
        entered, release, changed = (threading.Event() for _ in range(3))
        errors, pids = [], Queue()
        original = self.transport

        def slow(selected, conn, method, **kwargs):
            entered.set()
            if not release.wait(8):
                raise AssertionError("Directory request was not released")
            return original(selected, conn, method, **kwargs)

        def read():
            close_old_connections()
            try:
                with patch.object(mirror, "_invoke_slack_with_locked_connection", side_effect=slow):
                    snapshots.warm_workspace_directory_once()
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        def revoke():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids.put(cursor.fetchone()[0])
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=self.a.user_id)
                    type(self.a).objects.filter(pk=self.a.pk).update(status="paused")
                changed.set()
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()

        reader, revoker = threading.Thread(target=read), threading.Thread(target=revoke)
        reader.start()
        try:
            self.assertTrue(entered.wait(8))
            revoker.start()
            pid, blocked, end = pids.get(timeout=5), False, monotonic() + 5
            while monotonic() < end:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                sleep(0.01)
            self.assertTrue(blocked)
            self.assertFalse(changed.is_set())
        finally:
            release.set()
            reader.join(10)
            if revoker.ident is not None:
                revoker.join(10)
        self.assertFalse(reader.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(changed.is_set())
        self.assertIsNone(snapshots.cached_workspace_users("TTEST"))
