"""Synthetic public Slack consent and worker dispatch regressions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import AiConsentRecord, CommunityChatDevice, DeviceBindingStatus
from community_chat.privacy import set_ai_consent
from community_chat.tests.privacy_fixtures import PROVIDERS, grant_test_ai_consent
from community_chat.tests.test_account_profiles import credentials_for
from integrations.models import CommunityBridgeIdentityLink, CommunityBridgeIdentityVerificationMethod
from integrations.services.community_bridge import ai_consent, worker

CONFIG = dict(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True,
              COMMUNITY_CHAT_AI_PROVIDERS=PROVIDERS,
              COMMUNITY_CHAT_AI_DISCLOSURE_VERSION="public-test-v1")
DELIVERY = {
    "id": 1, "source_platform": "buzz", "target_platform": "slack",
    "delivery_type": "create", "source_message_id": "ab" * 32,
    "source_channel_id": "synthetic-chat", "target_channel_id": "CPUBLIC",
    "channel": {"slack_workspace_id": "TTEST"},
    "payload": {"source_author_id": "cd" * 32, "text": "A normal untagged message",
                "attachments": [{"url": "https://example.com/synthetic.png"}]},
}


def create_sender(test):
    test.member = get_user_model().objects.create_user(email="public-consent@example.test")
    test.device = CommunityChatDevice.objects.create(
        user=test.member, public_key=DELIVERY["payload"]["source_author_id"],
        status=DeviceBindingStatus.VERIFIED, verified_at=timezone.now(),
    )
    test.link = CommunityBridgeIdentityLink.objects.create(
        user=test.member, slack_workspace_id="TTEST", slack_user_id="UMEMBER",
        display_name="Synthetic member",
        buzz_pubkey=test.device.public_key, verified_at=timezone.now(),
        verification_method=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
        verification_reference="synthetic-public-consent",
    )


@override_settings(**CONFIG)
class PublicBridgeConsentTests(TestCase):
    def setUp(self):
        create_sender(self)

    def assert_blocked(self, delivery=None):
        send = Mock()
        with self.assertRaises(ai_consent.PublicBridgeConsentRequired):
            ai_consent.send_with_ai_consent(delivery or DELIVERY, send, text="Synthetic")
        send.assert_not_called()

    def test_untagged_message_and_media_require_current_permission(self):
        self.assert_blocked()
        grant_test_ai_consent(self.member)
        send = Mock(return_value={"message_id": "123.456"})
        self.assertEqual(ai_consent.send_with_ai_consent(DELIVERY, send, text="Synthetic"),
                         {"message_id": "123.456"})
        send.assert_called_once_with(text="Synthetic")

    def test_withdrawal_blocks_queued_retry_thread_edit_and_reaction(self):
        grant_test_ai_consent(self.member)
        ai_consent.send_with_ai_consent(DELIVERY, Mock())
        AiConsentRecord.objects.filter(user=self.member).update(withdrawn_at=timezone.now())
        for operation in ("create", "edit", "reaction_add"):
            with self.subTest(operation=operation):
                self.assert_blocked({**DELIVERY, "delivery_type": operation,
                                     "source_parent_message_id": "parent"})

    def test_changed_or_missing_disclosure_rejects_previously_consented_work(self):
        grant_test_ai_consent(self.member)
        for config in (
            {"COMMUNITY_CHAT_AI_DISCLOSURE_VERSION": "new"},
            {"COMMUNITY_CHAT_AI_PROVIDERS": []},
            {"COMMUNITY_CHAT_AI_PROVIDERS": [{**PROVIDERS[0], "name": "Changed"}]},
        ):
            with self.subTest(config=config), override_settings(**config):
                self.assert_blocked()

    def test_revoked_device_link_and_inactive_account_are_rejected(self):
        grant_test_ai_consent(self.member)
        for obj, changed, restored in (
            (self.device, {"revoked_at": timezone.now()}, {"revoked_at": None}),
            (self.link, {"revoked_at": timezone.now()}, {"revoked_at": None}),
            (self.member, {"is_active": False}, {"is_active": True}),
        ):
            with self.subTest(model=type(obj).__name__):
                type(obj).objects.filter(pk=obj.pk).update(**changed)
                self.assert_blocked()
                type(obj).objects.filter(pk=obj.pk).update(**restored)

    def test_legacy_key_cannot_borrow_account_consent(self):
        grant_test_ai_consent(self.member)
        self.device.delete()
        self.link.user = None
        self.link.save(update_fields=["user"])
        self.assert_blocked()

    def test_identity_is_rechecked_under_the_account_lock(self):
        grant_test_ai_consent(self.member)
        current = ai_consent.verified_identity_for_buzz(
            slack_workspace_id="TTEST", buzz_pubkey=self.device.public_key,
        )
        with patch.object(ai_consent, "verified_identity_for_buzz", side_effect=[current, None]):
            self.assert_blocked()

    def test_payload_user_id_cannot_override_signed_device_owner(self):
        other = get_user_model().objects.create_user(email="other-public@example.test")
        grant_test_ai_consent(other)
        delivery = deepcopy(DELIVERY)
        delivery["payload"]["user_id"] = other.pk
        self.assert_blocked(delivery)

    def test_foreign_workspace_identity_is_not_used(self):
        grant_test_ai_consent(self.member)
        self.assert_blocked({**DELIVERY, "channel": {"slack_workspace_id": "TOTHER"}})


@override_settings(**CONFIG)
class PublicBridgeWorkerConsentTests(SimpleTestCase):
    def run_operation(self, operation, *, source="buzz", blocked=False):
        delivery = deepcopy(DELIVERY)
        delivery.update(delivery_type=operation, source_platform=source)
        if operation.startswith("reaction"):
            delivery["payload"]["text"] = "👍"
        client = worker.CommunityBridgeDiscordClient()
        with ExitStack() as stack:
            stack.enter_context(patch.object(client, "_resolve_author_display_name", new=AsyncMock(return_value="Synthetic")))
            stack.enter_context(patch.object(client, "_resolve_parent_destination_message", new=AsyncMock(return_value="123.456")))
            stack.enter_context(patch.object(worker, "resolve_message_link", return_value={
                "destination_channel_id": "CPUBLIC", "destination_message_id": "123.456",
                "destination_payload": {"channel_id": "CPUBLIC", "message_id": "123.456", "reaction": "+1"},
            }))
            for name in ("complete_create_delivery", "complete_delivery", "mark_link_deleted"):
                stack.enter_context(patch.object(worker, name))
            identity = stack.enter_context(patch.object(ai_consent, "verified_identity_for_buzz", return_value=None))
            sends = {name: stack.enter_context(patch.object(worker.SlackBridgeClient, name, return_value={
                "channel": "CPUBLIC", "message_id": "123.456",
            })) for name in ("post_message", "update_message", "add_reaction", "delete_message", "remove_reaction")}
            try:
                if blocked:
                    with self.assertRaises(ai_consent.PublicBridgeConsentRequired):
                        asyncio.run(client._deliver_to_slack(delivery))
                    for send in sends.values():
                        send.assert_not_called()
                else:
                    asyncio.run(client._deliver_to_slack(delivery))
                    method = {"create": "post_message", "delete": "delete_message", "reaction_remove": "remove_reaction"}[operation]
                    sends[method].assert_called_once()
                    identity.assert_not_called()
            finally:
                asyncio.run(client.close())

    def test_each_content_write_uses_real_consent_boundary(self):
        for operation in ("create", "edit", "reaction_add"):
            with self.subTest(operation=operation):
                self.run_operation(operation, blocked=True)

    def test_cleanup_remains_possible_without_consent(self):
        for operation in ("delete", "reaction_remove"):
            with self.subTest(operation=operation):
                self.run_operation(operation)

    def test_discord_origin_does_not_require_a_chat_account(self):
        self.run_operation("create", source="discord")


@override_settings(**CONFIG)
class PublicBridgeConsentConcurrencyTests(TransactionTestCase):
    def test_withdrawal_waits_for_send_and_then_blocks_retry(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locking required")
        create_sender(self)
        grant_test_ai_consent(self.member)
        session = credentials_for(self.member).session
        entered, release = Event(), Event()
        withdrawal_pid = Queue()

        def in_flight():
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("Test did not release synthetic I/O")

        send = Mock(side_effect=in_flight)

        def run_send():
            close_old_connections()
            try:
                ai_consent.send_with_ai_consent(DELIVERY, send)
            finally:
                connection.close()

        def withdraw():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    withdrawal_pid.put(cursor.fetchone()[0])
                set_ai_consent(authenticated_session=session, granted=False, version="", provider_digest="")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
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
                self.assertTrue(blocked, "Withdrawal must wait for I/O holding the account lock")
            finally:
                release.set()
            sending.result(timeout=5)
            withdrawing.result(timeout=5)
        with self.assertRaises(ai_consent.PublicBridgeConsentRequired):
            ai_consent.send_with_ai_consent(DELIVERY, send)
        send.assert_called_once()
