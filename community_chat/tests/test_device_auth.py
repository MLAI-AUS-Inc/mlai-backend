import base64
import hashlib

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.models import (
    CommunityChatBootstrapToken,
    CommunityChatDeviceAuthRequest,
)


ORIGIN = "https://chat.mlai.au"
NATIVE_ORIGIN = "mlaichat://callback"


def _public_key(private_key):
    return private_key.public_key.format(compressed=True)[1:].hex()


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, NATIVE_ORIGIN],
    COMMUNITY_CHAT_FRONTEND_URL=ORIGIN,
    COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS=900,
    COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS=1200,
)
class CommunityChatDeviceAuthTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="device-auth@example.com")
        self.public_key = _public_key(PrivateKey.from_int(7))
        self.other_public_key = _public_key(PrivateKey.from_int(8))
        self.state = "state-" + "a" * 48
        self.verifier = "verifier-" + "b" * 48

    def start(self, **overrides):
        payload = {
            "public_key": self.public_key,
            "state": self.state,
            "code_challenge": _challenge(self.verifier),
            **overrides,
        }
        return self.client.post(
            reverse("community_chat_auth_start"),
            payload,
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

    def exchange(self, request_id, **overrides):
        payload = {
            "request_id": request_id,
            "state": self.state,
            "code_verifier": self.verifier,
            **overrides,
        }
        return self.client.post(
            reverse("community_chat_auth_exchange"),
            payload,
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )

    @override_settings(COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=False)
    def test_browser_handoff_is_disabled_for_email_code_only_launches(self):
        response = self.start()

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"error": "device_auth_disabled"})

    def test_state_bound_pkce_handoff_issues_scoped_token_once(self):
        started = self.start()
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        request_id = started.data["request_id"]
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertNotEqual(auth_request.state_hash, self.state)
        self.assertNotIn(self.state, str(auth_request.__dict__))

        pending = self.exchange(request_id)
        self.assertEqual(pending.status_code, status.HTTP_202_ACCEPTED)

        self.client.force_authenticate(user=self.user)
        authorized = self.client.post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(authorized.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(user=None)
        exchanged = self.exchange(request_id)
        self.assertEqual(exchanged.status_code, status.HTTP_200_OK)
        token = exchanged.data["access_token"]
        self.assertTrue(token.startswith("mlai_chat_"))
        token_row = CommunityChatBootstrapToken.objects.get()
        self.assertNotEqual(token_row.token_hash, token)
        self.assertEqual(token_row.public_key, self.public_key)

        replay = self.exchange(request_id)
        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data["error"], "authorization_consumed")

        token_client = APIClient()
        token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        session = token_client.get(reverse("community_chat_session"))
        self.assertEqual(session.status_code, status.HTTP_200_OK)
        wrong_key = token_client.post(
            reverse("community_chat_challenge"),
            {"public_key": self.other_public_key},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(wrong_key.status_code, status.HTTP_403_FORBIDDEN)

    def test_native_request_is_approved_by_browser_then_exchanged_by_native_origin(self):
        started = self.client.post(
            reverse("community_chat_auth_start"),
            {
                "public_key": self.public_key,
                "state": self.state,
                "code_challenge": _challenge(self.verifier),
                "origin": NATIVE_ORIGIN,
            },
            format="json",
            HTTP_ORIGIN=NATIVE_ORIGIN,
        )
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        request_id = started.data["request_id"]

        self.client.force_authenticate(user=self.user)
        approved = self.client.post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id, "origin": ORIGIN},
            format="json",
            HTTP_ORIGIN=ORIGIN,
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(user=None)
        exchanged = self.client.post(
            reverse("community_chat_auth_exchange"),
            {
                "request_id": request_id,
                "state": self.state,
                "code_verifier": self.verifier,
                "origin": NATIVE_ORIGIN,
            },
            format="json",
            HTTP_ORIGIN=NATIVE_ORIGIN,
        )
        self.assertEqual(exchanged.status_code, status.HTTP_200_OK)
        self.assertEqual(exchanged.data["public_key"], self.public_key)

    def test_bad_state_origin_and_verifier_fail_without_consuming_request(self):
        request_id = self.start().data["request_id"]
        self.client.force_authenticate(user=self.user)
        mismatch = self.client.post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            format="json",
            HTTP_ORIGIN="https://attacker.example",
        )
        self.assertEqual(mismatch.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(user=None)

        bad_state = self.exchange(request_id, state="tampered-" + "x" * 40)
        self.assertEqual(bad_state.status_code, status.HTTP_403_FORBIDDEN)
        bad_verifier = self.exchange(request_id, code_verifier="tampered-" + "y" * 40)
        self.assertEqual(bad_verifier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIsNone(CommunityChatDeviceAuthRequest.objects.get(id=request_id).consumed_at)

    def test_start_rejects_short_state_and_invalid_pkce(self):
        short_state = self.start(state="short")
        self.assertEqual(short_state.status_code, status.HTTP_400_BAD_REQUEST)
        bad_challenge = self.start(code_challenge="not-s256")
        self.assertEqual(bad_challenge.status_code, status.HTTP_400_BAD_REQUEST)
