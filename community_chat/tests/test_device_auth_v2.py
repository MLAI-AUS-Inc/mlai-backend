"""P1.4 desktop sign-in v2, within ADR-006: flag off is today, byte for byte."""

import base64
import hashlib
import uuid
from datetime import timedelta

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat import device_auth_v2
from community_chat.account_sessions import issue_account_session
from community_chat.device_revocation import revoke_device_credentials_locked
from community_chat.email_codes import code_digest
from community_chat.models import (
    CommunityChatBootstrapToken, CommunityChatDeviceAuthRequest, CommunityChatEmailCodeChallenge,
    CommunityMemberProfile,
)
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org

BROWSER_ORIGIN = "https://chat.mlai.au"
NATIVE_ORIGIN = "tauri://localhost"
CODE = "123456"
V2 = dict(COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED=True)
ALL_ON_WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


def _public_key(private_int):
    return PrivateKey.from_int(private_int).public_key.format(compressed=True)[1:].hex()


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[BROWSER_ORIGIN, NATIVE_ORIGIN],
    COMMUNITY_CHAT_FRONTEND_URL=BROWSER_ORIGIN,
    COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS=900,
    COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS=3600,
    COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS=1200,
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
    COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER="test-code-pepper",
    COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS=0,
)
class DeviceAuthV2Tests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="founder@example.com")
        self.other_user = get_user_model().objects.create_user(email="other@example.com")
        self.installation_id = uuid.uuid4()
        self.state = "state-" + "a" * 48
        self.verifier = "verifier-" + "b" * 48
        self.device = {"installation_id": str(self.installation_id), "name": "MLAI Chat · Mac",
                       "platform": "macos", "public_key": _public_key(7)}
        self._key = 30

    # -- desktop side -------------------------------------------------------

    def start(self):
        return self.client.post(reverse("community_chat_auth_start"), {
            "client_id": "mlai-chat-desktop", "code_challenge": _challenge(self.verifier),
            "device": self.device, "origin": NATIVE_ORIGIN, "state": self.state,
        }, format="json", HTTP_ORIGIN=NATIVE_ORIGIN)

    def started_id(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        return response.data["request_id"]

    def probe(self, request_id=None):
        query = {"probe": "1", **({"request_id": request_id} if request_id else {})}
        return APIClient().get(reverse("community_chat_auth_start"), query)

    def exchange(self, request_id, authorization_code):
        return self.client.post(reverse("community_chat_auth_exchange"), {
            "client_id": "mlai-chat-desktop", "code_verifier": self.verifier, "device": self.device,
            "origin": NATIVE_ORIGIN, "request_id": request_id, "state": self.state,
            "authorization_code": authorization_code,
        }, format="json", HTTP_ORIGIN=NATIVE_ORIGIN)

    def renew(self, request_id, state=None, **extra):
        return self.client.post(reverse("community_chat_auth_renew"), {
            "request_id": request_id, "state": state or self.state, **extra,
        }, format="json", HTTP_ORIGIN=NATIVE_ORIGIN)

    # -- browser side -------------------------------------------------------

    def browser_session(self, user=None):
        """An already signed-in browser: a web account session with no fresh code."""
        self._key += 1
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user or self.user, email_digest="d" * 64, code_digest="e" * 64, client_id="mlai-chat-web",
            installation_id=uuid.uuid4(), origin=BROWSER_ORIGIN, platform="web", device_name="Browser",
            public_key=_public_key(self._key), expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(user or self.user, challenge)
        client = APIClient()
        client.cookies["mlai_chat_access"] = credentials.access_token
        return client, credentials

    def verify_code_in_browser(self, user=None, *, device_request_id=None, client_id="mlai-chat-web"):
        """Enter a code on the browser page; returns a client carrying the new cookies."""
        self._key += 1
        installation_id = uuid.uuid4()
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user or self.user, email_digest="d" * 64, code_digest="0" * 64, client_id=client_id,
            installation_id=installation_id, origin=BROWSER_ORIGIN, platform="web", device_name="Browser",
            public_key=_public_key(self._key), expires_at=timezone.now() + timedelta(minutes=10),
        )
        challenge.code_digest = code_digest(challenge.id, CODE)
        challenge.save(update_fields=("code_digest",))
        client = APIClient()
        body = {"challenge_id": str(challenge.id), "code": CODE, "client_id": client_id,
                "installation_id": str(installation_id)}
        if device_request_id:
            body["device_request_id"] = device_request_id
        response = client.post(reverse("community_chat_email_code_verify"), body, format="json",
                               HTTP_ORIGIN=BROWSER_ORIGIN)
        self.assertEqual(response.status_code, 200, response.data)
        return client, response

    def read_authorize(self, client, request_id):
        return client.get(reverse("community_chat_auth_authorize"), {"request_id": request_id},
                          HTTP_ORIGIN=BROWSER_ORIGIN)

    def authorize(self, client, request_id):
        return client.post(reverse("community_chat_auth_authorize"), {"request_id": request_id},
                           format="json", HTTP_ORIGIN=BROWSER_ORIGIN)

    # -- flag off: today ----------------------------------------------------

    def test_flag_off_start_verify_authorize_and_exchange_are_today(self):
        started = self.start()
        self.assertEqual(set(started.data), {"request_id", "callback_path", "expires_at"})
        request_id = started.data["request_id"]
        browser, verified = self.verify_code_in_browser(device_request_id=request_id)
        self.assertEqual(verified.data["status"], "authenticated")
        self.assertIsNone(cache.get(f"device-auth-fresh:{request_id}"))
        self.assertEqual(self.read_authorize(browser, request_id).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        authorized = self.authorize(browser, request_id)
        self.assertEqual(set(authorized.data), {"status", "request_id", "authorization_code"})
        exchanged = self.exchange(request_id, authorized.data["authorization_code"])
        self.assertEqual(exchanged.status_code, 200)
        self.assertEqual(exchanged.data["status"], "authenticated")
        self.assertTrue(exchanged.data["bootstrap_token"].startswith("mlai_chat_"))
        self.assertEqual(set(exchanged.data),
                         {"status", "bootstrap_token", "expires_at", "relay_url", "origin", "profile", "session"})

    def test_flag_off_probe_says_flow_one_and_renew_is_absent(self):
        request_id = self.started_id()
        self.assertEqual(self.probe(request_id).data, {"flow": 1})
        self.assertEqual(self.probe().data, {"flow": 1})
        self.assertEqual(self.renew(request_id).status_code, 404)

    @override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=True)
    def test_flag_off_pending_member_is_refused_as_today(self):
        CommunityMemberProfile.objects.create(user=self.user, status=CommunityMemberProfile.Status.PENDING)
        request_id = self.started_id()
        browser, _ = self.browser_session()
        response = self.authorize(browser, request_id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"], "onboarding_required")

    # -- flag on ------------------------------------------------------------

    @override_settings(**V2)
    def test_start_and_probe_carry_flow_two_and_requested_at(self):
        started = self.start()
        self.assertEqual(started.data["flow"], 2)
        request_id = started.data["request_id"]
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        probe = self.probe(request_id)
        self.assertEqual(probe["Cache-Control"], "no-store")
        self.assertEqual(set(probe.data), {"flow", "requestedAt"})
        self.assertEqual(probe.data["flow"], 2)
        self.assertEqual(probe.data["requestedAt"], auth_request.created_at)
        self.assertEqual(self.probe().data, {"flow": 2})
        self.assertEqual(self.probe(str(uuid.uuid4())).data, {"flow": 2})
        self.assertEqual(self.probe("not-a-uuid").data, {"flow": 2})

    @override_settings(**V2)
    def test_probe_omits_requested_at_for_consumed_or_expired_requests(self):
        consumed, expired = self.started_id(), self.started_id()
        CommunityChatDeviceAuthRequest.objects.filter(id=consumed).update(consumed_at=timezone.now())
        CommunityChatDeviceAuthRequest.objects.filter(id=expired).update(expires_at=timezone.now())
        self.assertEqual(self.probe(consumed).data, {"flow": 2})
        self.assertEqual(self.probe(expired).data, {"flow": 2})

    @override_settings(**V2)
    def test_fresh_code_marker_is_bound_to_the_user_and_browser_session(self):
        request_id = self.started_id()
        browser, _ = self.verify_code_in_browser(device_request_id=request_id)
        marker = cache.get(f"device-auth-fresh:{request_id}")
        self.assertEqual(marker["user"], self.user.pk)
        self.assertEqual(len(marker["session"]), 32)
        self.assertEqual(device_auth_v2.FRESH_CODE_TTL_SECONDS, 600)
        read = self.read_authorize(browser, request_id)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read["Cache-Control"], "no-store")
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertEqual(read.data, {"signedInAs": "founder@example.com", "fresh": True, "flow": 2,
                                     "expiresAt": auth_request.expires_at,
                                     "requestedAt": auth_request.created_at})

    @override_settings(**V2)
    def test_fresh_is_false_for_another_user_or_another_session(self):
        request_id = self.started_id()
        self.verify_code_in_browser(device_request_id=request_id)
        same_user_other_browser, _ = self.browser_session()
        self.assertFalse(self.read_authorize(same_user_other_browser, request_id).data["fresh"])
        other_user_browser, _ = self.browser_session(self.other_user)
        self.assertFalse(self.read_authorize(other_user_browser, request_id).data["fresh"])

    @override_settings(**V2)
    def test_already_signed_in_browser_is_never_fresh_and_keeps_the_click(self):
        request_id = self.started_id()
        browser, _ = self.browser_session()
        self.assertFalse(self.read_authorize(browser, request_id).data["fresh"])
        self.assertIsNone(CommunityChatDeviceAuthRequest.objects.get(id=request_id).authorized_at)

    @override_settings(**V2)
    def test_a_code_for_a_non_web_client_or_unknown_request_sets_no_marker(self):
        request_id = self.started_id()
        self.verify_code_in_browser(device_request_id=str(uuid.uuid4()))
        self.verify_code_in_browser(device_request_id="garbage")
        self.assertIsNone(cache.get(f"device-auth-fresh:{request_id}"))

    @override_settings(**V2)
    def test_marker_is_single_use_after_authorize(self):
        request_id = self.started_id()
        browser, _ = self.verify_code_in_browser(device_request_id=request_id)
        self.assertEqual(self.authorize(browser, request_id).status_code, 200)
        self.assertIsNone(cache.get(f"device-auth-fresh:{request_id}"))

    @override_settings(**{**ALL_P1_FLAGS_ON, "COMMUNITY_CHAT_DEVICE_AUTH_ENABLED": True})
    def test_legacy_founder_gets_flow_one_even_when_fresh(self):
        legacy = make_named_legacy_org("talathrive")
        request_id = self.started_id()
        browser, _ = self.verify_code_in_browser(legacy.owner, device_request_id=request_id)
        read = self.read_authorize(browser, request_id).data
        self.assertEqual(read["flow"], 1)
        self.assertTrue(read["fresh"])

    @override_settings(**{**ALL_ON_WITHOUT_V2, "COMMUNITY_CHAT_DEVICE_AUTH_ENABLED": True})
    def test_legacy_founder_keeps_flow_one_with_the_journey_switched_off(self):
        legacy = make_named_legacy_org("la_vie", marker=False)
        request_id = self.started_id()
        browser, _ = self.verify_code_in_browser(legacy.owner, device_request_id=request_id)
        self.assertEqual(self.read_authorize(browser, request_id).data["flow"], 1)

    @override_settings(**V2)
    def test_authorize_read_404s_for_unknown_consumed_and_expired_requests(self):
        browser, _ = self.browser_session()
        consumed, expired = self.started_id(), self.started_id()
        CommunityChatDeviceAuthRequest.objects.filter(id=consumed).update(consumed_at=timezone.now())
        CommunityChatDeviceAuthRequest.objects.filter(id=expired).update(expires_at=timezone.now())
        for request_id in (str(uuid.uuid4()), consumed, expired):
            with self.subTest(request_id=request_id):
                self.assertEqual(self.read_authorize(browser, request_id).status_code, 404)

    @override_settings(**V2)
    def test_authorize_read_needs_the_browser_cookie(self):
        request_id = self.started_id()
        _, credentials = self.browser_session()
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        self.assertEqual(self.read_authorize(bearer, request_id).status_code, 403)
        self.assertIn(self.read_authorize(APIClient(), request_id).status_code, (401, 403))

    @override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=True, **V2)
    def test_pending_member_authorizes_and_exchanges_into_an_account_session_only(self):
        CommunityMemberProfile.objects.create(user=self.user, status=CommunityMemberProfile.Status.PENDING)
        request_id = self.started_id()
        browser, verified = self.verify_code_in_browser(device_request_id=request_id)
        self.assertEqual(verified.data["status"], "onboarding_required")
        self.assertTrue(self.read_authorize(browser, request_id).data["fresh"])
        authorized = self.authorize(browser, request_id)
        self.assertEqual(authorized.status_code, 200, authorized.data)
        exchanged = self.exchange(request_id, authorized.data["authorization_code"])
        self.assertEqual(exchanged.status_code, 200, exchanged.data)
        self.assertEqual(exchanged.data["status"], "onboarding_required")
        self.assertEqual(exchanged.data["bootstrap_token"], "")
        self.assertTrue(exchanged.data["session"]["access_token"].startswith("mlai_session_access_"))
        self.assertEqual(exchanged.data["onboarding"]["status"], "pending_review")
        self.assertFalse(CommunityChatBootstrapToken.objects.filter(user=self.user,
                                                                    client_id="mlai-chat-desktop").exists())

    @override_settings(COMMUNITY_CHAT_SIGNUP_ENABLED=True, **V2)
    def test_rejected_or_suspended_members_still_cannot_authorize(self):
        profile = CommunityMemberProfile.objects.create(user=self.user, status=CommunityMemberProfile.Status.REJECTED)
        request_id = self.started_id()
        browser, _ = self.browser_session()
        for profile_status in (CommunityMemberProfile.Status.REJECTED, CommunityMemberProfile.Status.SUSPENDED):
            with self.subTest(status=profile_status):
                CommunityMemberProfile.objects.filter(pk=profile.pk).update(status=profile_status)
                self.assertEqual(self.authorize(browser, request_id).status_code, 403)
                self.assertEqual(self.read_authorize(browser, request_id).status_code, 403)

    @override_settings(**V2)
    def test_approved_member_exchange_is_unchanged_with_v2_on(self):
        request_id = self.started_id()
        browser, _ = self.browser_session()
        authorized = self.authorize(browser, request_id)
        exchanged = self.exchange(request_id, authorized.data["authorization_code"])
        self.assertEqual(exchanged.data["status"], "authenticated")
        self.assertTrue(exchanged.data["bootstrap_token"].startswith("mlai_chat_"))

    # -- renew --------------------------------------------------------------

    @override_settings(**V2)
    def test_renew_extends_a_live_request_within_the_cap(self):
        request_id = self.started_id()
        auth_request = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        CommunityChatDeviceAuthRequest.objects.filter(id=request_id).update(
            created_at=timezone.now() - timedelta(minutes=20), expires_at=timezone.now() + timedelta(seconds=30))
        renewed = self.renew(request_id)
        self.assertEqual(renewed.status_code, 200, renewed.data)
        auth_request.refresh_from_db()
        self.assertEqual(renewed.data, {"expiresAt": auth_request.expires_at})
        self.assertGreater(auth_request.expires_at, timezone.now() + timedelta(minutes=14))
        self.assertEqual(self.probe(request_id).data["requestedAt"], auth_request.created_at)

    @override_settings(**V2)
    def test_renew_never_revives_an_expired_request(self):
        request_id = self.started_id()
        expired_at = timezone.now() - timedelta(seconds=5)
        CommunityChatDeviceAuthRequest.objects.filter(id=request_id).update(
            created_at=timezone.now() - timedelta(minutes=20), expires_at=expired_at)
        response = self.renew(request_id)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.data, {"detail": "This sign-in link expired. Start again.",
                                         "code": "device_request_expired"})
        self.assertEqual(CommunityChatDeviceAuthRequest.objects.get(id=request_id).expires_at, expired_at)

    @override_settings(**V2)
    def test_renew_cannot_undo_device_revocation(self):
        """Deleting the device fences the handoff; renew must not bring it back."""
        request_id = self.started_id()
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=self.user.pk)
            revoke_device_credentials_locked(
                self.user, public_key=self.device["public_key"], installation_id=self.installation_id)
        fenced = CommunityChatDeviceAuthRequest.objects.get(id=request_id)
        self.assertLessEqual(fenced.expires_at, timezone.now())
        self.assertEqual(self.renew(request_id).status_code, 410)
        fenced.refresh_from_db()
        self.assertLessEqual(fenced.expires_at, timezone.now())
        browser, _ = self.browser_session()
        self.assertEqual(self.authorize(browser, request_id).status_code, 410)
        self.assertIsNone(CommunityChatDeviceAuthRequest.objects.get(id=request_id).authorized_at)

    @override_settings(**V2)
    def test_renew_is_capped_at_the_max_lifetime(self):
        request_id = self.started_id()
        created = timezone.now() - timedelta(minutes=55)
        CommunityChatDeviceAuthRequest.objects.filter(id=request_id).update(created_at=created)
        renewed = self.renew(request_id)
        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(renewed.data["expiresAt"], created + timedelta(seconds=3600))

    @override_settings(**V2)
    def test_renew_gives_410_past_the_cap_or_once_consumed(self):
        past_cap, consumed = self.started_id(), self.started_id()
        CommunityChatDeviceAuthRequest.objects.filter(id=past_cap).update(
            created_at=timezone.now() - timedelta(seconds=3601))
        CommunityChatDeviceAuthRequest.objects.filter(id=consumed).update(consumed_at=timezone.now())
        for request_id in (past_cap, consumed):
            with self.subTest(request_id=request_id):
                response = self.renew(request_id)
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.data, {"detail": "This sign-in link expired. Start again.",
                                                 "code": "device_request_expired"})

    @override_settings(**V2)
    def test_renew_needs_the_start_state_and_the_app_origin(self):
        request_id = self.started_id()
        before = CommunityChatDeviceAuthRequest.objects.get(id=request_id).expires_at
        self.assertEqual(self.renew(request_id, state="state-" + "z" * 48).status_code, 403)
        browser_origin = self.client.post(reverse("community_chat_auth_renew"),
                                          {"request_id": request_id, "state": self.state},
                                          format="json", HTTP_ORIGIN=BROWSER_ORIGIN)
        self.assertEqual(browser_origin.status_code, 403)
        self.assertEqual(self.renew(str(uuid.uuid4())).status_code, 404)
        self.assertEqual(CommunityChatDeviceAuthRequest.objects.get(id=request_id).expires_at, before)

    @override_settings(**V2)
    def test_renew_falls_back_to_the_full_state_binding_without_the_cache(self):
        request_id = self.started_id()
        cache.delete(f"device-auth-renew:{request_id}")
        self.assertEqual(self.renew(request_id).status_code, 403)
        full = self.renew(request_id, client_id="mlai-chat-desktop", device=self.device)
        self.assertEqual(full.status_code, 200, full.data)

    @override_settings(**V2)
    def test_an_authorized_request_is_not_extended(self):
        request_id = self.started_id()
        browser, _ = self.browser_session()
        self.authorize(browser, request_id)
        before = CommunityChatDeviceAuthRequest.objects.get(id=request_id).expires_at
        renewed = self.renew(request_id)
        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(renewed.data["expiresAt"], before)
