from django.test import RequestFactory, SimpleTestCase

from core.middleware import safe_request_path


class SafeRequestPathTests(SimpleTestCase):
    def test_connect_ticket_is_redacted_in_both_actual_request_log_records(self):
        from django.http import HttpResponse
        from core.middleware import RequestLoggingMiddleware
        request=RequestFactory().get("/integrations/connect/google",{
            "TiCkEt":"synthetic-connect-ticket","scope":"website_baseline"})
        with self.assertLogs("core.middleware",level="INFO") as captured:
            RequestLoggingMiddleware(lambda request:HttpResponse(status=302))(request)
        self.assertEqual(len(captured.output),2)
        for record in captured.output:
            self.assertNotIn("synthetic-connect-ticket",record)
            self.assertIn("TiCkEt=%5BREDACTED%5D",record)
            self.assertIn("scope=website_baseline",record)

    def test_redacts_oauth_query_credentials_but_preserves_routing_context(self):
        request = RequestFactory().get(
            "/integrations/callback/google-drive",
            {
                "code": "secret-code",
                "state": "signed-state",
                "error": "access_denied",
            },
        )

        result = safe_request_path(request)

        self.assertIn("code=%5BREDACTED%5D", result)
        self.assertIn("state=%5BREDACTED%5D", result)
        self.assertIn("error=access_denied", result)
        self.assertNotIn("secret-code", result)
        self.assertNotIn("signed-state", result)
