from datetime import timedelta
from io import StringIO
from unittest.mock import patch
import uuid

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    EmailCodeDeliveryStatus,
)
from community_chat.email_codes import _locked_email_code_challenges, code_digest


ORIGIN = "https://chat.mlai.au"


def public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
    COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET="test-delivery-secret",
    COMMUNITY_CHAT_EMAIL_CODE_MAX_ATTEMPTS=5,
    COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS=0,
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER="test-code-pepper",
    COMMUNITY_CHAT_EMAIL_CODE_RESEND_SECONDS=60,
    COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS=600,
    CUSTOMERIO_API_KEY="customerio-test-key",
    CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID="chat-code-template",
)
class CommunityChatEmailCodeAuthTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="member@example.com",
            first_name="MLAI",
            last_name="Member",
        )
        self.installation_id = uuid.uuid4()
        self.public_key = public_key(41)

    def request_code(self, email=None, **device_overrides):
        device = {
            "installation_id": str(self.installation_id),
            "public_key": self.public_key,
            "platform": "web",
            "name": "Chrome",
        }
        device.update(device_overrides)
        return self.client.post(
            reverse("community_chat_email_code_request"),
            {
                "email": email or self.user.email,
                "client_id": "mlai-chat-web",
                "device": device,
            },
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

    def verify(self, challenge_id, code):
        return self.client.post(
            reverse("community_chat_email_code_verify"),
            {
                "challenge_id": challenge_id,
                "code": code,
                "client_id": "mlai-chat-web",
                "installation_id": str(self.installation_id),
            },
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

    @patch("community_chat.email_delivery.send_community_chat_email_code")
    def deliver_code(self, mock_send):
        mock_send.return_value = {"delivery_id": "cio-test"}
        call_command("run_email_code_worker", "--once", stdout=StringIO())
        mock_send.assert_called_once()
        return mock_send.call_args.args[1]

    def test_request_contract_is_generic_for_existing_and_missing_users(self):
        existing = self.request_code()
        cache.clear()
        missing = self.request_code(email="missing@example.com")

        self.assertEqual(existing.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(missing.status_code, status.HTTP_202_ACCEPTED)
        for field in (
            "status",
            "message",
            "expires_in",
            "resend_after",
        ):
            self.assertEqual(existing.data[field], missing.data[field])
        self.assertIn("expires_at", existing.data)
        self.assertIn("resend_available_at", existing.data)
        self.assertLess(
            existing.data["resend_available_at"],
            existing.data["expires_at"],
        )
        self.assertNotEqual(existing.data["challenge_id"], missing.data["challenge_id"])
        self.assertEqual(CommunityChatEmailCodeChallenge.objects.count(), 2)
        self.assertEqual(CommunityChatEmailCodeDelivery.objects.count(), 1)
        missing_row = CommunityChatEmailCodeChallenge.objects.get(
            id=missing.data["challenge_id"]
        )
        self.assertIsNone(missing_row.user_id)

    def test_valid_code_verifies_email_and_mints_one_use_bootstrap(self):
        requested = self.request_code()
        code = self.deliver_code()

        verified = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(verified.status_code, status.HTTP_200_OK)
        self.assertEqual(verified.data["status"], "authenticated")
        self.assertTrue(verified.data["bootstrap_token"].startswith("mlai_chat_"))
        self.assertEqual(verified.data["profile"]["email"], self.user.email)
        self.assertIn("session", verified.data)
        self.assertNotIn("access_token", verified.data["session"])
        self.assertIn("mlai_chat_access", verified.cookies)
        self.assertIn("mlai_chat_refresh", verified.cookies)
        self.assertEqual(CommunityChatAccountSession.objects.count(), 1)
        token = CommunityChatBootstrapToken.objects.get()
        self.assertEqual(token.user, self.user)
        self.assertEqual(token.public_key, self.public_key)
        self.assertEqual(token.installation_id, self.installation_id)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        replay = self.verify(requested.data["challenge_id"], code)
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(replay.data, {"error": "invalid_or_expired_code"})

    def test_verification_locks_only_the_challenge_row(self):
        queryset = _locked_email_code_challenges()

        self.assertTrue(queryset.query.select_for_update)
        self.assertEqual(queryset.query.select_for_update_of, ("self",))

    def test_fifth_incorrect_code_invalidates_challenge(self):
        requested = self.request_code()
        challenge = CommunityChatEmailCodeChallenge.objects.get(
            id=requested.data["challenge_id"]
        )
        challenge.code_digest = code_digest(challenge.id, "123456")
        challenge.save(update_fields=("code_digest",))
        for _ in range(5):
            response = self.verify(requested.data["challenge_id"], "000000")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        challenge.refresh_from_db()
        self.assertEqual(challenge.attempt_count, 5)
        self.assertIsNotNone(challenge.invalidated_at)

    def test_expired_code_is_rejected(self):
        requested = self.request_code()
        code = self.deliver_code()
        CommunityChatEmailCodeChallenge.objects.filter(
            id=requested.data["challenge_id"]
        ).update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"error": "invalid_or_expired_code"})

    def test_resend_invalidates_prior_challenge_and_cancels_delivery(self):
        first = self.request_code()
        first_delivery = CommunityChatEmailCodeDelivery.objects.get()
        cache.clear()
        second = self.request_code()

        self.assertNotEqual(first.data["challenge_id"], second.data["challenge_id"])
        prior = CommunityChatEmailCodeChallenge.objects.get(id=first.data["challenge_id"])
        self.assertIsNotNone(prior.invalidated_at)
        first_delivery.refresh_from_db()
        self.assertEqual(first_delivery.status, EmailCodeDeliveryStatus.CANCELLED)
        self.assertEqual(first_delivery.encrypted_code, "")

    def test_inactive_user_receives_uniform_non_deliverable_challenge(self):
        self.user.is_active = False
        self.user.save(update_fields=("is_active",))

        response = self.request_code()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIsNone(CommunityChatEmailCodeChallenge.objects.get().user_id)
        self.assertFalse(CommunityChatEmailCodeDelivery.objects.exists())

    def test_request_has_per_email_installation_cooldown(self):
        self.assertEqual(self.request_code().status_code, status.HTTP_202_ACCEPTED)
        blocked = self.request_code()
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_platform_and_registered_client_must_match(self):
        response = self.request_code(platform="ios")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
