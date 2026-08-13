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

from community_chat.account_sessions import issue_account_session
from community_chat.adapter import MembershipAdapterUnavailable
from community_chat.email_codes import _locked_email_code_challenges, code_digest
from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    DeviceBindingStatus,
    EmailCodeDeliveryStatus,
)


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

    @patch("community_chat.views.revoke_relay_membership")
    def test_valid_code_replaces_existing_session_for_same_device(
        self,
        mock_revoke,
    ):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.public_key,
            installation_id=self.installation_id,
            client_id="mlai-chat-web",
            platform="web",
            name="Chrome",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        old_challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=self.installation_id,
            origin=ORIGIN,
            platform="web",
            device_name="Chrome",
            public_key=self.public_key,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        old_session = issue_account_session(self.user, old_challenge).session
        requested = self.request_code()
        code = self.deliver_code()

        verified = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(verified.status_code, status.HTTP_200_OK)
        mock_revoke.assert_not_called()
        device.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.VERIFIED)
        old_session.refresh_from_db()
        self.assertIsNotNone(old_session.revoked_at)
        active_session = CommunityChatAccountSession.objects.get(
            installation_id=self.installation_id,
            revoked_at__isnull=True,
        )
        self.assertNotEqual(active_session.id, old_session.id)
        self.assertEqual(active_session.public_key, self.public_key)

    @patch("community_chat.views.revoke_relay_membership")
    def test_valid_code_recovers_same_user_after_browser_key_rotation(
        self,
        mock_revoke,
    ):
        old_public_key = public_key(40)
        old_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=old_public_key,
            installation_id=self.installation_id,
            client_id="mlai-chat-web",
            platform="web",
            name="Chrome",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        old_challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=self.installation_id,
            origin=ORIGIN,
            platform="web",
            device_name="Chrome",
            public_key=old_public_key,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        old_session = issue_account_session(self.user, old_challenge).session
        old_bootstrap = CommunityChatBootstrapToken.objects.create(
            user=self.user,
            public_key=old_public_key,
            installation_id=self.installation_id,
            client_id="mlai-chat-web",
            origin=ORIGIN,
            platform="web",
            name="Chrome",
            token_hash="c" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        mock_revoke.return_value = ("revoked", uuid.uuid4())
        requested = self.request_code()
        code = self.deliver_code()

        verified = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(verified.status_code, status.HTTP_200_OK)
        self.assertEqual(verified.data["status"], "authenticated")
        mock_revoke.assert_called_once_with(old_public_key)
        old_device.refresh_from_db()
        self.assertEqual(old_device.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(
            old_device.revocation_reason,
            "email_code_identity_recovery",
        )
        self.assertEqual(old_device.revoked_by, self.user)
        replacement = CommunityChatDevice.objects.get(
            installation_id=self.installation_id,
            status=DeviceBindingStatus.PENDING,
        )
        self.assertEqual(replacement.user, self.user)
        self.assertEqual(replacement.public_key, self.public_key)
        old_session.refresh_from_db()
        self.assertIsNotNone(old_session.revoked_at)
        active_session = CommunityChatAccountSession.objects.get(
            installation_id=self.installation_id,
            revoked_at__isnull=True,
        )
        self.assertEqual(active_session.public_key, self.public_key)
        old_bootstrap.refresh_from_db()
        self.assertIsNotNone(old_bootstrap.revoked_at)
        active_bootstrap = CommunityChatBootstrapToken.objects.get(
            installation_id=self.installation_id,
            revoked_at__isnull=True,
        )
        self.assertEqual(active_bootstrap.public_key, self.public_key)
        enrollment = self.client.post(
            reverse("community_chat_challenge"),
            {"origin": ORIGIN, "public_key": self.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(enrollment.status_code, status.HTTP_201_CREATED)

    @patch("community_chat.views.revoke_relay_membership")
    def test_key_rotation_rolls_back_when_relay_revocation_is_unavailable(
        self,
        mock_revoke,
    ):
        old_public_key = public_key(40)
        old_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=old_public_key,
            installation_id=self.installation_id,
            client_id="mlai-chat-web",
            platform="web",
            name="Chrome",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        mock_revoke.side_effect = MembershipAdapterUnavailable("adapter_unavailable")
        requested = self.request_code()
        code = self.deliver_code()

        response = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data, {"error": "membership_service_unavailable"})
        old_device.refresh_from_db()
        self.assertEqual(old_device.status, DeviceBindingStatus.VERIFIED)
        challenge = CommunityChatEmailCodeChallenge.objects.get(
            id=requested.data["challenge_id"]
        )
        self.assertIsNone(challenge.consumed_at)
        self.assertEqual(CommunityChatDevice.objects.count(), 1)
        self.assertFalse(CommunityChatBootstrapToken.objects.exists())
        self.assertFalse(CommunityChatAccountSession.objects.exists())

    @patch("community_chat.views.revoke_relay_membership")
    def test_email_code_cannot_take_over_another_users_installation(
        self,
        mock_revoke,
    ):
        other_user = get_user_model().objects.create_user(email="other@example.com")
        other_device = CommunityChatDevice.objects.create(
            user=other_user,
            public_key=public_key(40),
            installation_id=self.installation_id,
            client_id="mlai-chat-web",
            platform="web",
            name="Chrome",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        requested = self.request_code()
        code = self.deliver_code()

        response = self.verify(requested.data["challenge_id"], code)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data, {"error": "installation_already_bound"})
        mock_revoke.assert_not_called()
        other_device.refresh_from_db()
        self.assertEqual(other_device.status, DeviceBindingStatus.VERIFIED)
        challenge = CommunityChatEmailCodeChallenge.objects.get(
            id=requested.data["challenge_id"]
        )
        self.assertIsNone(challenge.consumed_at)
        self.assertEqual(CommunityChatDevice.objects.count(), 1)
        self.assertFalse(CommunityChatBootstrapToken.objects.exists())
        self.assertFalse(CommunityChatAccountSession.objects.exists())

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
