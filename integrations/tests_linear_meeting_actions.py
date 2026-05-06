from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient


class FakeLinearResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@override_settings(
    LINEAR_API_KEY="lin-api-key",
    ROO_API_KEY="roo-api-key",
    INTERNAL_API_KEY="",
    MLAI_API_KEY="",
)
class LinearMeetingActionsApiTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()
        self.auth_headers = {"HTTP_X_API_KEY": "roo-api-key"}

    def test_context_rejects_requests_without_roo_api_key(self):
        response = self.client.get("/api/v1/integrations/linear/meeting-context")

        self.assertEqual(response.status_code, 401)

    @override_settings(LINEAR_API_KEY="")
    def test_missing_linear_api_key_returns_503(self):
        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "linear_not_configured")

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_context_returns_linear_meeting_context(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse({"data": {"teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]}}}),
            FakeLinearResponse({"data": {"users": {"nodes": [{"id": "user-1", "name": "Sam", "active": True}]}}}),
            FakeLinearResponse({"data": {"projects": {"nodes": [{"id": "project-1", "name": "Mlai Core", "state": "started"}]}}}),
            FakeLinearResponse({"data": {"issueLabels": {"nodes": [{"id": "label-1", "name": "meeting-action"}]}}}),
            FakeLinearResponse({"data": {"issues": {"nodes": [{"id": "issue-1", "identifier": "ENG-1", "title": "Open task"}]}}}),
        ]

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["teams"][0]["id"], "team-1")
        self.assertEqual(payload["users"][0]["id"], "user-1")
        self.assertEqual(payload["projects"][0]["id"], "project-1")
        self.assertEqual(payload["labels"][0]["id"], "label-1")
        self.assertEqual(payload["recentIssues"][0]["identifier"], "ENG-1")
        headers = mock_post.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["Authorization"], "lin-api-key")

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_graphql_errors_become_backend_error(self, mock_post):
        mock_post.return_value = FakeLinearResponse({"errors": [{"message": "bad query"}]})

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "linear_graphql_error")
        self.assertIn("bad query", response.json()["detail"])

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_create_issue_translates_snake_case_payload_to_linear_input(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-1",
                            "identifier": "ENG-123",
                            "title": "Update onboarding docs",
                            "url": "https://linear.app/acme/issue/ENG-123",
                        },
                    }
                }
            }
        )

        response = self.client.post(
            "/api/v1/integrations/linear/issues",
            {
                "title": "Update onboarding docs",
                "team_id": "team-1",
                "description": "Meeting-derived task",
                "assignee_id": "user-1",
                "project_id": "project-1",
                "priority": 2,
                "due_date": "2026-05-08",
                "label_ids": ["label-1"],
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["identifier"], "ENG-123")
        linear_input = mock_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(
            linear_input,
            {
                "title": "Update onboarding docs",
                "teamId": "team-1",
                "description": "Meeting-derived task",
                "assigneeId": "user-1",
                "projectId": "project-1",
                "dueDate": "2026-05-08",
                "priority": 2,
                "labelIds": ["label-1"],
            },
        )

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_rate_limit_preserves_retry_after(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {"errors": [{"message": "rate limited"}]},
            status_code=429,
            headers={"Retry-After": "7"},
        )

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["retryAfter"], 7)
        self.assertEqual(response["Retry-After"], "7")
