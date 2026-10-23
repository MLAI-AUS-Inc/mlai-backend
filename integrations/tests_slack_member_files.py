"""Current member authority governs file metadata, downloads and cached bytes."""

from copy import deepcopy
from queue import Queue
import threading
from time import monotonic, sleep
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.utils import timezone

from community_chat import slack_file_previews as files
from community_chat.models import CommunityChatDevice
from community_chat.tests.test_link_previews import _FakeResponse
from integrations.models import SlackDmMirrorConversation, SlackChannelMembership
from integrations.services import slack_file_authority as authority, slack_dm_mirror as dm
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.tests_message_sync_member_history_phase4b import Fixture, SETTINGS


@override_settings(**SETTINGS)
class MemberFileTests(Fixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        for grant in (self.a, self.b):
            grant.connection.scopes += ["files:read", "im:read"]
            grant.connection.save()
        self.file = {"id": "F123", "team_id": "TTEST", "title": "test.png", "mimetype": "image/png",
            "channels": ["CROOM"], "url_private": "https://files.slack.com/files-pri/T/F123/test.png"}
        self.raw = {"id": "CROOM", "is_channel": True, "is_private": False, "is_member": True}
        download = patch.object(files.requests.Session, "get", return_value=_FakeResponse(body=b"synthetic-image", content_type="image/png"))
        self.download = download.start()
        self.addCleanup(download.stop)

    def transport(self, selected, conn, method, **kwargs):
        self.calls.append((selected.grant_id, method, kwargs))
        if method == "files_info":
            return {"ok": True, "file": deepcopy(self.file)}
        if method == "conversations_info":
            return {"ok": True, "channel": deepcopy(self.raw)}
        raise AssertionError(method)

    def image(self, user=None):
        return files.fetch_slack_file_image("F123", user=user or self.a.user)

    def test_members_file_reads_with_owner_even_on_image_cache_hit(self):
        self.assertEqual(self.image(), ("image/png", b"synthetic-image"))
        self.image()
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual([method for _, method, _ in self.calls], ["files_info", "conversations_info"] * 2)
        self.assertEqual({grant for grant, _, _ in self.calls}, {self.a.pk})
        self.assertEqual(self.download.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token-1")
        self.bot.assert_not_called()

    def test_missing_files_scope_or_revoked_consent_blocks_cached_image(self):
        self.image()
        self.a.connection.scopes.remove("files:read")
        self.a.connection.save()
        self.calls.clear()
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.download.call_count, 1)

    def test_revocation_after_capture_prevents_metadata_cache_and_provider(self):
        self.image()
        captured = dm._capture_slack_grant_api_authority(self.a, refresh_token=False)
        type(self.a).objects.filter(pk=self.a.pk).update(status="revoked", revoked_at=timezone.now())
        self.calls.clear()
        with patch.object(authority, "_candidates", return_value=[captured]), self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.download.call_count, 1)

    def test_members_file_cannot_borrow_other_members_authority(self):
        self.image()
        SlackChannelMembership.objects.filter(grant=self.a).update(state="left")
        self.calls.clear()
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual({grant for grant, _, _ in self.calls}, {self.a.pk})
        self.assertEqual(self.download.call_count, 1)

    def test_revoked_device_blocks_members_cached_image(self):
        self.image()
        CommunityChatDevice.objects.filter(user=self.a.user).update(status="revoked", revoked_at=timezone.now())
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.download.call_count, 1)

    def test_unlinked_community_account_uses_current_joined_member(self):
        self.channel.audience = "community"
        self.channel.save()
        type(self.a).objects.filter(pk=self.a.pk).update(status="paused")
        self.image()
        self.assertEqual({grant for grant, _, _ in self.calls}, {self.b.pk})
        self.assertEqual(self.download.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token-2")
        SlackChannelMembership.objects.update(state="left")
        self.calls.clear()
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.calls, [])

    def test_unshared_or_wrong_workspace_file_cannot_reuse_image_cache(self):
        self.image()
        for changes in ({"channels": []}, {"team_id": "TOTHER"}, {"id": "FOTHER"}):
            original = deepcopy(self.file)
            self.file.update(changes)
            with self.subTest(changes=changes), self.assertRaises(files.SlackFilePreviewError):
                self.image()
            self.file = original
        self.assertEqual(self.download.call_count, 1)

    def test_private_archived_or_connect_source_cannot_reuse_cache(self):
        self.image()
        for key in ("is_private", "is_archived", "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared"):
            original = deepcopy(self.raw)
            self.raw[key] = True
            with self.subTest(key=key), self.assertRaises(files.SlackFilePreviewError):
                self.image()
            self.raw = original
        self.assertEqual(self.download.call_count, 1)

    def test_documented_file_without_top_level_team_uses_current_shared_source(self):
        self.file.pop("team_id")
        self.file["shares"] = {"public": {"CROOM": [{"team_id": "TTEST", "ts": "1700000000.000001"}]}}
        self.image()
        self.file["shares"]["public"]["CROOM"][0]["team_id"] = "TOTHER"
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.download.call_count, 1)

    def test_remote_file_and_external_redirect_never_receive_member_token(self):
        self.file["is_external"] = True
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.download.assert_not_called()
        self.file.pop("is_external")
        for destination in ("https://outside.invalid/file", "http://files.slack.com/file", "https://files.slack.com.outside.invalid/file"):
            response = _FakeResponse(body=b"", content_type="image/png")
            response.is_redirect = True
            response.headers["Location"] = destination
            self.download.return_value = response
            with self.subTest(destination=destination), self.assertRaises(files.SlackFilePreviewError):
                self.image()
            self.assertEqual(self.download.call_count, 1)
            self.assertFalse(self.download.call_args.kwargs["allow_redirects"])
            from django.core.cache import cache
            cache.clear()
            self.download.reset_mock()

    def test_same_file_host_redirect_is_checked_and_downloaded(self):
        response = _FakeResponse(body=b"", content_type="image/png")
        response.is_redirect = True
        response.headers["Location"] = "/new-file.png"
        self.download.side_effect = [response, _FakeResponse(body=b"redirected", content_type="image/png")]
        self.assertEqual(self.image(), ("image/png", b"redirected"))
        self.assertEqual(self.download.call_args.args[0], "https://files.slack.com/new-file.png")

    def test_rejected_token_only_revokes_that_exact_reader_then_tries_another(self):
        from slack_sdk.errors import SlackApiError
        self.channel.audience = "community"
        self.channel.save()
        original = self.transport
        def read(selected, conn, method, **kwargs):
            if selected.grant_id == self.a.pk:
                raise SlackApiError("synthetic", response={"error": "invalid_auth"})
            return original(selected, conn, method, **kwargs)
        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=read), patch.object(dm, "_revoke_remote_token"):
            self.image()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.status, "revoked")
        self.assertEqual(self.b.status, "active")
        self.assertEqual(self.download.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token-2")

    def test_credential_rotation_and_reconsent_get_new_image_cache_scope(self):
        self.channel.audience = "community"
        self.channel.save()
        self.image()
        self.a.connection.access_token = "synthetic-new-token"
        self.a.connection.save()
        self.image()
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(self.download.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-new-token")
        self.a.consented_at = timezone.now()
        self.a.save()
        SlackChannelMembership.objects.filter(grant=self.a).update(observed_at=timezone.now())
        self.image()
        self.assertEqual(self.download.call_count, 3)

    def test_budget_refusal_does_not_try_another_token(self):
        self.channel.audience = "community"
        self.channel.save()
        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=BudgetDeferred(7)) as transport:
            with self.assertRaises(files.SlackFilePreviewDeferred) as caught:
                self.image()
            self.assertEqual(caught.exception.retry_after, 7)
            self.assertEqual(transport.call_count, 1)
        self.download.assert_not_called()

    def test_private_file_uses_only_current_owner_and_live_conversation(self):
        self.file.pop("channels")
        self.file["ims"] = ["DPRIVATE"]
        self.raw = {"id": "DPRIVATE", "is_im": True, "is_private": True}
        conversation = SlackDmMirrorConversation.objects.create(grant=self.a, slack_workspace_id="TTEST",
            slack_conversation_id="DPRIVATE", status="live")
        self.image()
        with self.assertRaises(files.SlackFilePreviewError):
            self.image(self.b.user)
        conversation.status = "paused"
        conversation.save()
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.download.call_count, 1)

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_revocation_waits_until_download_finishes_then_blocks_cache_hit(self):
        entered, release, changed = (threading.Event() for _ in range(3))
        errors, pids = [], Queue()
        def download(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise AssertionError("Download was not released")
            return _FakeResponse(body=b"synthetic-image", content_type="image/png")
        self.download.side_effect = download
        def read():
            close_old_connections()
            try:
                self.image()
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
        with self.assertRaises(files.SlackFilePreviewError):
            self.image()
        self.assertEqual(self.download.call_count, 1)
