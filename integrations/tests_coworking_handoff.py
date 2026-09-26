import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch, Mock, AsyncMock

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import TestCase, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import AiConsentRecord, CommunityChatDevice, DeviceBindingStatus
from community_chat.privacy import set_ai_consent
from community_chat.tests.privacy_fixtures import PROVIDERS, grant_test_ai_consent
from community_chat.tests.test_account_profiles import credentials_for
from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod, CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus, CommunityBridgeMessageLink,
)
from integrations.services.community_bridge import coworking

CONFIG = dict(
    ROO_SERVICE_URL="https://roo.example.test", ROO_INTERNAL_MENTION_API_KEY="synthetic-mention-key",
    COMMUNITY_CHAT_ROO_PUBLIC_KEY="", COMMUNITY_CHAT_AI_PROVIDERS=PROVIDERS,
    COMMUNITY_CHAT_AI_DISCLOSURE_VERSION="test-v1",
)
DELIVERY = {
    "id": 1, "created_at": int(datetime(2026, 9, 8, 15, tzinfo=dt_timezone.utc).timestamp()),
    "source_platform": "buzz", "delivery_type": "create", "source_message_id": "ab" * 32,
    "source_channel_id": coworking.COWORKING_CHANNEL_ID, "target_channel_id": "CCOWORK",
    "channel": {"slack_workspace_id": "TMLAI", "destination_channel_id": coworking.COWORKING_CHANNEL_ID},
    "payload": {"text": coworking.BOOK_TODAY_MESSAGE, "source_author_id": "cd" * 32},
}


@override_settings(**CONFIG)
class CoworkingAvailabilityTests(TestCase):
    def test_availability_requires_this_members_verified_link_and_enabled_channel(self):
        member = get_user_model().objects.create_user(email="booking@example.test")
        other = get_user_model().objects.create_user(email="other@example.test")
        grant_test_ai_consent(member)
        channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI", slack_channel_id="CCOWORK",
            destination_platform="buzz", destination_channel_id=coworking.COWORKING_CHANNEL_ID,
        )
        link = CommunityBridgeIdentityLink.objects.create(
            user=member, slack_workspace_id="TMLAI", slack_user_id="UMEMBER",
            buzz_pubkey="cd" * 32, verified_at=timezone.now(),
            display_name="Test member",
            verification_method=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
            verification_reference="synthetic-booking-test",
        )
        self.assertTrue(coworking.coworking_booking_available(member))
        self.assertFalse(coworking.coworking_booking_available(other))
        channel.enabled = False
        channel.save()
        self.assertFalse(coworking.coworking_booking_available(member))
        channel.enabled = True
        channel.save()
        link.revoked_at = timezone.now()
        link.save()
        self.assertFalse(coworking.coworking_booking_available(member))


@override_settings(**CONFIG)
class CoworkingHandoffTests(SimpleTestCase):
    def test_bridge_worker_routes_the_signed_command_to_roo_without_a_second_slack_post(self):
        from integrations.services.community_bridge import worker

        client = worker.CommunityBridgeDiscordClient()
        with patch.object(client, "_resolve_author_display_name", new=AsyncMock(return_value="Test member")), \
             patch.object(client, "_resolve_parent_destination_message", new=AsyncMock(return_value="")), \
             patch.object(worker, "deliver_coworking_request", new=AsyncMock()) as deliver, \
             patch.object(worker.SlackBridgeClient, "post_message") as post:
            asyncio.run(client._deliver_to_slack(DELIVERY))
            self.assertEqual(deliver.await_args.args[0], DELIVERY)
            self.assertIn(coworking.BOOK_TODAY_MESSAGE, deliver.await_args.args[1])
            self.assertEqual(deliver.await_args.args[2], "")
            post.assert_not_called()
        asyncio.run(client.close())

    def test_only_the_explicit_command_in_the_exact_mapped_channel_is_routed(self):
        self.assertTrue(coworking.is_coworking_request(DELIVERY))
        for key, value in [("source_platform", "slack"), ("source_channel_id", "other"), ("delivery_type", "edit")]:
            self.assertFalse(coworking.is_coworking_request({**DELIVERY, key: value}))
        self.assertFalse(coworking.is_coworking_request({**DELIVERY, "payload": {"text": "Book someone else"}}))
        self.assertFalse(coworking.is_coworking_request({**DELIVERY, "source_parent_message_id": "parent"}))
        with override_settings(COMMUNITY_CHAT_ROO_PUBLIC_KEY="invalid"):
            self.assertTrue(coworking.is_coworking_request(DELIVERY))
        with override_settings(COMMUNITY_CHAT_ROO_PUBLIC_KEY="aa" * 32):
            self.assertFalse(coworking.is_coworking_request(DELIVERY))

    @patch.object(coworking, "_consented_identity", side_effect=lambda delivery: nullcontext({"slack_user_id": "UMEMBER"}))
    def test_identity_is_resolved_from_signed_sender_and_date_is_frozen(self, identity):
        payload = coworking.prepare_coworking_request(DELIVERY)
        self.assertEqual(payload["user_id"], "UMEMBER")
        self.assertIn("2026-09-09", payload["text"])
        self.assertEqual(payload, coworking.prepare_coworking_request(deepcopy(DELIVERY)))
        identity.assert_called_with(DELIVERY)

    @patch.object(coworking, "verified_identity_for_buzz", return_value=None)
    def test_unlinked_sender_cannot_book(self, _identity):
        with self.assertRaises(coworking.CoworkingHandoffError):
            coworking.prepare_coworking_request(DELIVERY)

    @patch.object(coworking.requests, "post")
    @patch.object(coworking, "_consented_identity", side_effect=lambda delivery: nullcontext({"slack_user_id": "UMEMBER"}))
    def test_dispatch_uses_service_bearer_and_requires_reply_ack(self, _identity, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"reply_delivered": True}
        coworking.dispatch_coworking_request(DELIVERY, "123.456")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["json"]["thread_ts"], "123.456")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer synthetic-mention-key"})
        post.return_value.json.return_value = {"message": "old Roo version"}
        with self.assertRaises(coworking.CoworkingHandoffError):
            coworking.dispatch_coworking_request(DELIVERY, "123.456")

    def test_retry_reuses_checkpointed_slack_root_and_same_roo_request(self):
        from integrations.services.community_bridge import store
        link = None
        def checkpoint(**kwargs):
            nonlocal link
            self.assertFalse(kwargs["mark_completed"])
            link = {"destination_message_id": kwargs["destination_message_id"]}
        with patch.object(coworking, "prepare_coworking_request", return_value={"request_id": "same"}), \
             patch.object(store, "resolve_message_link", side_effect=lambda **kw: link), \
             patch.object(store, "complete_create_delivery", side_effect=checkpoint), \
             patch.object(store, "complete_delivery") as complete, \
             patch.object(coworking, "_post_coworking_root", return_value={"message_id": "123.456"}) as post, \
             patch.object(coworking, "dispatch_coworking_request", side_effect=[coworking.CoworkingHandoffError("temporary"), None]) as dispatch:
            with self.assertRaises(coworking.CoworkingHandoffError):
                asyncio.run(coworking.deliver_coworking_request(DELIVERY, "Booking", ""))
            complete.assert_not_called()
            asyncio.run(coworking.deliver_coworking_request(DELIVERY, "Booking", ""))
            self.assertEqual(post.call_count, 1)
            self.assertEqual(dispatch.call_args_list[0], dispatch.call_args_list[1])
            complete.assert_called_once_with(delivery_id=1, wake_waiting_children=True)


@override_settings(**CONFIG)
class CoworkingConsentTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(email="consent@example.test")
        self.device = CommunityChatDevice.objects.create(
            user=self.member, public_key=DELIVERY["payload"]["source_author_id"],
            status=DeviceBindingStatus.VERIFIED, verified_at=timezone.now(),
        )
        self.link = CommunityBridgeIdentityLink.objects.create(
            user=self.member, slack_workspace_id="TMLAI", slack_user_id="UMEMBER",
            buzz_pubkey=self.device.public_key, verified_at=timezone.now(),
            display_name="Synthetic consent member",
            verification_method=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
            verification_reference="synthetic-consent-test",
        )

    def assert_blocked(self):
        from integrations.services.community_bridge.slack import SlackBridgeClient

        with patch.object(coworking.requests, "post") as roo_post, \
             patch.object(SlackBridgeClient, "post_message") as slack_post:
            for send in (
                lambda: coworking.prepare_coworking_request(DELIVERY),
                lambda: coworking._post_coworking_root(DELIVERY, "Booking", ""),
                lambda: coworking.dispatch_coworking_request(DELIVERY, "123.456"),
            ):
                with self.assertRaises(coworking.CoworkingHandoffError):
                    send()
            roo_post.assert_not_called()
            slack_post.assert_not_called()

    def test_no_consent_blocks_both_external_sends(self):
        self.assert_blocked()
        self.assertFalse(coworking.coworking_booking_available(self.member))

    def test_withdrawal_after_preparation_blocks_both_external_sends(self):
        grant_test_ai_consent(self.member)
        self.assertEqual(coworking.prepare_coworking_request(DELIVERY)["user_id"], "UMEMBER")
        AiConsentRecord.objects.filter(user=self.member).update(withdrawn_at=timezone.now())
        self.assert_blocked()

    def test_changed_or_missing_disclosure_blocks_queued_work(self):
        grant_test_ai_consent(self.member)
        for config in (
            {"COMMUNITY_CHAT_AI_DISCLOSURE_VERSION": "test-v2"},
            {"COMMUNITY_CHAT_AI_PROVIDERS": []},
            {"COMMUNITY_CHAT_AI_PROVIDERS": [{**PROVIDERS[0], "name": "New provider"}]},
        ):
            with self.subTest(config=config), override_settings(**config):
                self.assert_blocked()

    def test_revoked_device_or_identity_and_inactive_account_cannot_send(self):
        grant_test_ai_consent(self.member)
        for model, fields, restore in (
            (self.device, {"revoked_at": timezone.now()}, {"revoked_at": None}),
            (self.link, {"revoked_at": timezone.now()}, {"revoked_at": None}),
            (self.member, {"is_active": False}, {"is_active": True}),
        ):
            with self.subTest(model=type(model).__name__):
                type(model).objects.filter(pk=model.pk).update(**fields)
                self.assert_blocked()
                type(model).objects.filter(pk=model.pk).update(**restore)

    def test_legacy_key_without_account_cannot_borrow_another_accounts_consent(self):
        grant_test_ai_consent(self.member)
        self.device.delete()
        self.link.user = None
        self.link.save(update_fields=["user"])
        self.assert_blocked()

    def test_consented_sender_is_resolved_from_signed_device_and_can_send(self):
        from integrations.services.community_bridge.slack import SlackBridgeClient

        grant_test_ai_consent(self.member)
        # Client-supplied identity never replaces the verified device's owner.
        delivery = deepcopy(DELIVERY)
        delivery["payload"]["user_id"] = "UOTHER"
        with patch.object(coworking.requests, "post", return_value=Mock(
            status_code=200, json=Mock(return_value={"reply_delivered": True}),
        )) as roo_post, patch.object(SlackBridgeClient, "post_message") as slack_post:
            coworking._post_coworking_root(delivery, "Booking", "")
            coworking.dispatch_coworking_request(delivery, "123.456")
            slack_post.assert_called_once()
            self.assertEqual(roo_post.call_args.kwargs["json"]["user_id"], "UMEMBER")

    def test_withdrawal_between_slack_root_and_roo_dispatch_blocks_roo(self):
        from integrations.services.community_bridge.slack import SlackBridgeClient

        grant_test_ai_consent(self.member)
        with patch.object(SlackBridgeClient, "post_message", return_value={"message_id": "123.456"}):
            coworking._post_coworking_root(DELIVERY, "Booking", "")
        AiConsentRecord.objects.filter(user=self.member).update(withdrawn_at=timezone.now())
        with patch.object(coworking.requests, "post") as post:
            with self.assertRaises(coworking.CoworkingHandoffError):
                coworking.dispatch_coworking_request(DELIVERY, "123.456")
            post.assert_not_called()


@override_settings(**CONFIG)
class CoworkingConsentConcurrencyTests(TransactionTestCase):
    def test_withdrawal_waits_for_each_in_flight_send_then_blocks_retries(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locking required")
        CoworkingConsentTests.setUp(self)
        session = credentials_for(self.member).session
        from integrations.services.community_bridge.slack import SlackBridgeClient

        for target, send in (
            (coworking.requests, lambda: coworking.dispatch_coworking_request(DELIVERY, "123.456")),
            (SlackBridgeClient, lambda: coworking._post_coworking_root(DELIVERY, "Booking", "")),
        ):
            with self.subTest(target=target.__name__):
                grant_test_ai_consent(self.member)
                entered, release = Event(), Event()
                withdrawal_pid = Queue()

                def in_flight(*args, **kwargs):
                    entered.set()
                    if not release.wait(timeout=10):
                        raise AssertionError("Test did not release synthetic I/O")
                    return Mock(status_code=200, json=Mock(return_value={"reply_delivered": True}))

                def run_send():
                    close_old_connections()
                    try:
                        send()
                    finally:
                        connection.close()

                def withdraw():
                    close_old_connections()
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT pg_backend_pid()")
                            withdrawal_pid.put(cursor.fetchone()[0])
                        set_ai_consent(authenticated_session=session, granted=False,
                                       version="", provider_digest="")
                    finally:
                        connection.close()

                method = "post" if target is coworking.requests else "post_message"
                with patch.object(target, method, side_effect=in_flight) as post, ThreadPoolExecutor(max_workers=2) as pool:
                    sending = pool.submit(run_send)
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        withdrawing = pool.submit(withdraw)
                        pid = withdrawal_pid.get(timeout=5)
                        blocked = False
                        deadline = monotonic() + 5
                        while monotonic() < deadline:
                            with connection.cursor() as cursor:
                                cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                                blocked = cursor.fetchone()[0]
                            if blocked:
                                break
                            sleep(0.01)
                        self.assertTrue(blocked, "Withdrawal must wait for the account lock during I/O")
                    finally:
                        release.set()
                    sending.result(timeout=5)
                    withdrawing.result(timeout=5)
                    with self.assertRaises(coworking.CoworkingHandoffError):
                        send()
                    self.assertEqual(post.call_count, 1)


class CoworkingCheckpointTests(TestCase):
    def test_checkpoint_preserves_status_then_completion_wakes_replies_without_resurrecting_link(self):
        from integrations.services.community_bridge.store import complete_create_delivery, complete_delivery

        channel = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TMLAI", slack_channel_id="CCOWORK",
            destination_platform="buzz", destination_channel_id=coworking.COWORKING_CHANNEL_ID,
        )
        common = dict(channel=channel, target_platform="slack", source_platform="buzz",
                      delivery_type="create", source_channel_id=coworking.COWORKING_CHANNEL_ID,
                      target_channel_id="CCOWORK", available_at=timezone.now())
        parent = CommunityBridgeDelivery.objects.create(
            **common, source_event_key="parent", source_message_id="aa" * 32,
            status=CommunityBridgeDeliveryStatus.PROCESSING,
        )
        child = CommunityBridgeDelivery.objects.create(
            **common, source_event_key="child", source_message_id="bb" * 32,
            source_parent_message_id=parent.source_message_id,
            status=CommunityBridgeDeliveryStatus.WAITING_FOR_PARENT,
        )
        complete_create_delivery(delivery_id=parent.id, destination_message_id="123.456",
                                 destination_channel_id="CCOWORK", mark_completed=False)
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(parent.status, CommunityBridgeDeliveryStatus.PROCESSING)
        self.assertEqual(child.status, CommunityBridgeDeliveryStatus.WAITING_FOR_PARENT)
        link = CommunityBridgeMessageLink.objects.get(source_message_id=parent.source_message_id)
        link.destination_deleted_at = timezone.now()
        link.save()
        complete_delivery(delivery_id=parent.id, wake_waiting_children=True)
        parent.refresh_from_db()
        child.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(parent.status, CommunityBridgeDeliveryStatus.COMPLETED)
        self.assertEqual(child.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertIsNotNone(link.destination_deleted_at)
