import base64
import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.account_sessions import issue_account_session
from community_chat.adapter import MembershipAdapterUnavailable
from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatEmailCodeChallenge,
    DeviceBindingStatus,
)


BROWSER_ORIGIN = "https://chat.mlai.au"
NATIVE_ORIGIN = "tauri://localhost"
ALTERNATE_NATIVE_ORIGIN = "http://tauri.localhost"


def _public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[
        BROWSER_ORIGIN,
        NATIVE_ORIGIN,
        ALTERNATE_NATIVE_ORIGIN,
    ],
    COMMUNITY_CHAT_FRONTEND_URL=BROWSER_ORIGIN,
    COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS=900,
    COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS=1200,
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
)
class CommunityChatDeviceAuthTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="device-auth@example.com")
        self.other_user = get_user_model().objects.create_user(email="other-device-auth@example.com")
        self.public_key = _public_key(7)
        self.other_public_key = _public_key(8)
        self.installation_id = uuid.uuid4()
        self.state = "state-" + "a" * 48
        self.verifier = "verifier-" + "b" * 48
        self.device = {
            "installation_id": str(self.installation_id),
            "name": "MLAI Chat · Mac",
            "platform": "macos",
            "public_key": self.public_key,
        }

    def start(self, **overrides):
        payload = {
            "client_id": "mlai-chat-desktop",
            "code_challenge": _challenge(self.verifier),
            "device": self.device,
            "origin": NATIVE_ORIGIN,
            "state": self.state,
            **overrides,
        }
        return self.client.post(
            reverse("community_chat_auth_start"),
            payload,
            format="json",
            HTTP_ORIGIN=NATIVE_ORIGIN,
        )

    def exchange(
        self,
        request_id,
        *,
        authorization_code=None,
        origin=NATIVE_ORIGIN,
        **overrides,
    ):
        payload = {
            "client_id": "mlai-chat-desktop",
            "code_verifier": self.verifier,
            "device": self.device,
            "origin": origin,
            "request_id": request_id,
            "state": self.state,
            **overrides,
        }
        if authorization_code is not None:
            payload["authorization_code"] = authorization_code
        return self.client.post(
            reverse("community_chat_auth_exchange"),
            payload,
            format="json",
            HTTP_ORIGIN=origin,
        )

    def browser_client(self, user=None, *, private_int=31):
        user = user or self.user
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user,
            email_digest="d" * 64,
            code_digest="e" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin=BROWSER_ORIGIN,
            platform="web",
            device_name="Browser",
            public_key=_public_key(private_int),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(user, challenge)
        client = APIClient()
        client.cookies["mlai_chat_access"] = credentials.access_token
        return client

    def authorize(self, request_id, *, client=None):
        return (client or self.browser_client()).post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            format="json",
            HTTP_ORIGIN=BROWSER_ORIGIN,
        )

    @override_settings(COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=False)
    def test_browser_handoff_can_be_disabled_fail_closed(self):
        response = self.start()

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"error": "device_auth_disabled"})

    def test_pkce_handoff_issues_bootstrap_and_native_account_sessions_once(self):
        started = self.start()

        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(started["Cache-Control"], "no-store")
        self.assertEqual(started["Pragma"], "no-cache")
        request_id = started.data["request_id"]
        self.assertEqual(
            started.data["callback_path"],
            f"/auth/desktop?request={request_id}",
        )
        self.assertNotIn(self.state, started.data["callback_path"])
        self.assertNotIn(self.verifier, started.data["callback_path"])
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertNotEqual(auth_request.state_hash, self.state)
        self.assertNotIn(self.state, str(auth_request.__dict__))

        authorized = self.authorize(request_id)
        self.assertEqual(authorized.status_code, status.HTTP_200_OK)
        self.assertEqual(authorized["Cache-Control"], "no-store")
        self.assertEqual(authorized["Pragma"], "no-cache")
        authorization_code = authorized.data["authorization_code"]
        self.assertNotIn("access_token", authorized.data)
        self.assertNotIn("refresh_token", authorized.data)
        self.assertNotIn("bootstrap_token", authorized.data)

        exchanged = self.exchange(
            request_id,
            authorization_code=authorization_code,
        )
        self.assertEqual(exchanged.status_code, status.HTTP_200_OK)
        self.assertEqual(exchanged["Cache-Control"], "no-store")
        self.assertEqual(exchanged["Pragma"], "no-cache")
        self.assertEqual(exchanged.data["status"], "authenticated")
        bootstrap_token = exchanged.data["bootstrap_token"]
        access_token = exchanged.data["session"]["access_token"]
        refresh_token = exchanged.data["session"]["refresh_token"]
        self.assertTrue(bootstrap_token.startswith("mlai_chat_"))
        self.assertTrue(access_token.startswith("mlai_session_access_"))
        self.assertTrue(refresh_token.startswith("mlai_session_refresh_"))
        self.assertEqual(exchanged.data["origin"], NATIVE_ORIGIN)
        self.assertEqual(exchanged.data["profile"]["email"], self.user.email)
        self.assertEqual(exchanged.data["session"]["client_id"], "mlai-chat-desktop")
        self.assertEqual(
            exchanged.data["session"]["installation_id"],
            str(self.installation_id),
        )

        bootstrap = CommunityChatBootstrapToken.objects.get(
            user=self.user,
            client_id="mlai-chat-desktop",
        )
        self.assertNotEqual(bootstrap.token_hash, bootstrap_token)
        self.assertEqual(bootstrap.public_key, self.public_key)
        self.assertEqual(bootstrap.installation_id, self.installation_id)
        self.assertEqual(bootstrap.origin, NATIVE_ORIGIN)
        self.assertEqual(bootstrap.platform, "macos")
        self.assertEqual(bootstrap.name, self.device["name"])
        desktop_session = CommunityChatAccountSession.objects.get(
            user=self.user,
            client_id="mlai-chat-desktop",
            revoked_at__isnull=True,
        )
        self.assertNotEqual(desktop_session.access_token_hash, access_token)
        self.assertNotEqual(desktop_session.refresh_token_hash, refresh_token)
        self.assertEqual(desktop_session.public_key, self.public_key)
        self.assertEqual(desktop_session.installation_id, self.installation_id)
        self.assertEqual(desktop_session.platform, "macos")
        self.assertEqual(desktop_session.name, self.device["name"])

        account_client = APIClient()
        account_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        account = account_client.get(reverse("community_chat_account"))
        self.assertEqual(account.status_code, status.HTTP_200_OK)

        bootstrap_client = APIClient()
        bootstrap_client.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_token}")
        session = bootstrap_client.get(reverse("community_chat_session"))
        self.assertEqual(session.status_code, status.HTTP_200_OK)
        wrong_key = bootstrap_client.post(
            reverse("community_chat_challenge"),
            {"origin": NATIVE_ORIGIN, "public_key": self.other_public_key},
            format="json",
            HTTP_ORIGIN=NATIVE_ORIGIN,
        )
        self.assertEqual(wrong_key.status_code, status.HTTP_403_FORBIDDEN)

        replay = self.exchange(
            request_id,
            authorization_code=authorization_code,
        )
        self.assertEqual(replay.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(replay.data["error"], "authorization_consumed")

    def test_authorize_requires_chat_cookie_and_an_explicit_post(self):
        request_id = self.start().data["request_id"]

        unauthenticated = APIClient().post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            format="json",
            HTTP_ORIGIN=BROWSER_ORIGIN,
        )
        self.assertEqual(unauthenticated.status_code, status.HTTP_401_UNAUTHORIZED)

        browser = self.browser_client()
        bearer_only = APIClient()
        bearer_only.credentials(
            HTTP_AUTHORIZATION=f"Bearer {browser.cookies['mlai_chat_access'].value}"
        )
        bearer_approval = bearer_only.post(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            format="json",
            HTTP_ORIGIN=BROWSER_ORIGIN,
        )
        self.assertEqual(bearer_approval.status_code, status.HTTP_403_FORBIDDEN)

        passive_navigation = browser.get(
            reverse("community_chat_auth_authorize"),
            {"request_id": request_id},
            HTTP_ORIGIN=BROWSER_ORIGIN,
        )
        self.assertEqual(passive_navigation.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertIsNone(auth_request.authorized_at)

        approved = self.authorize(request_id, client=browser)
        self.assertEqual(approved.status_code, status.HTTP_200_OK)

    def test_exchange_requires_the_browser_returned_authorization_code(self):
        request_id = self.start().data["request_id"]
        authorized = self.authorize(request_id)
        self.assertEqual(authorized.status_code, status.HTTP_200_OK)

        missing = self.exchange(request_id)
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("authorization_code", missing.data)

        authorization_code = authorized.data["authorization_code"]
        replacement = "A" if authorization_code[-1] != "A" else "B"
        tampered = self.exchange(
            request_id,
            authorization_code=authorization_code[:-1] + replacement,
        )
        self.assertEqual(tampered.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            str(tampered.data["detail"]),
            "Desktop authorization code is invalid.",
        )

        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertIsNone(auth_request.consumed_at)
        self.assertFalse(
            CommunityChatAccountSession.objects.filter(
                client_id="mlai-chat-desktop"
            ).exists()
        )

    def test_authorization_code_is_bound_to_its_locked_request_and_user(self):
        first_request_id = self.start().data["request_id"]
        second_request_id = self.start().data["request_id"]
        first_code = self.authorize(first_request_id).data["authorization_code"]
        second_code = self.authorize(second_request_id).data["authorization_code"]

        crossed = self.exchange(
            second_request_id,
            authorization_code=first_code,
        )
        self.assertEqual(crossed.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            str(crossed.data["detail"]),
            "Desktop authorization code is invalid.",
        )
        first = CommunityChatDeviceAuthRequest.objects.get(id=first_request_id)
        second = CommunityChatDeviceAuthRequest.objects.get(id=second_request_id)
        self.assertIsNone(first.consumed_at)
        self.assertIsNone(second.consumed_at)

        wrong_user_code = signing.dumps(
            {
                "request_id": str(second_request_id),
                "user_id": str(self.other_user.id),
                "nonce": "n" * 32,
            },
            salt="community-chat.desktop-authorization.v1",
            compress=False,
        )
        wrong_user = self.exchange(
            second_request_id,
            authorization_code=wrong_user_code,
        )
        self.assertEqual(wrong_user.status_code, status.HTTP_403_FORBIDDEN)
        second.refresh_from_db()
        self.assertIsNone(second.consumed_at)

        exchanged = self.exchange(
            second_request_id,
            authorization_code=second_code,
        )
        self.assertEqual(exchanged.status_code, status.HTTP_200_OK)

    def test_expired_signed_authorization_code_is_rejected_without_consuming(self):
        request_id = self.start().data["request_id"]
        authorization_code = self.authorize(request_id).data["authorization_code"]

        with patch(
            "community_chat.views.signing.loads",
            side_effect=signing.SignatureExpired("expired"),
        ) as loads:
            response = self.exchange(
                request_id,
                authorization_code=authorization_code,
            )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        loads.assert_called_once_with(
            authorization_code,
            salt="community-chat.desktop-authorization.v1",
            max_age=900,
        )
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertIsNone(auth_request.consumed_at)

    def test_start_accepts_only_registered_desktop_metadata_and_tauri_origins(self):
        web_client = self.start(
            client_id="mlai-chat-web",
            device={**self.device, "platform": "web"},
        )
        self.assertEqual(web_client.status_code, status.HTTP_400_BAD_REQUEST)

        wrong_platform = self.start(device={**self.device, "platform": "web"})
        self.assertEqual(wrong_platform.status_code, status.HTTP_400_BAD_REQUEST)

        browser_origin = self.client.post(
            reverse("community_chat_auth_start"),
            {
                "client_id": "mlai-chat-desktop",
                "code_challenge": _challenge(self.verifier),
                "device": self.device,
                "origin": BROWSER_ORIGIN,
                "state": self.state,
            },
            format="json",
            HTTP_ORIGIN=BROWSER_ORIGIN,
        )
        self.assertEqual(browser_origin.status_code, status.HTTP_403_FORBIDDEN)

        trailing_slash_origin = self.client.post(
            reverse("community_chat_auth_start"),
            {
                "client_id": "mlai-chat-desktop",
                "code_challenge": _challenge(self.verifier),
                "device": self.device,
                "origin": NATIVE_ORIGIN + "/",
                "state": self.state,
            },
            format="json",
            HTTP_ORIGIN=NATIVE_ORIGIN + "/",
        )
        self.assertEqual(
            trailing_slash_origin.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_state_pkce_device_metadata_and_origin_are_bound_without_consuming_on_failure(self):
        request_id = self.start().data["request_id"]
        authorized = self.authorize(request_id)
        self.assertEqual(authorized.status_code, status.HTTP_200_OK)
        authorization_code = authorized.data["authorization_code"]

        bad_state = self.exchange(
            request_id,
            authorization_code=authorization_code,
            state="tampered-" + "x" * 40,
        )
        self.assertEqual(bad_state.status_code, status.HTTP_403_FORBIDDEN)
        bad_verifier = self.exchange(
            request_id,
            authorization_code=authorization_code,
            code_verifier="tampered-" + "y" * 40,
        )
        self.assertEqual(bad_verifier.status_code, status.HTTP_403_FORBIDDEN)
        changed_installation = self.exchange(
            request_id,
            authorization_code=authorization_code,
            device={**self.device, "installation_id": str(uuid.uuid4())},
        )
        self.assertEqual(changed_installation.status_code, status.HTTP_403_FORBIDDEN)
        changed_platform = self.exchange(
            request_id,
            authorization_code=authorization_code,
            device={**self.device, "platform": "windows"},
        )
        self.assertEqual(changed_platform.status_code, status.HTTP_403_FORBIDDEN)
        changed_name = self.exchange(
            request_id,
            authorization_code=authorization_code,
            device={**self.device, "name": "Another computer"},
        )
        self.assertEqual(changed_name.status_code, status.HTTP_403_FORBIDDEN)
        changed_key = self.exchange(
            request_id,
            authorization_code=authorization_code,
            device={**self.device, "public_key": self.other_public_key},
        )
        self.assertEqual(changed_key.status_code, status.HTTP_403_FORBIDDEN)
        changed_origin = self.exchange(
            request_id,
            authorization_code=authorization_code,
            origin=ALTERNATE_NATIVE_ORIGIN,
        )
        self.assertEqual(changed_origin.status_code, status.HTTP_403_FORBIDDEN)

        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertIsNone(auth_request.consumed_at)
        self.assertFalse(
            CommunityChatAccountSession.objects.filter(
                client_id="mlai-chat-desktop"
            ).exists()
        )
        self.assertEqual(
            self.exchange(
                request_id,
                authorization_code=authorization_code,
            ).status_code,
            status.HTTP_200_OK,
        )

    def test_authorization_cannot_be_reassigned_to_another_browser_account(self):
        request_id = self.start().data["request_id"]
        self.assertEqual(self.authorize(request_id).status_code, status.HTTP_200_OK)

        other_browser = self.browser_client(self.other_user, private_int=32)
        reassigned = self.authorize(request_id, client=other_browser)

        self.assertEqual(reassigned.status_code, status.HTTP_403_FORBIDDEN)
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertEqual(auth_request.user, self.user)

    def test_start_and_exchange_validate_state_and_pkce_shapes(self):
        short_state = self.start(state="short")
        self.assertEqual(short_state.status_code, status.HTTP_400_BAD_REQUEST)
        bad_challenge = self.start(code_challenge="not-s256")
        self.assertEqual(bad_challenge.status_code, status.HTTP_400_BAD_REQUEST)
        newline_challenge = self.start(
            code_challenge=_challenge(self.verifier) + "\n"
        )
        self.assertEqual(newline_challenge.status_code, status.HTTP_400_BAD_REQUEST)

        request_id = self.start().data["request_id"]
        authorized = self.authorize(request_id)
        authorization_code = authorized.data["authorization_code"]
        short_verifier = self.exchange(
            request_id,
            authorization_code=authorization_code,
            code_verifier="short",
        )
        self.assertEqual(short_verifier.status_code, status.HTTP_400_BAD_REQUEST)
        invalid_verifier = self.exchange(
            request_id,
            authorization_code=authorization_code,
            code_verifier="!" * 48,
        )
        self.assertEqual(invalid_verifier.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expired_request_does_not_issue_credentials(self):
        request_id = self.start().data["request_id"]
        authorized = self.authorize(request_id)
        authorization_code = authorized.data["authorization_code"]
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        auth_request.expires_at = timezone.now() - timedelta(seconds=1)
        auth_request.save(update_fields=("expires_at",))

        expired = self.exchange(
            request_id,
            authorization_code=authorization_code,
        )

        self.assertEqual(expired.status_code, status.HTTP_410_GONE)
        self.assertFalse(
            CommunityChatAccountSession.objects.filter(
                client_id="mlai-chat-desktop"
            ).exists()
        )

    @patch("community_chat.views.revoke_relay_membership")
    def test_membership_adapter_failure_rolls_back_exchange(self, mock_revoke):
        old_device = CommunityChatDevice.objects.create(
            user=self.user,
            public_key=_public_key(9),
            installation_id=self.installation_id,
            client_id="mlai-chat-desktop",
            platform="macos",
            name="Old Mac",
            status=DeviceBindingStatus.VERIFIED,
            verified_at=timezone.now(),
        )
        mock_revoke.side_effect = MembershipAdapterUnavailable("adapter unavailable")
        request_id = self.start().data["request_id"]
        authorized = self.authorize(request_id)
        self.assertEqual(authorized.status_code, status.HTTP_200_OK)

        failed = self.exchange(
            request_id,
            authorization_code=authorized.data["authorization_code"],
        )

        self.assertEqual(failed.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertIsNone(auth_request.consumed_at)
        old_device.refresh_from_db()
        self.assertEqual(old_device.status, DeviceBindingStatus.VERIFIED)
        self.assertFalse(
            CommunityChatBootstrapToken.objects.filter(
                client_id="mlai-chat-desktop"
            ).exists()
        )
        self.assertFalse(
            CommunityChatAccountSession.objects.filter(
                client_id="mlai-chat-desktop"
            ).exists()
        )
