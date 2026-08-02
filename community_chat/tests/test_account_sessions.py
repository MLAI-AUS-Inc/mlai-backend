from datetime import timedelta
import hashlib
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatEmailCodeChallenge,
)


ORIGIN = "https://chat.mlai.au"


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
            public_key="c" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.credentials = issue_account_session(self.user, self.challenge)

    def bearer_client(self, token=None):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token or self.credentials.access_token}"
        )
        return client

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
