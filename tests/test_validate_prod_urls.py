from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from core.management.commands.validate_prod_urls import validate_prod_url_settings


VALID_PROD_URL_SETTINGS = {
    "APP_ENV": "production",
    "DEBUG": False,
    "DEFAULT_BACKEND_URL": "https://api.mlai.au",
    "DEFAULT_FRONTEND_URL": "https://mlai.au",
    "MEDHACK_URL": "https://mlai.au",
    "ESAFETY_URL": "https://mlai.au",
    "INNOVATE_CONNECT_ALLIANCE_URL": "https://mlai.au",
    "VIBE_RAISING_URL": "https://mlai.au",
    "FOUNDER_TOOLS_URL": "https://mlai.au",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/google",
    "GITHUB_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/github",
    "STRIPE_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/stripe",
    "XERO_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/xero",
    "NOTION_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/notion",
    "GOOGLE_DRIVE_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/google-drive",
    "SLACK_OAUTH_REDIRECT_URI": "https://api.mlai.au/integrations/callback/slack",
    "CONTENT_FACTORY_URL": "http://content-factory-web:8000",
    "VALLEY_HARNESS_URL": "http://valley-api:8080",
    "CORS_ALLOWED_ORIGINS": ["https://mlai.au", "https://www.mlai.au"],
    "CSRF_TRUSTED_ORIGINS": ["https://mlai.au", "https://www.mlai.au", "https://api.mlai.au"],
}


class ValidateProdUrlsTests(SimpleTestCase):
    def _validation_errors(self, **overrides):
        settings = {**VALID_PROD_URL_SETTINGS, **overrides}
        with override_settings(**settings):
            return validate_prod_url_settings()

    def test_valid_prod_urls_allow_http_internal_service_hosts(self):
        self.assertEqual(self._validation_errors(), [])

    def test_public_url_must_use_https_in_production(self):
        errors = self._validation_errors(DEFAULT_BACKEND_URL="http://api.mlai.au")

        self.assertIn("DEFAULT_BACKEND_URL must use https in production.", errors)

    def test_oauth_redirect_uri_must_use_https_in_production(self):
        errors = self._validation_errors(
            GOOGLE_OAUTH_REDIRECT_URI="http://api.mlai.au/integrations/callback/google"
        )

        self.assertIn("GOOGLE_OAUTH_REDIRECT_URI must use https in production.", errors)

    def test_https_raw_ip_service_url_is_rejected(self):
        errors = self._validation_errors(CONTENT_FACTORY_URL="https://209.38.83.23")

        self.assertTrue(
            any(error.startswith("CONTENT_FACTORY_URL uses https with a raw IP address.") for error in errors)
        )

    def test_required_cors_and_csrf_origins_are_enforced(self):
        errors = self._validation_errors(
            CORS_ALLOWED_ORIGINS=["https://mlai.au"],
            CSRF_TRUSTED_ORIGINS=["https://mlai.au", "https://www.mlai.au"],
        )

        self.assertIn("CORS_ALLOWED_ORIGINS is missing required origin(s): https://www.mlai.au.", errors)
        self.assertIn("CSRF_TRUSTED_ORIGINS is missing required origin(s): https://api.mlai.au.", errors)

    def test_command_succeeds_for_valid_prod_urls(self):
        out = StringIO()
        with override_settings(**VALID_PROD_URL_SETTINGS):
            call_command("validate_prod_urls", stdout=out)

        self.assertIn("Production URL settings are valid.", out.getvalue())

    def test_command_fails_for_invalid_prod_urls(self):
        settings = {**VALID_PROD_URL_SETTINGS, "VALLEY_HARNESS_URL": "https://170.64.200.232:8080"}
        with override_settings(**settings):
            with self.assertRaises(CommandError):
                call_command("validate_prod_urls", stdout=StringIO(), stderr=StringIO())
