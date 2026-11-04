"""Exercise Slack catalog capability preflights without DB or endpoint I/O."""

from unittest.mock import Mock

from corsheaders.middleware import CorsMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings

from core.middleware import DesktopAuthCorsMiddleware


@override_settings(
    CORS_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    CORS_ALLOWED_ORIGIN_REGEXES=[],
    CORS_ALLOW_ALL_ORIGINS=False,
)
class SlackCatalogCorsTests(SimpleTestCase):
    def preflight(
        self,
        origin,
        *,
        path="/api/v1/community-chat/slack/",
        headers="authorization, content-type, x-mlai-slack-catalog-kinds",
    ):
        endpoint = Mock(side_effect=AssertionError("Preflight must not call the endpoint"))
        middleware = DesktopAuthCorsMiddleware(CorsMiddleware(endpoint))
        request = RequestFactory().options(
            path,
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
        )
        response = middleware(request)
        endpoint.assert_not_called()
        return response

    def test_allowed_browser_origin_can_send_catalog_capability(self):
        response = self.preflight("https://chat.mlai.au")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://chat.mlai.au")
        allowed_headers = {
            value.strip().lower()
            for value in response["Access-Control-Allow-Headers"].split(",")
        }
        self.assertTrue(
            {"authorization", "content-type", "x-mlai-slack-catalog-kinds"}.issubset(
                allowed_headers
            )
        )
        self.assertIn("GET", response["Access-Control-Allow-Methods"].split(", "))
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    def test_unrelated_browser_origin_cannot_send_catalog_capability(self):
        response = self.preflight("https://untrusted.example.invalid")

        self.assertNotIn("Access-Control-Allow-Origin", response)
        self.assertNotIn("Access-Control-Allow-Headers", response)
        self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_desktop_origins_accept_either_header_casing_on_slack_routes(self):
        for origin in ("tauri://localhost", "http://tauri.localhost"):
            for path in (
                "/api/v1/community-chat/slack/",
                "/api/v1/community-chat/slack/conversations/",
            ):
                for headers in (
                    "authorization, content-type, x-mlai-slack-catalog-kinds",
                    "Authorization, Content-Type, X-MLAI-Slack-Catalog-Kinds",
                ):
                    with self.subTest(origin=origin, path=path, headers=headers):
                        response = self.preflight(origin, path=path, headers=headers)
                        self.assertEqual(response.status_code, 204)
                        self.assertEqual(response["Access-Control-Allow-Origin"], origin)
                        self.assertIn(
                            "x-mlai-slack-catalog-kinds",
                            response["Access-Control-Allow-Headers"].split(", "),
                        )
                        self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_desktop_capability_is_not_allowed_on_unrelated_account_routes(self):
        for path in (
            "/api/v1/community-chat/account/",
            "/api/v1/my-startup/auth/me/",
        ):
            with self.subTest(path=path):
                response = self.preflight("tauri://localhost", path=path)
                self.assertNotIn("Access-Control-Allow-Origin", response)
                self.assertNotIn("Access-Control-Allow-Headers", response)
                self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_desktop_lookalike_origins_are_not_admitted(self):
        for origin in (
            "tauri://localhost.evil.test",
            "http://tauri.localhost.evil.test",
        ):
            with self.subTest(origin=origin):
                response = self.preflight(origin)
                self.assertNotIn("Access-Control-Allow-Origin", response)
                self.assertNotIn("Access-Control-Allow-Headers", response)
                self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_desktop_unknown_request_headers_remain_rejected(self):
        response = self.preflight(
            "tauri://localhost",
            headers="authorization, x-mlai-slack-catalog-kinds, x-untrusted-header",
        )

        self.assertNotIn("Access-Control-Allow-Origin", response)
        self.assertNotIn("Access-Control-Allow-Headers", response)
        self.assertNotIn("Access-Control-Allow-Credentials", response)
