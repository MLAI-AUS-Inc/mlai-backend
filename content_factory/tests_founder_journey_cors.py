"""C1-1: the journey poll's If-None-Match passes the CORS preflight and its ETag is readable cross-origin."""
from django.conf import settings
from django.test import SimpleTestCase, override_settings

ORIGIN = "https://chat.mlai.au"


@override_settings(CORS_ALLOWED_ORIGINS=[ORIGIN])
class FounderJourneyCorsTests(SimpleTestCase):
    def preflight(self, headers):
        return self.client.options("/healthz/live", HTTP_ORIGIN=ORIGIN, HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
                                   HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers)

    def test_if_none_match_preflight_is_allowed(self):
        response = self.preflight("if-none-match")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], ORIGIN)
        allowed = {value.strip().lower() for value in response["Access-Control-Allow-Headers"].split(",")}
        self.assertIn("if-none-match", allowed)

    def test_existing_allowed_headers_are_still_allowed(self):
        response = self.preflight("authorization, content-type, x-csrftoken, x-request-id, idempotency-key, "
                                  "x-mlai-slack-catalog-kinds, if-none-match")
        allowed = {value.strip().lower() for value in response["Access-Control-Allow-Headers"].split(",")}
        for header in ("authorization", "content-type", "x-csrftoken", "x-request-id", "idempotency-key",
                       "x-mlai-slack-catalog-kinds", "if-none-match"):
            self.assertIn(header, allowed)

    def test_get_exposes_etag(self):
        response = self.client.get("/healthz/live", HTTP_ORIGIN=ORIGIN)
        self.assertEqual(response["Access-Control-Allow-Origin"], ORIGIN)
        exposed = {value.strip().lower() for value in response["Access-Control-Expose-Headers"].split(",")}
        self.assertEqual(exposed, {"x-request-id", "retry-after", "etag"})

    def test_settings_entries(self):
        self.assertIn("if-none-match", settings.CORS_ALLOW_HEADERS)
        self.assertIn("ETag", settings.CORS_EXPOSE_HEADERS)
