"""Credential, URL and preview contract tests; no database access."""

from types import SimpleNamespace
from django.test import SimpleTestCase as TestCase
from unittest.mock import patch
from uuid import uuid4

from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import Resolver404, resolve
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from community_chat.account_sessions import InvalidAccountSession
from .api import MyStartupAuthentication, MyStartupViewMixin
from .connectors import MyStartupConnectorConnectView, safe_startup_return
from .links import rewrite_payload, rewrite_url

ORIGIN = "https://chat.mlai.test"


class EchoView(MyStartupViewMixin, APIView):
    def get(self, request):
        return Response(
            {"user": request.user.pk, "company": request.query_params.get("company_id")}
        )

    post = get


def session(user_id=2):
    return SimpleNamespace(
        user=SimpleNamespace(pk=user_id, is_authenticated=True, is_active=True),
        origin=ORIGIN,
        public_key="pubkey",
        installation_id="install",
        client_id="client",
        platform="browser",
        name="test",
    )


@override_settings(
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN], COMMUNITY_CHAT_FRONTEND_URL=ORIGIN
)
class StartupContractTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        admission = patch("community_chat.onboarding.require_community_access")
        self.admission = admission.start()
        self.addCleanup(admission.stop)

    def request(self, method="get", **kwargs):
        return getattr(self.factory, method)("/api/v1/my-startup/test/", **kwargs)

    @patch("community_chat.authentication.authenticate_access_token")
    def test_dual_cookie_uses_chat_account(self, authenticate):
        authenticate.return_value = session()
        request = self.request(
            HTTP_COOKIE="access_token=legacy-user-one; mlai_chat_access=mlai_session_access_chat-user-two"
        )
        response = EchoView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"], 2)
        authenticate.assert_called_once_with("mlai_session_access_chat-user-two")
        self.admission.assert_called_once_with(authenticate.return_value.user)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    @patch("community_chat.authentication.authenticate_access_token")
    def test_pending_member_cannot_enter_startup_facade(self, authenticate):
        from rest_framework.exceptions import PermissionDenied

        authenticate.return_value = session()
        self.admission.side_effect = PermissionDenied("onboarding_required")
        response = EchoView.as_view()(
            self.request(HTTP_COOKIE="mlai_chat_access=valid")
        )
        self.assertEqual(response.status_code, 403)
        self.admission.assert_called_once_with(authenticate.return_value.user)

    def test_legacy_cookie_alone_cannot_enter(self):
        self.assertEqual(
            EchoView.as_view()(
                self.request(HTTP_COOKIE="access_token=legacy")
            ).status_code,
            401,
        )

    @patch("community_chat.authentication.authenticate_access_token")
    def test_foreign_authorization_cannot_fall_back_to_chat_cookie(self, authenticate):
        for token in [
            "mlai_chat_bootstrap",
            "mlai_usage_report",
            "jwt-other-account",
            "Basic password",
        ]:
            response = EchoView.as_view()(
                self.request(
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                    HTTP_COOKIE="mlai_chat_access=valid",
                )
            )
            self.assertEqual(response.status_code, 401)
        authenticate.assert_not_called()

    @patch("community_chat.authentication.authenticate_access_token")
    def test_cookie_mutations_require_exact_session_origin(self, authenticate):
        authenticate.return_value = session()
        for origin, expected in [("", 401), ("https://evil.test", 401), (ORIGIN, 200)]:
            response = EchoView.as_view()(
                self.request(
                    "post", HTTP_COOKIE="mlai_chat_access=valid", HTTP_ORIGIN=origin
                )
            )
            self.assertEqual(response.status_code, expected)

    @patch("community_chat.authentication.authenticate_access_token")
    def test_expired_session_is_not_anonymous_fallback(self, authenticate):
        authenticate.side_effect = InvalidAccountSession()
        self.assertEqual(
            EchoView.as_view()(
                self.request(HTTP_COOKIE="mlai_chat_access=expired")
            ).status_code,
            401,
        )

    def test_facade_is_bounded_and_legacy_is_unchanged(self):
        good = [
            "vibe-marketing/bootstrap/",
            "vibe-marketing/editorial-catalog",
            "founder-tools/companies/",
            "points/me/balance/",
            "auth/me/",
            "users/slack-founder-link/complete/",
            "integrations/sources/status",
        ]
        for path in good:
            view = resolve("/api/v1/my-startup/" + path).func.view_class
            self.assertEqual(view.authentication_classes, (MyStartupAuthentication,))
        for path in [
            "vibe-marketing/admin/usage/",
            "points/admins/",
            "integrations/bridge/slack/events",
            "auth/login/",
        ]:
            with self.assertRaises(Resolver404):
                resolve("/api/v1/my-startup/" + path)
        self.assertNotEqual(
            resolve(
                "/api/v1/vibe-marketing/bootstrap/"
            ).func.view_class.authentication_classes,
            (MyStartupAuthentication,),
        )

    @patch("community_chat.authentication.authenticate_access_token")
    def test_preview_company_path_rejects_conflicting_query(self, authenticate):
        authenticate.return_value = session()
        company = uuid4()
        request = self.factory.get("/preview/?company_id=other")
        self.assertEqual(
            EchoView.as_view()(request, startup_company_id=company).status_code, 400
        )
        authenticate.assert_not_called()

    @patch("community_chat.authentication.authenticate_access_token")
    def test_preview_resources_keep_company_scope(self, authenticate):
        authenticate.return_value = session()
        company = str(uuid4())

        class Preview(MyStartupViewMixin, APIView):
            def get(self, request, run_id):
                return HttpResponse(
                    f'<script src="/api/v1/vibe-marketing/runs/{run_id}/live-preview/assets/app.js"></script>',
                    content_type="text/html",
                )

        response = Preview.as_view()(
            self.request(HTTP_COOKIE="mlai_chat_access=valid"),
            startup_company_id=company,
            run_id="existing-run",
        )
        self.assertIn(
            f"/api/v1/my-startup/companies/{company}/vibe-marketing/runs/existing-run/live-preview/assets/app.js".encode(),
            response.content,
        )

    def test_rewrite_changes_links_not_user_content(self):
        old = "https://mlai.au/founder-tools/marketing/runs/123?step=review"
        response = rewrite_payload(
            {
                "href": old,
                "description": old,
                "previewUrl": "https://untrusted.test/founder-tools/marketing",
            },
            company_id="company-a",
        )
        self.assertEqual(response["description"], old)
        self.assertEqual(
            response["previewUrl"], "https://untrusted.test/founder-tools/marketing"
        )
        self.assertEqual(
            response["href"],
            ORIGIN + "/my-startup/runs/123?step=review&company_id=company-a",
        )

    def test_nested_google_return_carries_startup(self):
        from urllib.parse import urlencode, parse_qs, urlsplit

        old = "https://api.mlai.au/integrations/connect/google?" + urlencode(
            {
                "ticket": "signed",
                "next": "https://mlai.au/founder-tools/marketing/create?step=baseline",
            }
        )
        query = parse_qs(urlsplit(rewrite_url(old, company_id="one")).query)
        self.assertEqual(query["ticket"], ["signed"])
        self.assertEqual(
            query["next"], [ORIGIN + "/my-startup/create?step=baseline&company_id=one"]
        )

    def test_oauth_returns_reject_lookalikes_and_escapes(self):
        for value in [
            "//evil.test",
            ORIGIN + "/my-startup-evil",
            ORIGIN + "/my-startup/../other",
            ORIGIN + "/my-startup/%2e%2e",
            ORIGIN + "/my-startup\\evil",
            "https://chat.mlai.test.evil/my-startup",
        ]:
            self.assertIsNone(safe_startup_return(value), value)
        self.assertEqual(
            safe_startup_return(ORIGIN + "/my-startup/create?company_id=one"),
            ORIGIN + "/my-startup/create?company_id=one",
        )

    @patch("founder_tools.my_startup.connectors.connector_connect")
    @patch("community_chat.authentication.authenticate_access_token")
    def test_oauth_start_uses_chat_user_even_with_legacy_cookie(
        self, authenticate, connect
    ):
        authenticate.return_value = session(22)
        connect.return_value = HttpResponse(
            status=302,
            headers={"Location": "https://slack.com/oauth/v2/authorize?state=opaque"},
        )
        request = self.factory.post(
            "/api/v1/my-startup/connectors/slack/connect/?company_id=owned",
            {"next": ORIGIN + "/my-startup/connections"},
            format="json",
            HTTP_ORIGIN=ORIGIN,
            HTTP_COOKIE="mlai_chat_access=valid; access_token=other-user",
        )
        response = MyStartupConnectorConnectView.as_view()(request, provider="slack")
        self.assertEqual(response.status_code, 200)
        raw, provider = connect.call_args.args
        self.assertEqual(raw.user.pk, 22)
        self.assertEqual(raw.GET["company_id"], "owned")
        self.assertEqual(provider, "slack")

    @patch("founder_tools.my_startup.handoff.company_for_handoff")
    def test_handoff_is_bound_to_account_and_redeemed_once(self, company_lookup):
        from django.core.cache import cache
        from .handoff import CreateStartupHandoffView, RedeemStartupHandoffView
        from rest_framework.test import force_authenticate
        from urllib.parse import urlsplit, parse_qs

        cache.clear()
        company_lookup.return_value = SimpleNamespace(pk=uuid4(), organization_id=1)
        user = session().user
        create = self.factory.post(
            "/api/v1/founder-tools/my-startup-handoff/",
            {
                "companyId": str(company_lookup.return_value.pk),
                "path": "/founder-tools/marketing/create?step=topics&researchRunId=existing-research&articleStep=review&company_id=other&next=https://evil.test",
                "research": {
                    "brief": {"subject": "Trees"},
                    "requestId": "existing-paid-request",
                    "selected": ["option-a"],
                    "step": 1,
                },
            },
            format="json",
        )
        force_authenticate(create, user=user)
        issued = CreateStartupHandoffView.as_view()(create)
        token = parse_qs(urlsplit(issued.data["url"]).query)["token"][0]

        def redeem(actor):
            request = self.factory.post(
                "/api/v1/my-startup/handoff/redeem/", {"token": token}, format="json"
            )
            force_authenticate(request, user=actor)
            return RedeemStartupHandoffView.as_view()(request)

        self.assertEqual(redeem(session(9).user).status_code, 404)
        result = redeem(user)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["research"]["requestId"], "existing-paid-request")
        self.assertEqual(result.data["research"]["selected"], ["option-a"])
        restored = parse_qs(urlsplit(result.data["path"]).query)
        self.assertEqual(restored["researchRunId"], ["existing-research"])
        self.assertEqual(restored["articleStep"], ["review"])
        self.assertEqual(restored["company_id"], [str(company_lookup.return_value.pk)])
        self.assertNotIn("next", restored)
        self.assertEqual(redeem(user).status_code, 404)

    @patch("founder_tools.my_startup.handoff.get_object_or_404")
    def test_handoff_validates_existing_run_ownership_and_workflow(self, get_object):
        from .handoff import validate_bundle
        from workflow_runs.models import ContentFactoryRun

        validate_bundle(
            {"runId": "research-running", "brief": {}, "step": 2},
            SimpleNamespace(organization_id=17),
        )
        get_object.assert_called_once_with(
            ContentFactoryRun,
            run_id="research-running",
            organization_id=17,
            workflow="island_refresh",
            run_request__island_research_brief__isnull=False,
        )

    def test_roo_link_capture_is_origin_checked_and_httponly(self):
        from .roo_link import CaptureRooLinkView, COOKIE

        token = "a" * 43
        for origin, expected in [("https://evil.test", 403), (ORIGIN, 200)]:
            response = CaptureRooLinkView.as_view()(
                self.factory.post(
                    "/api/v1/my-startup/roo-link/capture/",
                    {"token": token},
                    format="json",
                    HTTP_ORIGIN=origin,
                )
            )
            self.assertEqual(response.status_code, expected)
            if expected == 200:
                self.assertTrue(response.cookies[COOKIE]["httponly"])
                self.assertEqual(
                    response.cookies[COOKIE]["path"], "/api/v1/my-startup/roo-link/"
                )
                self.assertNotIn(token, str(response.data))

    @patch("founder_tools.my_startup.purchases.get_object_or_404")
    @patch(
        "founder_tools.my_startup.purchases.PointsPurchaseViewSet._response_data",
        return_value={"id": "owned"},
    )
    def test_purchase_review_checks_ownership(self, serialize, get_object):
        from rest_framework.test import force_authenticate
        from .purchases import StartupPurchaseView
        from roo.models import PointsPurchase

        user = session().user
        purchase_id = uuid4()
        request = self.factory.get("/api/v1/my-startup/points/purchases/owned/")
        force_authenticate(request, user=user)
        self.assertEqual(
            StartupPurchaseView.as_view()(request, purchase_id=purchase_id).status_code,
            200,
        )
        get_object.assert_called_once_with(PointsPurchase, pk=purchase_id, user=user)

    @override_settings(DEFAULT_FRONTEND_URL="https://mlai.test")
    def test_checkout_return_preserves_selected_frontend(self):
        from roo.services import PointsPurchaseService

        purchase = SimpleNamespace(
            id="existing-purchase", purchase_from={"surface": "my-startup"}
        )
        self.assertEqual(
            PointsPurchaseService.stripe_return_url(purchase, "success"),
            ORIGIN + "/my-startup/credits/existing-purchase?checkout=success",
        )
        purchase.purchase_from = {"surface": "founder-tools-upgrades"}
        self.assertEqual(
            PointsPurchaseService.frontend_checkout_page_url(purchase),
            "https://mlai.test/roo/topup/existing-purchase",
        )

    @patch("founder_tools.my_startup.connectors.disconnect_external_connection")
    @patch(
        "founder_tools.my_startup.connectors.ExternalServiceConnection.objects.filter"
    )
    @patch("founder_tools.my_startup.connectors._org_scope_or_response")
    @patch("community_chat.authentication.authenticate_access_token")
    def test_disconnect_requires_owned_selected_company_and_marketing_provider(
        self, authenticate, scope, connections, disconnect
    ):
        from .connectors import MyStartupConnectorDisconnectView

        account = session()
        authenticate.return_value = account
        organization = SimpleNamespace(pk=44)
        scope.return_value = (organization, None)
        connections.return_value.exists.return_value = False

        def request(company="owned"):
            return self.factory.delete(
                f"/api/v1/my-startup/integrations/sources/connections/12?company_id={company}",
                HTTP_ORIGIN=ORIGIN,
                HTTP_COOKIE="mlai_chat_access=valid",
            )

        view = MyStartupConnectorDisconnectView.as_view()
        self.assertEqual(view(request(), connection_id=12).status_code, 404)
        disconnect.assert_not_called()
        connections.assert_called_with(
            pk=12,
            user=account.user,
            organization=organization,
            provider__in=("google_analytics", "slack"),
        )
        connections.return_value.exists.return_value = True
        disconnect.return_value = True
        self.assertEqual(view(request(), connection_id=12).status_code, 200)
        disconnect.assert_called_once_with(account.user, 12)
        self.assertEqual(view(request(""), connection_id=12).status_code, 400)

    def test_handoff_rejects_malformed_company_without_database_access(self):
        from rest_framework.exceptions import ValidationError
        from .handoff import company_for_handoff

        for company in [None, "", "not-a-uuid", [], {}]:
            with self.assertRaises(ValidationError):
                company_for_handoff(session().user, company)

    def test_delivery_link_cutover_is_independent_and_reversible(self):
        from .links import marketing_delivery_url

        old = "https://mlai.au/founder-tools/marketing/runs/existing-run?articleStep=review"
        with override_settings(MY_STARTUP_DELIVERY_LINKS_ENABLED=False):
            self.assertEqual(marketing_delivery_url(old), old)
        with override_settings(MY_STARTUP_DELIVERY_LINKS_ENABLED=True):
            self.assertEqual(
                marketing_delivery_url(old, company_id="owned"),
                ORIGIN
                + "/my-startup/runs/existing-run?articleStep=review&company_id=owned",
            )
            self.assertEqual(
                marketing_delivery_url("https://example.test/article"),
                "https://example.test/article",
            )
