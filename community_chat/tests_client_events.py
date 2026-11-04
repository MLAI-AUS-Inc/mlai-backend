"""POST /api/v1/community-chat/client-events/ (founder copy fallback telemetry)."""

import json
import uuid
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from community_chat.account_sessions import ACCESS_TOKEN_PREFIX, issue_account_session
from community_chat.client_events import MAX_BODY_BYTES


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ORIGIN = "https://chat.mlai.au"
LOGGER = "community_chat.client_events"


def event(**overrides):
    payload = {
        "kind": "founder_copy_fallback",
        "copy_id": "startup.error.generic",
        "raw_code": "http_500",
        "raw_digest": "0123456789abcdef",
        "status": 500,
        "route": "/my-startup/articles",
        "surface": "StartupError",
        "client": "web",
        "app_version": "0.5.60",
        "occurred_at": "2026-10-29T10:15:00Z",
    }
    payload.update(overrides)
    return payload


@override_settings(
    CACHES=LOCMEM,
    TRUSTED_PROXY_HOPS=1,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
)
class ClientEventsViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.url = reverse("community_chat_client_events")

    def post(self, body, *, forwarded="198.51.100.7", **extra):
        return self.client.post(
            self.url,
            body,
            format="json",
            HTTP_X_FORWARDED_FOR=forwarded,
            **extra,
        )

    def assert_invalid(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"error": "invalid_client_events"})

    def logged_events(self, logs):
        prefix = f"INFO:{LOGGER}:founder_client_event "
        return [json.loads(line.removeprefix(prefix)) for line in logs.output]

    def test_valid_anonymous_batch_returns_204_and_logs_each_event(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            response = self.post({"events": [event(), event(copy_id="join.error.generic", status=None)]})

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.content, b"")
        logged = self.logged_events(logs)
        self.assertEqual(len(logged), 2)
        self.assertEqual(logged[0]["copy_id"], "startup.error.generic")
        self.assertEqual(logged[0]["raw_code"], "http_500")
        self.assertIsNone(logged[0]["user_id"])
        self.assertIsNone(logged[1]["status"])
        self.assertEqual(logged[0]["occurred_at"], "2026-10-29T10:15:00+00:00")
        self.assertEqual(
            set(logged[0]),
            {
                "kind", "copy_id", "raw_code", "raw_digest", "status", "route",
                "surface", "client", "app_version", "occurred_at", "user_id",
            },
        )

    def test_signed_in_pending_member_is_attributed(self):
        user = get_user_model().objects.create_user(email="founder@example.com")
        credentials = issue_account_session(
            user,
            SimpleNamespace(
                client_id="mlai-chat-ios",
                installation_id=uuid.uuid4(),
                origin="mlaichat://callback",
                platform="ios",
                device_name="Telemetry test",
                public_key="a" * 64,
            ),
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")

        with self.assertLogs(LOGGER, level="INFO") as logs:
            response = self.post({"events": [event(client="desktop")]})

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.logged_events(logs)[0]["user_id"], user.pk)
        self.assertNotIn("founder@example.com", "\n".join(logs.output))

    def test_expired_session_is_counted_as_anonymous(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {ACCESS_TOKEN_PREFIX}not-a-session")

        with self.assertLogs(LOGGER, level="INFO") as logs:
            response = self.post({"events": [event()]})

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIsNone(self.logged_events(logs)[0]["user_id"])

    def test_bad_raw_code_is_rejected(self):
        for raw_code in ("Something went wrong: email@example.com", "has space", "x" * 121, 42):
            with self.subTest(raw_code=raw_code):
                self.assert_invalid(self.post({"events": [event(raw_code=raw_code)]}))

    def test_known_raw_codes_are_accepted(self):
        for raw_code in ("http_429", "network", "abort", "timeout", "unknown", "", "roo_points_quote_changed"):
            with self.subTest(raw_code=raw_code):
                cache.clear()
                with self.assertLogs(LOGGER, level="INFO"):
                    response = self.post({"events": [event(raw_code=raw_code)]})
                self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_more_than_twenty_events_is_rejected(self):
        self.assert_invalid(self.post({"events": [event() for _ in range(21)]}))
        self.assert_invalid(self.post({"events": []}))

    def test_routes_with_a_query_or_fragment_are_rejected(self):
        for route in ("/join?email=a@example.com", "/my-startup#token", "my-startup", "/a b", "/" + "a" * 200):
            with self.subTest(route=route):
                self.assert_invalid(self.post({"events": [event(route=route)]}))

    def test_free_text_and_unknown_fields_are_rejected(self):
        self.assert_invalid(self.post({"events": [event(message="Raw backend text")]}))
        self.assert_invalid(self.post({"events": [event()], "user": "founder@example.com"}))
        self.assert_invalid(self.post({"events": [event(copy_id="Not an identifier!")]}))
        self.assert_invalid(self.post({"events": [event(raw_digest="not-hex-digest!!")]}))
        self.assert_invalid(self.post({"events": [event(kind="page_view")]}))
        self.assert_invalid(self.post({"events": [event(client="android")]}))
        self.assert_invalid(self.post({"events": [event(status=True)]}))
        self.assert_invalid(self.post({"events": [event(occurred_at="yesterday")]}))

    def test_non_json_and_oversized_bodies_are_rejected(self):
        self.assert_invalid(
            self.client.post(self.url, "events", content_type="text/plain")
        )
        self.assert_invalid(
            self.client.post(self.url, "{not json", content_type="application/json")
        )
        self.assert_invalid(
            self.client.post(self.url, "[" * 5000 + "]" * 5000, content_type="application/json")
        )
        # Valid JSON that only the size cap refuses.
        padded = json.dumps({"events": [event()]}) + " " * MAX_BODY_BYTES
        self.assert_invalid(
            self.client.post(self.url, padded, content_type="application/json")
        )

    def test_an_over_long_integer_is_invalid_input_not_a_server_error(self):
        # json.loads raises a plain ValueError (not JSONDecodeError) for an
        # integer past Python's 4300-digit conversion limit.
        body = '{"events": [{"status": ' + "1" * 5000 + "}]}"
        self.assertLess(len(body), MAX_BODY_BYTES)
        self.assert_invalid(
            self.client.post(self.url, body, content_type="application/json")
        )

    def test_anonymous_callers_are_throttled_at_sixty_a_minute(self):
        with self.assertLogs(LOGGER, level="INFO"):
            for index in range(60):
                # Rotating a spoofed leftmost X-Forwarded-For entry does not
                # buy a fresh budget.
                response = self.post(
                    {"events": [event()]},
                    forwarded=f"192.0.2.{index}, 198.51.100.7",
                )
                self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, index)
        limited = self.post({"events": [event()]}, forwarded="192.0.2.250, 198.51.100.7")
        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertGreaterEqual(int(limited["Retry-After"]), 1)

        with self.assertLogs(LOGGER, level="INFO"):
            other = self.post({"events": [event()]}, forwarded="203.0.113.5")
        self.assertEqual(other.status_code, status.HTTP_204_NO_CONTENT)
