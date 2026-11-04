"""Actual middleware/normal view refusal controls; no database is permitted."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed

from community_chat.tests.founder_staging_fixtures import SETTINGS
from .founder_staging_admission import CHAT, FounderAdmissionMiddleware, require_browser_credential

FRONTEND = "https://founder-staging.mlai.au"
KEY = "a" * 64


@override_settings(**SETTINGS)
class FounderAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = FounderAdmissionMiddleware(lambda request: None)

    def call(self, cls, *, method="POST", body=None, module=CHAT, origin=FRONTEND, key=KEY, authorization=None):
        view = SimpleNamespace(view_class=type(cls, (), {"__module__": module}))
        headers = {"HTTP_ORIGIN": origin}
        if key is not None:
            headers["HTTP_X_MLAI_PUBLIC_KEY"] = key
        if authorization:
            headers["HTTP_AUTHORIZATION"] = authorization
        import json
        request = self.factory.generic(method, "/api/", data=json.dumps(body or {}),
                                       content_type="application/json", **headers)
        return self.middleware.process_view(request, view, (), {})

    def email_body(self):
        return {"email": f"founder-acceptance-{uuid4()}@{SETTINGS['FOUNDER_STAGING_EMAIL_DOMAIN']}",
                "client_id": "mlai-chat-web", "device": {"public_key": KEY, "platform": "web",
                "installation_id": str(uuid4())}, "onboarding_version": 1}

    def test_real_browser_anonymous_email_shapes_do_not_require_account_headers(self):
        self.assertIsNone(self.call("EmailCodeRequestView", body=self.email_body(), key=None))
        self.assertIsNone(self.call("EmailCodeVerifyView", key=None, body={"client_id": "mlai-chat-web",
            "installation_id": str(uuid4()), "challenge_id": str(uuid4()), "code": "123456"}))

    def test_foreign_email_native_identity_and_mixed_key_are_refused_before_the_view(self):
        original = self.email_body()
        for change in ({"email": "sam@mlai.au"}, {"email": original["email"].upper()},
                       {"client_id": "mlai-chat-ios"}, {"device": {**original["device"], "platform": "ios"}},
                       {"device": {**original["device"], "installation_id": "not-an-installation"}}):
            body = {**deepcopy(original), **change}
            self.assertEqual(self.call("EmailCodeRequestView", body=body).status_code, 401)
        self.assertEqual(self.call("EmailCodeRequestView", body=original, key="b" * 64).status_code, 401)
        self.assertEqual(self.call("EmailCodeRequestView", body=original, origin="https://chat.mlai.au").status_code, 401)

    def test_verify_cannot_authorize_a_desktop_request_or_wrong_client(self):
        body = {"client_id": "mlai-chat-web", "installation_id": str(uuid4()), "challenge_id": str(uuid4()), "code": "123456"}
        for change in ({"device_request_id": str(uuid4())}, {"client_id": "mlai-chat-desktop"},
                       {"installation_id": "wrong"}, {"challenge_id": "wrong"}):
            self.assertEqual(self.call("EmailCodeVerifyView", key=None, body={**body, **change}).status_code, 401)

    def test_legacy_login_native_admin_and_unreviewed_module_collisions_stay_closed(self):
        for module, name in ((CHAT, "PasswordAuthView"), (CHAT, "DeviceAuthStartView"),
                             ("core.views", "SendMagicLinkView"), ("core.views", "CreateUserView"),
                             ("other.module", "EmailCodeRequestView"),
                             ("founder_tools.startup_team_views", "TeamView")):
            self.assertEqual(self.call(name, module=module).status_code, 404)

    def test_refresh_logout_accept_only_bound_cookie_requests(self):
        for name in ("AccountSessionRefreshView", "AccountSessionLogoutView"):
            self.assertIsNone(self.call(name))
            self.assertEqual(self.call(name, body={"refresh_token": "native"}).status_code, 401)
            self.assertEqual(self.call(name, authorization="Bearer mlai_session_access_native").status_code, 401)
            self.assertEqual(self.call(name, key=None).status_code, 401)

    def test_pending_onboarding_keeps_normal_view_and_does_not_gain_domain_access(self):
        from community_chat.onboarding_views import MemberOnboardingView
        from community_chat.authentication import CommunityChatOnboardingAuthentication
        self.assertIn(CommunityChatOnboardingAuthentication, MemberOnboardingView.authentication_classes)
        self.assertIsNone(self.call("MemberOnboardingView", method="PUT", module="community_chat.onboarding_views"))
        credential = SimpleNamespace(client_id="mlai-chat-web", origin=FRONTEND, platform="web",
            user=SimpleNamespace(email="founder-acceptance-7ca45224-cfeb-4b82-9f0b-d712f5985520@" + SETTINGS["FOUNDER_STAGING_EMAIL_DOMAIN"]))
        request = self.factory.put("/api/", HTTP_ORIGIN=FRONTEND)
        # No eligible-only pilot guard or database authority shortcut is invoked.
        require_browser_credential(request, credential)
        credential.platform = "ios"
        with self.assertRaises(AuthenticationFailed):
            require_browser_credential(request, credential)

    def test_bootstrap_only_bearer_and_normal_copy_download_routes(self):
        self.assertIsNone(self.call("ChallengeView", key=None, authorization="Bearer mlai_chat_bootstrap"))
        self.assertEqual(self.call("AccountView", method="GET", key=None, authorization="Bearer mlai_chat_bootstrap").status_code, 401)
        self.assertIsNone(self.call("MyStartupFounderDraftCopiedView", module="founder_tools.my_startup.api"))
        self.assertIsNone(self.call("MyStartupFounderDraftDownloadView", method="GET", module="founder_tools.my_startup.api"))

    def test_only_exact_signed_preview_proxy_is_exempt_from_cookie_origin_headers(self):
        module = "content_factory.article_preview_lease"
        for method in ("GET", "HEAD"):
            self.assertIsNone(self.call("ArticlePreviewLeaseProxyView", method=method, module=module, origin="", key=None))
        self.assertEqual(self.call("ArticlePreviewLeaseProxyView", module=module, origin="", key=None).status_code, 401)
        self.assertEqual(self.call("ArticlePreviewLeaseProxyView", method="GET", module="other.module", origin="", key=None).status_code, 404)

    def test_general_api_never_falls_back_to_production_jwt(self):
        from core.authentication import CustomJWTAuthentication
        request = SimpleNamespace(headers={"Authorization": "Bearer native-jwt"}, COOKIES={})
        with patch("community_chat.authentication.CommunityChatAccountAuthentication.authenticate", return_value=None) as account, \
             patch("rest_framework_simplejwt.authentication.JWTAuthentication.authenticate", side_effect=AssertionError("JWT fallback forbidden")):
            self.assertIsNone(CustomJWTAuthentication().authenticate(request))
        account.assert_called_once_with(request)

    def test_production_and_existing_password_profile_are_unaffected(self):
        for profile in ("mlai.settings", "mlai.settings_staging_chat"):
            with override_settings(SETTINGS_MODULE=profile):
                self.assertIsNone(self.call("CreateUserView", module="core.views", origin="", key=None))

    def test_invalid_json_is_rejected_before_normal_email_throttles_or_challenge_writes(self):
        from community_chat.views import EmailCodeRequestView
        request = self.factory.post("/api/", data="{invalid", content_type="application/json", HTTP_ORIGIN=FRONTEND)
        self.assertEqual(self.middleware.process_view(request, EmailCodeRequestView.as_view(), (), {}).status_code, 401)
