from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


FAKE_PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"


class CheckGitHubAppCredentialsCommandTests(SimpleTestCase):
    def test_command_fails_when_credentials_are_missing(self):
        with override_settings(GITHUB_APP_ID="", GITHUB_APP_PRIVATE_KEY=""):
            with self.assertRaises(CommandError) as raised:
                call_command("check_github_app_credentials", stdout=StringIO(), stderr=StringIO())

        self.assertIn("GITHUB_APP_ID", str(raised.exception))
        self.assertIn("GITHUB_APP_PRIVATE_KEY", str(raised.exception))

    def test_command_builds_jwt_without_printing_secret(self):
        out = StringIO()
        with override_settings(GITHUB_APP_ID="12345", GITHUB_APP_PRIVATE_KEY=FAKE_PRIVATE_KEY), patch(
            "integrations.services.github_app._github_app_jwt",
            return_value="jwt-token",
        ) as mock_jwt:
            call_command("check_github_app_credentials", stdout=out)

        mock_jwt.assert_called_once_with()
        output = out.getvalue()
        self.assertIn("GitHub App credentials can sign a JWT.", output)
        self.assertNotIn("abc", output)
        self.assertNotIn("jwt-token", output)

    def test_command_reports_malformed_private_key(self):
        with override_settings(GITHUB_APP_ID="12345", GITHUB_APP_PRIVATE_KEY="not-a-pem"):
            with self.assertRaises(CommandError) as raised:
                call_command("check_github_app_credentials", stdout=StringIO(), stderr=StringIO())

        self.assertIn("does not look like a PEM private key", str(raised.exception))

    def test_validate_github_calls_app_endpoint(self):
        out = StringIO()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"slug": "mlai-tools"}

        with override_settings(GITHUB_APP_ID="12345", GITHUB_APP_PRIVATE_KEY=FAKE_PRIVATE_KEY), patch(
            "integrations.services.github_app._github_app_jwt",
            return_value="jwt-token",
        ), patch("integrations.management.commands.check_github_app_credentials.http_requests.get", return_value=response) as mock_get:
            call_command("check_github_app_credentials", "--validate-github", stdout=out)

        mock_get.assert_called_once()
        self.assertIn("GitHub App API validation succeeded for mlai-tools.", out.getvalue())
