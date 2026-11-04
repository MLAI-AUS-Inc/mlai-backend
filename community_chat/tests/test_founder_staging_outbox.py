"""Real PostgreSQL namespace boundaries and local HTTP delivery, not live E2E."""
from datetime import timedelta
import importlib
import threading
from unittest.mock import patch
import uuid

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
import requests

from community_chat import email_delivery, founder_staging_email as provider
from community_chat.models import CommunityChatEmailCodeChallenge as Challenge, CommunityChatEmailCodeDelivery as Delivery, EmailCodeDeliveryStatus as Status
from community_chat.tests.founder_staging_fixtures import DOMAIN, SETTINGS, WRITER

inbox_module = importlib.import_module("ops.founder-staging.inbox_fixture")


@override_settings(**SETTINGS, COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="synthetic-fixture-delivery-secret")
class FounderStagingOutboxTests(TestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.email = "founder-acceptance-" + str(uuid.uuid4()) + "@" + DOMAIN

    def create(self, *, origin=provider.FRONTEND, client=provider.CLIENT, expired=False):
        challenge = Challenge.objects.create(email_digest=uuid.uuid4().hex * 2, code_digest=uuid.uuid4().hex * 2,
            client_id=client, installation_id=uuid.uuid4(), origin=origin, platform="web", public_key="a" * 64,
            expires_at=timezone.now() + timedelta(seconds=-1 if expired else 600),
            encrypted_signup_email=email_delivery.encrypt_signup_email(self.email), onboarding_version=1)
        return challenge, Delivery.objects.create(challenge=challenge, encrypted_code=email_delivery.encrypt_email_code("004200"))

    def rows(self, challenge, delivery):
        return Challenge.objects.values().get(pk=challenge.pk), Delivery.objects.values().get(pk=delivery.pk)

    def test_lease_and_expiry_cleanup_preserve_existing_origin_and_native_client(self):
        old, old_delivery = self.create(origin="https://chat-staging.mlai.au", expired=True)
        native, native_delivery = self.create(client="mlai-chat-ios", expired=True)
        old_before, native_before = self.rows(old, old_delivery), self.rows(native, native_delivery)
        own, own_delivery = self.create()
        self.assertEqual(email_delivery.claim_email_code_delivery(), own_delivery.id)
        self.assertIsNone(email_delivery.claim_email_code_delivery())
        self.assertEqual(self.rows(old, old_delivery), old_before)
        self.assertEqual(self.rows(native, native_delivery), native_before)
        own_delivery.refresh_from_db()
        self.assertEqual((own_delivery.status, own_delivery.attempts), (Status.SENDING, 1))

    def test_expired_fixture_cancels_only_its_own_outbox_row(self):
        old, old_delivery = self.create(origin="https://chat-staging.mlai.au", expired=True)
        before = self.rows(old, old_delivery)
        own, own_delivery = self.create(expired=True)
        with patch("community_chat.email_delivery.APIClient") as legacy, patch.object(provider.requests, "Session") as sender:
            self.assertEqual(email_delivery.process_email_code_deliveries(),
                {"sent": 0, "retry": 0, "failed": 0, "cancelled": 1, "not_scoped": 0})
        self.assertEqual(self.rows(old, old_delivery), before)
        own.refresh_from_db(); own_delivery.refresh_from_db()
        self.assertEqual(own.encrypted_signup_email, "")
        self.assertEqual((own_delivery.status, own_delivery.encrypted_code), (Status.CANCELLED, ""))
        legacy.assert_not_called(); sender.assert_not_called()

    def test_direct_foreign_delivery_is_refused_without_changes_or_provider_calls(self):
        old, old_delivery = self.create(origin="https://chat-staging.mlai.au")
        before = self.rows(old, old_delivery)
        with patch("community_chat.email_delivery.APIClient") as legacy, patch.object(provider.requests, "Session") as sender:
            self.assertEqual(email_delivery.deliver_email_code(old_delivery.id), "not_scoped")
        self.assertEqual(self.rows(old, old_delivery), before)
        legacy.assert_not_called(); sender.assert_not_called()

    def test_invalid_fixture_configuration_refuses_before_database_access(self):
        self.create(origin="https://chat-staging.mlai.au", expired=True)
        with override_settings(SETTINGS_MODULE=provider.PROFILE, FOUNDER_STAGING_INBOX_WRITE_TOKEN=""), \
                self.assertNumQueries(0), self.assertRaises(ImproperlyConfigured):
            email_delivery.claim_email_code_delivery()

    def test_provider_failure_keeps_original_retry_and_encrypted_code(self):
        own, delivery = self.create()
        with patch.object(provider.requests, "Session", side_effect=RuntimeError("synthetic provider failure")), \
                patch("community_chat.email_delivery.APIClient") as legacy:
            self.assertEqual(email_delivery.process_email_code_deliveries(limit=1)["retry"], 1)
        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.attempts, delivery.last_error_code), (Status.PENDING, 1, "RuntimeError"))
        self.assertTrue(delivery.encrypted_code)
        legacy.assert_not_called()

    def test_actual_leased_outbox_delivers_over_http_with_immutable_source_identity(self):
        own, delivery = self.create()
        inbox = inbox_module.Inbox(DOMAIN)
        server = inbox_module.InboxServer(("127.0.0.1", 0), inbox_module.handler(inbox, "R" * 48, WRITER))
        server.inbox = inbox
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        original_session, observed = requests.Session, []

        class LocalProviderSession(original_session):
            # Test-only DNS/port translation after the fixed provider guard;
            # real HTTP handler, payload, scoped auth and acceptance are used.
            def post(self, url, **kwargs):
                if url != provider.PROVIDER:
                    raise AssertionError("Unexpected provider destination")
                observed.append(kwargs["json"])
                return super().post("http://127.0.0.1:" + str(server.server_port) + "/deliver", **kwargs)

        try:
            with patch.object(provider.requests, "Session", LocalProviderSession), patch("community_chat.email_delivery.APIClient") as legacy:
                self.assertEqual(email_delivery.process_email_code_deliveries(limit=1),
                    {"sent": 1, "retry": 0, "failed": 0, "cancelled": 0, "not_scoped": 0})
            delivery.refresh_from_db()
            self.assertEqual((delivery.status, delivery.encrypted_code, delivery.provider_delivery_id), (Status.SENT, "", str(delivery.id)))
            self.assertEqual(observed[0]["deliveryId"], str(delivery.id))
            self.assertEqual(observed[0]["issuedAt"], own.created_at.isoformat())
            self.assertEqual(observed[0]["expiresAt"], own.expires_at.isoformat())
            self.assertEqual(inbox.read(self.email, own.created_at.isoformat())["code"], "004200")
            legacy.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
