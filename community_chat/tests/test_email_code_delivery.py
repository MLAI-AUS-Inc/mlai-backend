from datetime import timedelta
from io import StringIO
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.email_delivery import encrypt_email_code
from community_chat.models import (
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    EmailCodeDeliveryStatus,
)


@override_settings(
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="test-delivery-secret",
    COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS=600,
    CUSTOMERIO_API_KEY="customerio-test-key",
    CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID="chat-code-template",
)
class CommunityChatEmailCodeDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="member@example.com",
            first_name="MLAI",
            last_name="Member",
        )

    def create_delivery(self, *, expires_at=None):
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Chrome",
            public_key="c" * 64,
            expires_at=expires_at or timezone.now() + timedelta(minutes=10),
        )
        return CommunityChatEmailCodeDelivery.objects.create(
            challenge=challenge,
            encrypted_code=encrypt_email_code("042817"),
        )

    @patch("community_chat.email_delivery.APIClient")
    def test_worker_sends_customerio_template_and_clears_code(self, api_client):
        delivery = self.create_delivery()
        api_client.return_value.send_email.return_value = {"delivery_id": "cio-123"}

        call_command("run_email_code_worker", "--once", stdout=StringIO())

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailCodeDeliveryStatus.SENT)
        self.assertEqual(delivery.encrypted_code, "")
        self.assertEqual(delivery.provider_delivery_id, "cio-123")
        payload = api_client.return_value.send_email.call_args.args[0]
        self.assertEqual(payload["transactional_message_id"], "chat-code-template")
        self.assertEqual(payload["message_data"]["verification_code"], "042817")
        self.assertEqual(payload["message_data"]["expires_minutes"], 10)
        self.assertEqual(payload["to"], self.user.email)
        self.assertNotEqual(encrypt_email_code("042817"), "042817")

    @patch("community_chat.email_delivery.APIClient")
    def test_expired_challenge_is_cancelled_without_sending(self, api_client):
        delivery = self.create_delivery(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        call_command("run_email_code_worker", "--once", stdout=StringIO())

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailCodeDeliveryStatus.CANCELLED)
        self.assertEqual(delivery.encrypted_code, "")
        api_client.assert_not_called()

    @patch("community_chat.email_delivery.APIClient")
    def test_provider_failure_retries_without_persisting_error_text(self, api_client):
        delivery = self.create_delivery()
        api_client.return_value.send_email.side_effect = RuntimeError(
            "provider unavailable code=042817"
        )

        call_command("run_email_code_worker", "--once", stdout=StringIO())

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailCodeDeliveryStatus.PENDING)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_error_code, "RuntimeError")
        self.assertNotIn("042817", delivery.last_error_code)
        self.assertTrue(delivery.encrypted_code)

    @override_settings(CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID="")
    def test_missing_provider_configuration_retries(self):
        delivery = self.create_delivery()

        call_command("run_email_code_worker", "--once", stdout=StringIO())

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailCodeDeliveryStatus.PENDING)
        self.assertEqual(delivery.last_error_code, "RuntimeError")
