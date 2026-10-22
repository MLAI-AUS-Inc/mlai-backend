"""All verified Members devices, exact audience acknowledgement and capacity."""
import threading
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature, tag
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import BridgeSyncJob, CommunityBridgeDelivery
from integrations.services import slack_dm_mirror as dm
from integrations.services import slack_member_devices as devices
from integrations.services.community_bridge import member_audiences as audiences
from integrations.services.community_bridge import member_delivery as delivery
from integrations.services.community_bridge.buzz import BuzzBridgeClient
from integrations.services.message_sync import member_history
from integrations.services.slack_chat_catalog import member_channel_room_id
from integrations.services.slack_history_pages import _public_progress
from integrations.services.slack_owner_inventory_api import InventoryError, _member_public_open_allowed
from integrations.tests_community_bridge_member_audiences_phase4a import _Adapter
from integrations.tests_member_delivery_phase4c import MemberDeliveryFixture
from integrations.tests_message_sync_member_history_phase4b import SETTINGS


@override_settings(**SETTINGS, COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False)
class MembersAllDevicesTests(MemberDeliveryFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = _Adapter(
            audiences.current_audience_keys("TTEST", "CROOM"),
            str(self.channel.audience_generation),
        )
        for method, implementation in (
            ("inspect_member_audience", self.adapter.inspect),
            ("update_member_audience", self.adapter.update),
        ):
            mock = patch.object(BuzzBridgeClient, method, side_effect=implementation)
            mock.start()
            self.addCleanup(mock.stop)

    def add_keys(self, count, *, start=4096, user=None):
        user = user or self.a.user
        keys = [f"{start + number:064x}" for number in range(count)]
        CommunityChatDevice.objects.bulk_create([
            CommunityChatDevice(
                user=user, public_key=key, status="verified", verified_at=timezone.now(),
            ) for key in keys
        ])
        return keys

    def sync(self, *, reduction_only=False):
        result = audiences.sync_audience(self.channel.pk, reduction_only=reduction_only)
        self.channel.refresh_from_db()
        return result

    def room(self, key):
        self.channel.refresh_from_db()
        return member_channel_room_id(
            self.a, key, self.channel, catalog_kinds="public_channel",
        )

    def test_all_nine_keys_wait_for_full_cas_then_catalog_history_and_send(self):
        ninth = self.add_keys(8)[-1]
        # Presence can request provisioning, but neither a ready room nor content
        # may use the prior two-key acknowledgement.
        self.assertTrue(_member_public_open_allowed(
            self.a, ninth, "CROOM", catalog_kinds="public_channel",
        ))
        self.assertIsNone(self.room(ninth))
        self.assertIsNone(self.room("1" * 64))
        with self.assertRaises(InventoryError):
            member_history.request_history(
                self.a.user, self.channel, ninth, "a" * 64, _public_progress,
            )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sync(), "applied")
        self.assertEqual(len(self.adapter.keys), 10)  # Nine A devices plus B.
        self.assertIn(ninth, self.adapter.keys)
        self.assertEqual(self.room(ninth), self.channel.destination_channel_id)
        result = member_history.request_history(
            self.a.user, self.channel, ninth, "a" * 64, _public_progress,
        )
        self.assertTrue(result["pending"])
        row = self.delivery(author=ninth)
        self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.payload[delivery.MEMBER_STAMP]["actor"]["public_key"], ninth)
        self.bot.assert_not_called()

    def test_ninth_slack_import_and_history_need_current_full_audience(self):
        self.add_keys(8)
        self.history_messages = [{
            "ts": f"{self.now - 30}.000001", "user": "UOTHER",
            "text": "synthetic all-device history",
        }]
        self.run_page()
        self.assertEqual(self.calls, [])
        self.assertFalse(CommunityBridgeDelivery.objects.exists())
        self.assertEqual(self.sync(), "applied")
        BridgeSyncJob.objects.update(due_at=timezone.now())
        self.run_page()
        self.assertEqual([call[1] for call in self.calls], [
            "conversations_info", "conversations_history", "conversations_info",
        ])
        row = CommunityBridgeDelivery.objects.get()
        with patch.object(BuzzBridgeClient, "deliver", return_value={
            "channel_id": self.channel.destination_channel_id,
            "message_id": "d" * 64, "parent_message_id": "",
        }) as adapter:
            self.send(row)
        self.assertEqual(row.status, "completed")
        self.assertEqual(adapter.call_args.kwargs["member_scope"]["expected_audience_hash"], self.channel.audience_hash)

    def test_revoked_device_cannot_use_prior_ack_and_reduction_removes_it(self):
        ninth = self.add_keys(8)[-1]
        self.assertEqual(self.sync(), "applied")
        self.assertIsNotNone(self.room(ninth))
        CommunityChatDevice.objects.filter(public_key=ninth).update(revoked_at=timezone.now())
        self.assertIsNone(self.room(ninth))
        self.assertIsNone(self.room("1" * 64))
        self.assertFalse(_member_public_open_allowed(
            self.a, ninth, "CROOM", catalog_kinds="public_channel",
        ))
        row = self.delivery(author=ninth)
        with self.assertRaises(delivery.MemberDeliveryUnavailable):
            self.send(row)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sync(reduction_only=True), "applied")
        self.assertNotIn(ninth, self.adapter.keys)
        self.assertFalse(self.channel.enabled)  # Reductions never reopen.
        self.assertEqual(self.sync(), "applied")
        self.assertIsNotNone(self.room("1" * 64))
        self.assertIsNone(self.room(ninth))

    def test_one_thousand_complete_keys_then_overflow_hold_and_recovery(self):
        CommunityChatDevice.objects.filter(user=self.b.user).update(revoked_at=timezone.now())
        self.add_keys(999)
        self.assertEqual(len(audiences.current_audience_keys("TTEST", "CROOM")), 1000)
        self.assertEqual(self.sync(), "applied")
        self.assertEqual(len(self.adapter.keys), 1000)
        self.assertIsNotNone(self.room("1" * 64))
        extra = self.add_keys(1, start=9000)[0]
        self.assertEqual(devices.device_key_result({self.a.user_id}), (None, "audience_too_large"))
        self.assertIsNone(audiences.current_audience_keys("TTEST", "CROOM"))
        self.assertIsNone(self.room("1" * 64))
        self.assertFalse(_member_public_open_allowed(
            self.a, "1" * 64, "CROOM", catalog_kinds="public_channel",
        ))
        with self.assertRaises(InventoryError):
            member_history.request_history(
                self.a.user, self.channel, "1" * 64, "a" * 64, _public_progress,
            )
        row = self.delivery()
        with self.assertRaises(delivery.MemberDeliveryDeferred):
            self.send(row)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sync(), "held")
        self.assertEqual(self.adapter.keys, set())
        self.assertEqual(self.channel.hold_reason, "audience_too_large")
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertFalse(self.channel.enabled)
        CommunityChatDevice.objects.filter(public_key=extra).update(revoked_at=timezone.now())
        self.assertEqual(self.sync(), "applied")
        self.assertEqual(len(self.adapter.keys), 1000)
        self.assertEqual(self.channel.hold_reason, "")
        self.assertIsNotNone(self.room("1" * 64))

    def test_selector_proves_overflow_with_bounded_distinct_query(self):
        self.add_keys(1000)
        with CaptureQueriesContext(connection) as queries:
            result = devices.device_key_result({self.a.user_id})
        self.assertEqual(result, (None, "audience_too_large"))
        self.assertEqual(len(queries), 1)
        self.assertIn("DISTINCT", queries[0]["sql"])
        self.assertIn("LIMIT 1001", queries[0]["sql"])

    def test_pending_revoked_and_inactive_devices_do_not_grant_keys(self):
        pending, revoked = self.add_keys(2)
        CommunityChatDevice.objects.filter(public_key=pending).update(status="pending")
        CommunityChatDevice.objects.filter(public_key=revoked).update(revoked_at=timezone.now())
        self.b.user.is_active = False
        self.b.user.save(update_fields=("is_active",))
        self.assertEqual(devices.device_key_result({self.a.user_id, self.b.user_id}), ({"1" * 64}, ""))
        for key in (pending, revoked):
            self.assertFalse(devices.member_device_key_allowed(self.a.user_id, key))
        self.assertFalse(devices.member_device_key_allowed(self.b.user_id, "2" * 64))

    def test_invalid_key_refuses_complete_proof_instead_of_dropping_bad_device(self):
        self.add_keys(1)
        CommunityChatDevice.objects.filter(user=self.a.user).exclude(public_key="1" * 64).update(public_key="not-hex")
        self.assertEqual(devices.device_key_result({self.a.user_id}), (None, "audience_authority_unavailable"))
        self.assertIsNone(self.room("1" * 64))

    def test_private_eight_device_helper_remains_unchanged(self):
        self.add_keys(11)
        self.assertEqual(len(dm._owner_device_pubkeys(self.a.user_id, limit=1000)), 8)
        self.assertEqual(len(devices.device_key_result({self.a.user_id})[0]), 12)

    def test_existing_armed_command_never_rebases_to_expanded_audience(self):
        row = self.delivery()
        with patch.object(delivery, "_finish_slack", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.send(row)
        row.refresh_from_db()
        original_command = row.payload[delivery.MEMBER_STAMP]
        self.add_keys(8)
        self.assertEqual(self.sync(), "applied")
        self.retry(row)
        self.calls.clear()
        self.mutations.clear()
        with self.assertRaises(delivery.MemberDeliveryUnavailable):
            self.send(row)
        row.refresh_from_db()
        self.assertEqual(row.payload[delivery.MEMBER_STAMP], original_command)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.mutations, [])

    def background(self, operation):
        result, errors = [], []
        def run():
            close_old_connections()
            try:
                result.append(operation())
            except BaseException as error:
                errors.append(error)
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, result, errors

    def finish(self, handle):
        thread, result, errors = handle
        thread.join(10)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        return result

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_new_device_verification_waits_for_cas_then_old_ack_denies(self):
        self.add_keys(8)
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block(channel, command):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Synthetic CAS timeout")
        self.adapter.before_update = block
        writer = self.background(self.sync)
        self.assertTrue(entered.wait(5))
        pids = []
        def verify():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids.append(cursor.fetchone()[0])
            ready.set()
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.a.user_id)
                return self.add_keys(1, start=9000)[0]
        verifier = self.background(verify)
        self.assertTrue(ready.wait(5))
        self.assert_blocked(pids[0])
        release.set()
        self.assertEqual(self.finish(writer), ["applied"])
        new_key = self.finish(verifier)[0]
        self.assertIsNone(self.room(new_key))
        self.assertIsNone(self.room("1" * 64))
        self.adapter.before_update = None
        self.assertEqual(self.sync(), "applied")
        self.assertIsNotNone(self.room(new_key))

    def assert_blocked(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.02)
        self.fail("Concurrent authority writer did not wait for the fence")

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_direct_ninth_device_revocation_waits_through_current_send(self):
        ninth = self.add_keys(8)[-1]
        self.assertEqual(self.sync(), "applied")
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.transport
        def block(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[2] == "chat_postMessage":
                entered.set()
                if not release.wait(8):
                    raise RuntimeError("Synthetic send timeout")
            return response
        row = self.delivery(author=ninth)
        with patch.object(dm, "_invoke_slack_with_locked_connection", side_effect=block):
            writer = self.background(lambda: self.send(row))
            self.assertTrue(entered.wait(5))
            pids = []
            def revoke():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids.append(cursor.fetchone()[0])
                ready.set()
                return CommunityChatDevice.objects.filter(public_key=ninth).update(revoked_at=timezone.now())
            revoker = self.background(revoke)
            self.assertTrue(ready.wait(5))
            self.assert_blocked(pids[0])
            release.set()
            self.finish(writer)
            self.assertEqual(self.finish(revoker), [1])
        self.assertIsNone(self.room(ninth))
        self.assertEqual(self.sync(reduction_only=True), "applied")
        self.assertNotIn(ninth, self.adapter.keys)
