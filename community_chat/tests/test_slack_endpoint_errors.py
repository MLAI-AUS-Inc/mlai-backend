from unittest.mock import MagicMock, patch

from django.db import DatabaseError
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory
from slack_sdk.errors import SlackApiError, SlackRequestError

from community_chat.slack_views import SlackDmStartView, SlackUserDirectoryView
from integrations.services.slack_dm_mirror import (
    SlackDmMirrorError,
    SlackDmMirrorUpstreamError,
)


def _slack_api_error(error_code, *, status_code=200, retry_after=None):
    response = MagicMock()
    response.get.side_effect = lambda key, default=None: {
        "error": error_code
    }.get(key, default)
    response.status_code = status_code
    response.headers = (
        {"Retry-After": str(retry_after)} if retry_after is not None else {}
    )
    return SlackApiError("Slack API request failed", response)


class SlackEndpointErrorMappingTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _request_with_service_error(self, endpoint, exc):
        view_options = {
            "authentication_classes": (),
            "permission_classes": (),
            "throttle_classes": (),
        }
        with patch(
            "community_chat.slack_views.active_grant_for_user",
            return_value=MagicMock(),
        ):
            if endpoint == "directory":
                with patch(
                    "community_chat.slack_views.search_slack_users",
                    side_effect=exc,
                ):
                    request = self.factory.get("/community-chat/slack/users/")
                    return SlackUserDirectoryView.as_view(**view_options)(request)
            with patch(
                "community_chat.slack_views.open_slack_dm",
                side_effect=exc,
            ):
                request = self.factory.post(
                    "/community-chat/slack/dms/",
                    {"slack_user_ids": ["UALICE"]},
                    format="json",
                )
                request.community_chat_public_key = "1" * 64
                return SlackDmStartView.as_view(**view_options)(request)

    def _assert_both_endpoints(self, exception_factory, expected_status, payload):
        for endpoint in ("directory", "dm_start"):
            with self.subTest(endpoint=endpoint):
                response = self._request_with_service_error(
                    endpoint,
                    exception_factory(),
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data, payload)

    def test_slack_auth_errors_require_reauthorization(self):
        self._assert_both_endpoints(
            lambda: _slack_api_error("invalid_auth"),
            status.HTTP_401_UNAUTHORIZED,
            {"error": "slack_reauthorization_required"},
        )

    def test_slack_rate_limits_preserve_retry_after(self):
        for endpoint in ("directory", "dm_start"):
            with self.subTest(endpoint=endpoint):
                response = self._request_with_service_error(
                    endpoint,
                    _slack_api_error(
                        "ratelimited",
                        status_code=429,
                        retry_after=17,
                    ),
                )
                self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
                self.assertEqual(
                    response.data,
                    {
                        "error": "slack_rate_limited",
                        "retry_after_seconds": 17,
                    },
                )
                self.assertEqual(response["Retry-After"], "17")

    def test_other_slack_api_errors_are_bad_gateway(self):
        self._assert_both_endpoints(
            lambda: _slack_api_error("internal_error", status_code=500),
            status.HTTP_502_BAD_GATEWAY,
            {"error": "slack_upstream_unavailable"},
        )

    def test_slack_sdk_transport_errors_are_bad_gateway(self):
        self._assert_both_endpoints(
            lambda: SlackRequestError("Slack could not be reached"),
            status.HTTP_502_BAD_GATEWAY,
            {"error": "slack_upstream_unavailable"},
        )

    def test_token_refresh_transport_errors_are_bad_gateway(self):
        self._assert_both_endpoints(
            lambda: SlackDmMirrorUpstreamError("Slack token refresh failed"),
            status.HTTP_502_BAD_GATEWAY,
            {"error": "slack_upstream_unavailable"},
        )

    def test_database_errors_are_service_unavailable(self):
        self._assert_both_endpoints(
            lambda: DatabaseError("database connection failed"),
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": "slack_storage_unavailable"},
        )

    def test_domain_errors_keep_the_existing_validation_contract(self):
        directory = self._request_with_service_error(
            "directory",
            SlackDmMirrorError("Slack is not linked."),
        )
        dm_start = self._request_with_service_error(
            "dm_start",
            SlackDmMirrorError("Slack is not linked."),
        )

        self.assertEqual(directory.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(directory.data, {"slack": "Slack is not linked."})
        self.assertEqual(dm_start.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            dm_start.data,
            {
                "slack": "Slack is not linked.",
                "code": "slack_dm_mirror_error",
            },
        )
