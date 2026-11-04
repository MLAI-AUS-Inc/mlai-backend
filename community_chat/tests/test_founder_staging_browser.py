"""Normal PostgreSQL auth/join handlers behind the fixture admission boundary.

These synthetic in-process requests prove neither hosted signup nor relay/model
readiness. They use ordinary email challenges, pending sessions and join locks.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat import email_delivery
from community_chat.models import CommunityChatEmailCodeChallenge, CommunityChatEmailCodeDelivery, CommunityMemberConsent
from community_chat.tests.founder_staging_fixtures import SETTINGS
from community_chat.tests.test_email_code_auth import public_key

PROFILE = {**SETTINGS, "ROOT_URLCONF": "mlai.urls", "COMMUNITY_CHAT_ALLOWED_ORIGINS": [SETTINGS["COMMUNITY_CHAT_FRONTEND_URL"]],
    "COMMUNITY_CHAT_HOST_ONLY_COOKIES": True, "COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES": False,
    "COMMUNITY_CHAT_ACCESS_COOKIE_NAME": "__Host-mlai_founder_access",
    "COMMUNITY_CHAT_REFRESH_COOKIE_NAME": "__Host-mlai_founder_refresh",
    "COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED": True, "COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED": False,
    "COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS": 0,
    "COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET": "synthetic-browser-outbox-delivery-key"}


@override_settings(**PROFILE)
class FounderBrowserTests(TestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_"))
        cache.clear()
        self.enterContext(override_settings(SETTINGS_MODULE=SETTINGS["SETTINGS_MODULE"],
            MIDDLEWARE=["mlai.founder_staging_admission.FounderAdmissionMiddleware", *settings.MIDDLEWARE]))
        self.client = APIClient()
        self.origin = SETTINGS["COMMUNITY_CHAT_FRONTEND_URL"]
        self.client.credentials(HTTP_ORIGIN=self.origin)
        self.key, self.installation = public_key(341), str(uuid4())
        self.email = f"founder-acceptance-{uuid4()}@{SETTINGS['FOUNDER_STAGING_EMAIL_DOMAIN']}"
        self.body = {"email": self.email, "client_id": "mlai-chat-web", "onboarding_version": 1,
                     "device": {"platform": "web", "installation_id": self.installation, "public_key": self.key}}
        self.root = "/api/v1/community-chat/"

    def sign_in(self):
        requested = self.client.post(self.root + "auth/email-code/request/", self.body, format="json")
        self.assertEqual(requested.status_code, 202, getattr(requested, "data", requested.content))
        row = CommunityChatEmailCodeDelivery.objects.get(challenge_id=requested.data["challenge_id"])
        # Only the provider is replaced here; full leased HTTP delivery has its
        # separate real transport test. Do not count this as hosted inbox proof.
        with patch("community_chat.founder_staging_email.send_fixture_code", return_value={"delivery_id": str(row.id)}) as provider:
            self.assertEqual(email_delivery.process_email_code_deliveries(limit=1)["sent"], 1)
        code = provider.call_args.kwargs["code"]
        response = self.client.post(self.root + "auth/email-code/verify/", {
            "challenge_id": requested.data["challenge_id"], "code": code,
            "client_id": "mlai-chat-web", "installation_id": self.installation}, format="json")
        self.assertEqual(response.status_code, 200, getattr(response, "data", response.content))
        self.client.credentials(HTTP_ORIGIN=self.origin, HTTP_X_MLAI_PUBLIC_KEY=self.key)
        return response

    def test_normal_pending_account_can_join_without_a_manufactured_approved_user(self):
        self.assertFalse(get_user_model().objects.filter(email=self.email).exists())
        response = self.sign_in()
        self.assertEqual(response.data["status"], "onboarding_required")
        user = get_user_model().objects.get(email=self.email)
        from community_chat.onboarding import has_community_access
        self.assertFalse(has_community_access(user))
        account = self.client.get(self.root + "account/")
        self.assertEqual(account.status_code, 200)
        self.assertTrue(account.data["onboarding"]["required"])
        self.assertEqual(self.client.get("/api/v1/my-startup/auth/me/").status_code, 403)
        result = self.client.put(self.root + "account/onboarding/", {"step": "join", "first_name": "Priya",
            "last_name": "Shah", "adult_and_terms": True, "policy_version": settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION,
            "intent": "startup"}, format="json")
        self.assertEqual(result.status_code, 200, getattr(result, "data", result.content))
        self.assertFalse(result.data["onboarding"]["required"])
        self.assertEqual(result.data["next"], "my_startup")
        self.assertEqual(set(CommunityMemberConsent.objects.filter(user=user).values_list("purpose", flat=True)),
                         {"adult_eligibility", "code_of_conduct", "terms"})
        self.assertEqual(self.client.get("/api/v1/my-startup/auth/me/").status_code, 200)
        for name in ("__Host-mlai_founder_access", "__Host-mlai_founder_refresh"):
            cookie = response.cookies[name]
            self.assertEqual((cookie["domain"], cookie["path"]), ("", "/"))
            self.assertTrue(cookie["secure"])
            self.assertTrue(cookie["httponly"])
        self.assertNotIn("mlai_chat_access", response.cookies)

    def test_foreign_request_and_verify_do_not_change_existing_challenges_or_accounts(self):
        requested = self.client.post(self.root + "auth/email-code/request/", self.body, format="json")
        self.assertEqual(requested.status_code, 202)
        before = list(CommunityChatEmailCodeChallenge.objects.values())
        count = CommunityChatEmailCodeDelivery.objects.count()
        for body in ({**deepcopy(self.body), "email": "sam@mlai.au"},
                     {**deepcopy(self.body), "client_id": "mlai-chat-ios"}):
            self.assertEqual(self.client.post(self.root + "auth/email-code/request/", body, format="json").status_code, 401)
        result = self.client.post(self.root + "auth/email-code/verify/", {"client_id": "mlai-chat-ios",
            "installation_id": self.installation, "challenge_id": requested.data["challenge_id"], "code": "000000"}, format="json")
        self.assertEqual(result.status_code, 401)
        self.assertEqual(list(CommunityChatEmailCodeChallenge.objects.values()), before)
        self.assertEqual(CommunityChatEmailCodeDelivery.objects.count(), count)
        self.assertFalse(get_user_model().objects.filter(email=self.email).exists())

    def test_wrong_key_cross_origin_revoked_and_production_cookies_cannot_read_account(self):
        self.sign_in()
        from community_chat.models import CommunityChatAccountSession
        session = CommunityChatAccountSession.objects.get(user__email=self.email)
        last_used = session.last_used_at
        self.client.credentials(HTTP_ORIGIN=self.origin, HTTP_X_MLAI_PUBLIC_KEY=public_key(342))
        self.assertEqual(self.client.get(self.root + "account/").status_code, 401)
        session.refresh_from_db()
        self.assertEqual(session.last_used_at, last_used)
        self.client.credentials(HTTP_ORIGIN="https://chat.mlai.au", HTTP_X_MLAI_PUBLIC_KEY=self.key)
        self.assertEqual(self.client.get(self.root + "account/").status_code, 401)
        session.refresh_from_db()
        self.assertEqual(session.last_used_at, last_used)
        self.client.credentials(HTTP_ORIGIN=self.origin, HTTP_X_MLAI_PUBLIC_KEY=self.key)
        CommunityChatAccountSession.objects.filter(pk=session.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.root + "account/").status_code, 401)
        raw = self.client.cookies["__Host-mlai_founder_access"].value
        self.client.cookies.clear()
        self.client.cookies["mlai_chat_access"] = raw
        self.client.cookies["access_token"] = raw
        self.assertEqual(self.client.get(self.root + "account/").status_code, 401)

    def test_stored_foreign_challenges_are_refused_before_throttling_or_attempt_changes(self):
        response = self.client.post(self.root + "auth/email-code/request/", self.body, format="json")
        self.assertEqual(response.status_code, 202)
        pk = response.data["challenge_id"]
        row = CommunityChatEmailCodeChallenge.objects.get(pk=pk)
        original = {key: getattr(row, key) for key in ("origin", "platform", "client_id", "encrypted_signup_email")}
        from community_chat.email_delivery import encrypt_signup_email
        for changes in ({"origin": "https://chat-staging.mlai.au"}, {"platform": "ios"},
                        {"client_id": "mlai-chat-ios"}, {"encrypted_signup_email": encrypt_signup_email("foreign@example.test")}):
            CommunityChatEmailCodeChallenge.objects.filter(pk=pk).update(**{**original, **changes})
            before = list(CommunityChatEmailCodeChallenge.objects.values())
            deliveries = list(CommunityChatEmailCodeDelivery.objects.values())
            with patch("community_chat.views.enforce_chat_email_code_verify_limits", side_effect=AssertionError("Foreign challenge reached throttle")):
                result = self.client.post(self.root + "auth/email-code/verify/", {"client_id": "mlai-chat-web",
                    "installation_id": self.installation, "challenge_id": pk, "code": "000000"}, format="json")
            # These normal anonymous APIViews have no authenticate_header;
            # DRF renders AuthenticationFailed as403 before throttle writes.
            self.assertEqual(result.status_code, 403)
            self.assertEqual(list(CommunityChatEmailCodeChallenge.objects.values()), before)
            self.assertEqual(list(CommunityChatEmailCodeDelivery.objects.values()), deliveries)

    def test_reserved_alias_to_foreign_primary_cannot_issue_or_consume(self):
        response = self.client.post(self.root + "auth/email-code/request/", self.body, format="json")
        self.assertEqual(response.status_code, 202)
        from core.models import UserEmailAlias
        foreign = get_user_model().objects.create_user(email="foreign-primary@example.test")
        UserEmailAlias.objects.create(user=foreign, email=self.email)
        before = list(CommunityChatEmailCodeChallenge.objects.values())
        with patch("community_chat.views.enforce_chat_email_code_request_limits", side_effect=AssertionError("Alias reached request throttle")), \
             patch("community_chat.views.enforce_chat_email_code_verify_limits", side_effect=AssertionError("Alias reached verify throttle")):
            self.assertEqual(self.client.post(self.root + "auth/email-code/request/", self.body, format="json").status_code, 403)
            self.assertEqual(self.client.post(self.root + "auth/email-code/verify/", {"client_id": "mlai-chat-web",
                "installation_id": self.installation, "challenge_id": response.data["challenge_id"], "code": "000000"}, format="json").status_code, 403)
        self.assertEqual(list(CommunityChatEmailCodeChallenge.objects.values()), before)
        self.assertFalse(get_user_model().objects.filter(email=self.email).exists())

    def test_stored_native_refresh_credentials_cannot_rotate_revoke_or_touch_last_used(self):
        self.sign_in()
        from community_chat.models import CommunityChatAccountSession
        session = CommunityChatAccountSession.objects.get(user__email=self.email)
        original = {key: getattr(session, key) for key in ("origin", "client_id", "platform", "user_id")}
        foreign = get_user_model().objects.create_user(email="foreign-refresh@example.test")
        refresh = self.client.cookies["__Host-mlai_founder_refresh"].value
        for changes in ({"platform": "ios"}, {"client_id": "mlai-chat-ios"},
                        {"origin": "https://chat-staging.mlai.au"}, {"user_id": foreign.pk}):
            CommunityChatAccountSession.objects.filter(pk=session.pk).update(**{**original, **changes})
            before = CommunityChatAccountSession.objects.values().get(pk=session.pk)
            for route in ("auth/session/refresh/", "auth/session/logout/"):
                self.client.cookies["__Host-mlai_founder_refresh"] = refresh
                self.assertEqual(self.client.post(self.root + route, {}, format="json").status_code, 401)
                self.assertEqual(CommunityChatAccountSession.objects.values().get(pk=session.pk), before)
            self.assertEqual(self.client.get(self.root + "account/").status_code, 401)
            self.assertEqual(CommunityChatAccountSession.objects.values().get(pk=session.pk), before)

    def test_consume_rechecks_namespace_under_the_original_challenge_lock(self):
        requested = self.client.post(self.root + "auth/email-code/request/", self.body, format="json")
        self.assertEqual(requested.status_code, 202)
        challenge = CommunityChatEmailCodeChallenge.objects.get(pk=requested.data["challenge_id"])
        CommunityChatEmailCodeChallenge.objects.filter(pk=challenge.pk).update(origin="https://chat-staging.mlai.au")
        before = CommunityChatEmailCodeChallenge.objects.values().get(pk=challenge.pk)
        from community_chat.email_codes import consume_email_code, InvalidEmailCode
        with self.assertRaises(InvalidEmailCode):
            consume_email_code(challenge_id=challenge.pk, code="000000", client_id="mlai-chat-web", installation_id=challenge.installation_id)
        self.assertEqual(CommunityChatEmailCodeChallenge.objects.values().get(pk=challenge.pk), before)

    def test_foreign_primary_bootstrap_is_refused_before_normal_membership_or_enrollment(self):
        import hashlib
        from datetime import timedelta
        from community_chat.models import CommunityChatBootstrapToken
        foreign = get_user_model().objects.create_user(email="foreign-bootstrap@example.test")
        raw = "mlai_chat_" + "a" * 64
        token = CommunityChatBootstrapToken.objects.create(user=foreign,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(), public_key=self.key,
            client_id="mlai-chat-web", origin=self.origin, platform="web", expires_at=timezone.now() + timedelta(minutes=2))
        before = CommunityChatBootstrapToken.objects.values().get(pk=token.pk)
        self.client.credentials(HTTP_ORIGIN=self.origin, HTTP_AUTHORIZATION="Bearer " + raw)
        with patch("community_chat.onboarding.require_community_access", side_effect=AssertionError("Foreign primary reached membership")):
            self.assertEqual(self.client.get(self.root + "session/").status_code, 401)
        self.assertEqual(CommunityChatBootstrapToken.objects.values().get(pk=token.pk), before)
