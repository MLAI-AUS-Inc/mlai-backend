from datetime import timedelta
import hashlib
import uuid
from unittest.mock import patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.adapter import RelayMembership
from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatEmailCodeChallenge,
    DeviceBindingStatus,
)


ORIGIN = "https://chat.mlai.au"


def public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
)
class CommunityChatAccountSessionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="session@example.com",
            first_name="Session",
            last_name="Member",
        )
        self.challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-ios",
            installation_id=uuid.uuid4(),
            origin="mlaichat://callback",
            platform="ios",
            device_name="iPhone",
            public_key=public_key(41),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.credentials = issue_account_session(self.user, self.challenge)

    def bearer_client(self, token=None):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token or self.credentials.access_token}"
        )
        return client

    def web_session(self, private_int=51):
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="d" * 64,
            code_digest="e" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin=ORIGIN,
            platform="web",
            device_name="Chrome",
            public_key=public_key(private_int),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(self.user, challenge)
        client = APIClient()
        client.cookies["mlai_chat_access"] = credentials.access_token
        return challenge, credentials, client

    def test_tokens_are_hashed_and_account_endpoint_returns_own_profile(self):
        session = self.credentials.session
        self.assertEqual(
            session.access_token_hash,
            hashlib.sha256(self.credentials.access_token.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            session.refresh_token_hash,
            hashlib.sha256(self.credentials.refresh_token.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(session.access_token_hash, self.credentials.access_token)

        response = self.bearer_client().get(reverse("community_chat_account"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["profile"]["email"], self.user.email)
        self.assertEqual(
            response.data["session"]["installation_id"],
            str(self.challenge.installation_id),
        )

    def test_refresh_rotates_both_tokens_and_invalidates_old_access(self):
        response = APIClient().post(
            reverse("community_chat_session_refresh"),
            {"refresh_token": self.credentials.refresh_token},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_access = response.data["session"]["access_token"]
        new_refresh = response.data["session"]["refresh_token"]
        self.assertNotEqual(new_access, self.credentials.access_token)
        self.assertNotEqual(new_refresh, self.credentials.refresh_token)
        self.assertEqual(
            self.bearer_client(self.credentials.access_token)
            .get(reverse("community_chat_account"))
            .status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.bearer_client(new_access)
            .get(reverse("community_chat_account"))
            .status_code,
            status.HTTP_200_OK,
        )
        replay = APIClient().post(
            reverse("community_chat_session_refresh"),
            {"refresh_token": self.credentials.refresh_token},
            format="json",
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_revokes_session(self):
        response = APIClient().post(
            reverse("community_chat_session_logout"),
            {"refresh_token": self.credentials.refresh_token},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.credentials.session.refresh_from_db()
        self.assertIsNotNone(self.credentials.session.revoked_at)
        denied = self.bearer_client().get(reverse("community_chat_account"))
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("community_chat.views.revoke_relay_membership")
    def test_account_session_can_revoke_only_its_bound_device(self, mock_revoke):
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=self.challenge.public_key,
            installation_id=self.challenge.installation_id,
            client_id=self.challenge.client_id,
            platform=self.challenge.platform,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        mock_revoke.return_value = ("revoked", uuid.uuid4())

        response = self.bearer_client().delete(
            reverse("community_chat_device", args=(device.public_key,)),
            {"reason": "user_requested_device_removal"},
            format="json",
            HTTP_ORIGIN="mlaichat://callback",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        device.refresh_from_db()
        self.credentials.session.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        self.assertIsNotNone(self.credentials.session.revoked_at)
        denied = self.bearer_client().get(reverse("community_chat_account"))
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("community_chat.views.revoke_relay_membership")
    def test_account_session_cannot_revoke_another_device(self, mock_revoke):
        other_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=public_key(42),
            installation_id=uuid.uuid4(),
            client_id=self.challenge.client_id,
            platform=self.challenge.platform,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )

        response = self.bearer_client().delete(
            reverse("community_chat_device", args=(other_device.public_key,)),
            {"reason": "user_requested_device_removal"},
            format="json",
            HTTP_ORIGIN="mlaichat://callback",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_revoke.assert_not_called()

    def test_auth_version_change_invalidates_session(self):
        self.user.auth_version += 1
        self.user.save(update_fields=("auth_version",))

        denied = self.bearer_client().get(reverse("community_chat_account"))

        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cookie_refresh_requires_exact_bound_origin(self):
        web_challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=self.user,
            email_digest="d" * 64,
            code_digest="e" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin=ORIGIN,
            platform="web",
            device_name="Chrome",
            public_key="f" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        web_credentials = issue_account_session(self.user, web_challenge)
        client = APIClient()
        client.cookies["mlai_chat_refresh"] = web_credentials.refresh_token

        rejected = client.post(
            reverse("community_chat_session_refresh"),
            {},
            format="json",
            HTTP_ORIGIN="https://attacker.example",
        )
        self.assertEqual(rejected.status_code, status.HTTP_401_UNAUTHORIZED)

        accepted_client = APIClient()
        accepted_client.cookies["mlai_chat_refresh"] = web_credentials.refresh_token
        accepted = accepted_client.post(
            reverse("community_chat_session_refresh"),
            {},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)
        self.assertNotIn("access_token", accepted.data["session"])
        self.assertIn("mlai_chat_access", accepted.cookies)
        self.assertIn("mlai_chat_refresh", accepted.cookies)

    @patch("community_chat.views.get_relay_membership")
    def test_web_cookie_session_reconfirms_verified_device_after_reload(
        self,
        mock_membership,
    ):
        challenge, _, client = self.web_session()
        device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=challenge.public_key,
            installation_id=challenge.installation_id,
            client_id=challenge.client_id,
            platform=challenge.platform,
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        mock_membership.return_value = RelayMembership(True, "member", timezone.now())

        response = client.post(
            reverse("community_chat_confirm"),
            {"origin": ORIGIN, "public_key": challenge.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "verified")
        device.refresh_from_db()
        self.assertIsNotNone(device.last_seen_at)

    def test_web_cookie_session_can_resume_device_enrollment_after_reload(self):
        challenge, _, client = self.web_session(private_int=52)

        response = client.post(
            reverse("community_chat_challenge"),
            {"origin": ORIGIN, "public_key": challenge.public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        enrollment = CommunityChatChallenge.objects.get(
            id=response.data["challenge_id"]
        )
        self.assertEqual(enrollment.user, self.user)
        self.assertEqual(enrollment.public_key, challenge.public_key)
        self.assertEqual(enrollment.installation_id, challenge.installation_id)
        self.assertEqual(enrollment.client_id, challenge.client_id)

    def test_web_cookie_session_cannot_enroll_a_different_device_key(self):
        _, _, client = self.web_session(private_int=53)

        response = client.post(
            reverse("community_chat_challenge"),
            {"origin": ORIGIN, "public_key": public_key(54)},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(CommunityChatChallenge.objects.exists())

    def test_expired_access_is_rejected_while_refresh_remains_valid(self):
        CommunityChatAccountSession.objects.filter(id=self.credentials.session.id).update(
            access_expires_at=timezone.now() - timedelta(seconds=1)
        )
        denied = self.bearer_client().get(reverse("community_chat_account"))
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

        refreshed = APIClient().post(
            reverse("community_chat_session_refresh"),
            {"refresh_token": self.credentials.refresh_token},
            format="json",
        )
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
