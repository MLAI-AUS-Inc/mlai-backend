"""Tests for Sentry credential scrubbing and fail-open initialisation.

The scrubbing path is security-critical: events are shipped to a third party, so
a leak here exports live credentials off-platform.
"""

import logging
from unittest import mock

from django.test import SimpleTestCase

from core.middleware import SENSITIVE_QUERY_PARAMETERS
from core.observability import _before_send, _env_float, init_sentry


class BeforeSendScrubbingTests(SimpleTestCase):
    def test_redacts_sensitive_query_string_params(self):
        event = {
            "request": {
                "query_string": "code=live_auth_code&state=abc&next=/dashboard",
            }
        }
        scrubbed = _before_send(event, {})
        query = scrubbed["request"]["query_string"]

        self.assertNotIn("live_auth_code", query)
        self.assertIn("code=%5BREDACTED%5D", query)
        self.assertIn("state=%5BREDACTED%5D", query)
        # Non-sensitive params must survive, or the event loses its diagnostic value.
        self.assertIn("next=%2Fdashboard", query)

    def test_redacts_credentials_in_full_url_but_keeps_path(self):
        event = {
            "request": {
                "url": "https://api.mlai.au/api/v1/auth/verify?token=live_magic_token&app=hospital",
            }
        }
        scrubbed = _before_send(event, {})
        url = scrubbed["request"]["url"]

        self.assertNotIn("live_magic_token", url)
        self.assertIn("/api/v1/auth/verify", url)
        self.assertIn("app=hospital", url)

    def test_url_without_query_is_untouched(self):
        event = {"request": {"url": "https://api.mlai.au/api/v1/auth/me/"}}
        scrubbed = _before_send(event, {})
        self.assertEqual(scrubbed["request"]["url"], "https://api.mlai.au/api/v1/auth/me/")

    def test_redacts_credential_headers_case_insensitively(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer live.jwt.value",
                    "COOKIE": "access_token=live",
                    "X-Api-Key": "live-key",
                    "User-Agent": "curl/8.0",
                }
            }
        }
        headers = _before_send(event, {})["request"]["headers"]

        self.assertEqual(headers["Authorization"], "[REDACTED]")
        self.assertEqual(headers["COOKIE"], "[REDACTED]")
        self.assertEqual(headers["X-Api-Key"], "[REDACTED]")
        self.assertEqual(headers["User-Agent"], "curl/8.0")

    def test_drops_request_body_and_cookies_entirely(self):
        event = {
            "request": {
                "data": {"email": "a@b.com", "password": "hunter2"},
                "cookies": {"refresh_token": "live"},
                "url": "https://api.mlai.au/x",
            }
        }
        request = _before_send(event, {})["request"]

        self.assertNotIn("data", request)
        self.assertNotIn("cookies", request)

    def test_event_without_request_passes_through(self):
        event = {"message": "boom"}
        self.assertEqual(_before_send(event, {}), event)

    def test_scrubbing_covers_every_shared_sensitive_parameter(self):
        """Guards against the middleware list and Sentry scrubbing drifting apart."""
        query = "&".join(f"{name}=leaked_{name}" for name in SENSITIVE_QUERY_PARAMETERS)
        scrubbed = _before_send({"request": {"query_string": query}}, {})

        for name in SENSITIVE_QUERY_PARAMETERS:
            self.assertNotIn(f"leaked_{name}", scrubbed["request"]["query_string"])

    def test_fails_closed_when_scrubbing_raises(self):
        """If scrubbing cannot complete, the event is dropped rather than sent raw."""
        with mock.patch(
            "core.observability._scrub_query_string", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("core.observability", level=logging.ERROR):
                result = _before_send({"request": {"query_string": "token=live"}}, {})

        self.assertIsNone(result)


class InitSentryTests(SimpleTestCase):
    def test_no_dsn_is_a_noop(self):
        with mock.patch.dict("os.environ", {"SENTRY_DSN": ""}, clear=False):
            self.assertFalse(init_sentry(environment="test", release="abc"))

    def test_missing_sdk_does_not_raise(self):
        """A configured DSN with the SDK absent must not stop the app booting."""
        with mock.patch.dict(
            "os.environ", {"SENTRY_DSN": "https://k@o0.ingest.sentry.io/0"}, clear=False
        ):
            with mock.patch.dict("sys.modules", {"sentry_sdk": None}):
                self.assertFalse(init_sentry(environment="test", release="abc"))

    def test_init_failure_does_not_raise(self):
        fake_sdk = mock.MagicMock()
        fake_sdk.init.side_effect = RuntimeError("bad dsn")
        modules = {
            "sentry_sdk": fake_sdk,
            "sentry_sdk.integrations.django": mock.MagicMock(),
            "sentry_sdk.integrations.logging": mock.MagicMock(),
        }
        with mock.patch.dict(
            "os.environ", {"SENTRY_DSN": "https://k@o0.ingest.sentry.io/0"}, clear=False
        ):
            with mock.patch.dict("sys.modules", modules):
                with self.assertLogs("core.observability", level=logging.ERROR):
                    self.assertFalse(init_sentry(environment="test", release="abc"))


class EnvFloatTests(SimpleTestCase):
    def test_parses_value(self):
        with mock.patch.dict("os.environ", {"X_RATE": "0.25"}, clear=False):
            self.assertEqual(_env_float("X_RATE", 0.0), 0.25)

    def test_falls_back_on_malformed_value(self):
        with mock.patch.dict("os.environ", {"X_RATE": "not-a-number"}, clear=False):
            self.assertEqual(_env_float("X_RATE", 0.0), 0.0)

    def test_falls_back_when_unset(self):
        with mock.patch.dict("os.environ", {"X_RATE": ""}, clear=False):
            self.assertEqual(_env_float("X_RATE", 0.5), 0.5)
