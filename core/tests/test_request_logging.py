from django.test import RequestFactory, SimpleTestCase

from core.middleware import safe_request_path


class SafeRequestPathTests(SimpleTestCase):
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
