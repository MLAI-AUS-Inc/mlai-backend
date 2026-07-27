from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
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
    LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE="off",
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
            FakeLinearResponse(
                {
                    "data": {
                        "teams": {
                            "nodes": [
                                {
                                    "id": "team-1",
                                    "key": "ENG",
                                    "name": "Engineering",
                                    "members": {
                                        "nodes": [
                                            {
                                                "id": "user-1",
                                                "name": "Sam",
                                                "displayName": "Sam",
                                                "email": "sam@example.com",
                                                "active": True,
                                            },
                                            {
                                                "id": "inactive-user",
                                                "name": "Inactive",
                                                "active": False,
                                            },
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            ),
            FakeLinearResponse({"data": {"users": {"nodes": [{"id": "user-1", "name": "Sam", "active": True}]}}}),
            FakeLinearResponse(
                {
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "Mlai Core",
                                    "description": "Founder Games and other founder programs.",
                                    "content": "Run sheets, applications, and program operations.",
                                    "slackChannelId": "CFOUNDERS",
                                    "completedAt": None,
                                    "canceledAt": None,
                                    "status": {"name": "In Progress", "type": "started"},
                                    "lead": {
                                        "id": "lead-1",
                                        "name": "Jane",
                                        "displayName": "Jane",
                                        "email": "jane@example.com",
                                    },
                                    "lastUpdate": {
                                        "id": "update-1",
                                        "url": "https://linear.app/acme/project-update/update-1",
                                        "body": "Last update body",
                                        "health": "onTrack",
                                        "createdAt": "2026-05-01T00:00:00Z",
                                        "updatedAt": "2026-05-01T00:00:00Z",
                                        "user": {
                                            "id": "user-1",
                                            "name": "Sam",
                                            "displayName": "Sam",
                                            "email": "sam@example.com",
                                        },
                                    },
                                    "teams": {
                                        "nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]
                                    },
                                    "members": {
                                        "nodes": [
                                            {
                                                "id": "user-1",
                                                "name": "Sam",
                                                "displayName": "Sam",
                                                "email": "sam@example.com",
                                                "active": True,
                                            }
                                        ]
                                    },
                                },
                                {
                                    "id": "project-closed",
                                    "name": "Closed Project",
                                    "completedAt": "2026-05-01T00:00:00Z",
                                    "canceledAt": None,
                                    "status": {"name": "Completed", "type": "completed"},
                                    "teams": {"nodes": [{"id": "team-1"}]},
                                },
                            ]
                        }
                    }
                }
            ),
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
        self.assertEqual(payload["projects"][0]["lastUpdate"]["id"], "update-1")
        self.assertEqual(payload["projects"][0]["lastUpdate"]["user"]["email"], "sam@example.com")
        self.assertEqual(payload["projects"][0]["description"], "Founder Games and other founder programs.")
        self.assertEqual(payload["projects"][0]["slackChannelId"], "CFOUNDERS")
        self.assertEqual(payload["projects"][0]["membersSource"], "project")
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(
            [member["id"] for member in payload["projects"][0]["members"]["nodes"]],
            ["user-1", "lead-1"],
        )
        self.assertEqual(payload["labels"][0]["id"], "label-1")
        self.assertEqual(payload["recentIssues"][0]["identifier"], "ENG-1")
        headers = mock_post.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["Authorization"], "lin-api-key")
        team_request = mock_post.call_args_list[0].kwargs["json"]
        self.assertEqual(team_request["variables"]["memberFirst"], 50)
        project_request = mock_post.call_args_list[2].kwargs["json"]
        self.assertEqual(project_request["operationName"], "LinearProjects")
        self.assertIn("status", project_request["query"])
        self.assertIn("completedAt", project_request["query"])
        self.assertIn("canceledAt", project_request["query"])
        self.assertIn("lastUpdate", project_request["query"])
        self.assertNotIn("\n          state\n", project_request["query"])
        self.assertIn("\n          members", project_request["query"])
        self.assertIn("description", project_request["query"])
        self.assertIn("content", project_request["query"])
        self.assertIn("slackChannelId", project_request["query"])

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_project_graphql_errors_include_operation(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse({"data": {"teams": {"nodes": []}}}),
            FakeLinearResponse({"data": {"users": {"nodes": []}}}),
            FakeLinearResponse(
                {"errors": [{"message": 'Cannot query field "state" on type "Project".'}]},
                status_code=400,
            ),
        ]

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "linear_graphql_error")
        self.assertEqual(response.json()["operation"], "LinearProjects")
        self.assertIn("Cannot query field", response.json()["detail"])

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_team_members_query_falls_back_when_unsupported(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse({"errors": [{"message": 'Cannot query field "members" on type "Team".'}]}),
            FakeLinearResponse({"data": {"teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]}}}),
            FakeLinearResponse({"data": {"users": {"nodes": [{"id": "user-1", "name": "Sam", "active": True}]}}}),
            FakeLinearResponse(
                {
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "Mlai Core",
                                    "status": {"name": "In Progress", "type": "started"},
                                    "lead": {"id": "lead-1", "name": "Jane"},
                                    "teams": {"nodes": [{"id": "team-1"}]},
                                }
                            ]
                        }
                    }
                }
            ),
            FakeLinearResponse({"data": {"issueLabels": {"nodes": []}}}),
            FakeLinearResponse({"data": {"issues": {"nodes": []}}}),
        ]

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                call.kwargs["json"]["operationName"]
                for call in mock_post.call_args_list
            ],
            [
                "LinearTeamsWithMembers",
                "LinearTeams",
                "LinearUsers",
                "LinearProjects",
                "LinearIssueLabels",
                "LinearRecentIssues",
            ],
        )
        self.assertEqual(
            response.json()["projects"][0]["members"]["nodes"],
            [{"id": "lead-1", "name": "Jane"}],
        )

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
    def test_create_project_update_translates_payload_to_linear_input(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "projectUpdateCreate": {
                        "success": True,
                        "projectUpdate": {
                            "id": "update-1",
                            "url": "https://linear.app/acme/project-update/update-1",
                            "body": "Meeting-derived update",
                            "health": "onTrack",
                            "createdAt": "2026-05-08T00:00:00Z",
                            "project": {"id": "project-1", "name": "Mlai Core"},
                        },
                    }
                }
            }
        )

        response = self.client.post(
            "/api/v1/integrations/linear/project-updates",
            {
                "project_id": "project-1",
                "body": "Meeting-derived update",
                "health": "onTrack",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["id"], "update-1")
        linear_input = mock_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(
            linear_input,
            {
                "projectId": "project-1",
                "body": "Meeting-derived update",
                "health": "onTrack",
            },
        )

    def test_create_project_update_rejects_invalid_health(self):
        response = self.client.post(
            "/api/v1/integrations/linear/project-updates",
            {
                "project_id": "project-1",
                "body": "Meeting-derived update",
                "health": "blocked",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("health", response.json()["detail"])

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


@override_settings(
    LINEAR_API_KEY="lin-api-key",
    ROO_API_KEY="roo-api-key",
    INTERNAL_API_KEY="",
    MLAI_API_KEY="",
    LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE="off",
)
class LinearMeetingIssueIdempotencyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.auth_headers = {"HTTP_X_API_KEY": "roo-api-key"}

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_repeated_idempotency_key_returns_original_issue(self, mock_post):
        from integrations.models import LinearIssueCreationReceipt

        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-1",
                            "identifier": "ENG-123",
                            "title": "Send Founder Games run sheet to Jess",
                            "url": "https://linear.app/acme/issue/ENG-123",
                        },
                    }
                }
            }
        )
        payload = {
            "title": "Send Founder Games run sheet to Jess",
            "team_id": "team-1",
            "assignee_id": "user-sam",
            "project_id": "project-founder-program",
            "due_date": "2026-07-24",
            "idempotency_key": "a" * 64,
        }

        first = self.client.post(
            "/api/v1/integrations/linear/issues",
            payload,
            format="json",
            **self.auth_headers,
        )
        second = self.client.post(
            "/api/v1/integrations/linear/issues",
            payload,
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["identifier"], "ENG-123")
        self.assertTrue(second.json()["idempotentReplay"])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(LinearIssueCreationReceipt.objects.count(), 1)
        receipt = LinearIssueCreationReceipt.objects.get()
        self.assertEqual(receipt.status, LinearIssueCreationReceipt.Status.COMPLETED)
        self.assertEqual(receipt.linear_issue_payload["identifier"], "ENG-123")

    def test_invalid_idempotency_key_is_rejected(self):
        response = self.client.post(
            "/api/v1/integrations/linear/issues",
            {
                "title": "Invalid key",
                "team_id": "team-1",
                "idempotency_key": "short",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("idempotency_key", response.json()["detail"])

    @override_settings(LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE="required")
    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_required_studio_sizing_is_enforced_and_persisted(self, mock_post):
        from integrations.models import LinearIssueCreationReceipt

        sizing_metadata = {
            "effortLabel": "Extra Small (XS)",
            "rationale": "This is a well-scoped email expected to take about 15 minutes.",
            "projectId": "project-studio",
            "projectNameAtAssessment": "[Studio] Founder Games",
            "rubricVersion": "studio-effort-v1",
        }
        mock_post.side_effect = [
            FakeLinearResponse(
                {
                    "data": {
                        "project": {
                            "id": "project-studio",
                            "name": "[Studio] Founder Games",
                        }
                    }
                }
            ),
            FakeLinearResponse(
                {
                    "data": {
                        "issueLabels": {
                            "nodes": [
                                {
                                    "id": "effort-xs",
                                    "name": "Extra Small (XS)",
                                    "team": None,
                                }
                            ]
                        }
                    }
                }
            ),
            FakeLinearResponse(
                {
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {
                                "id": "issue-1",
                                "identifier": "STU-1",
                                "title": "Email the Founder Games run sheet",
                                "url": "https://linear.app/acme/issue/STU-1",
                            },
                        }
                    }
                }
            ),
        ]
        payload = {
            "title": "Email the Founder Games run sheet",
            "team_id": "team-1",
            "project_id": "project-studio",
            "label_ids": ["effort-xs"],
            "idempotency_key": "s" * 64,
            "sizing_metadata": sizing_metadata,
        }

        response = self.client.post(
            "/api/v1/integrations/linear/issues",
            payload,
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sizingMetadata"], sizing_metadata)
        self.assertTrue(response.json()["sizingEnforcement"]["valid"])
        linear_input = mock_post.call_args_list[-1].kwargs["json"]["variables"]["input"]
        self.assertNotIn("sizing_metadata", linear_input)
        self.assertEqual(linear_input["labelIds"], ["effort-xs"])
        receipt = LinearIssueCreationReceipt.objects.get(idempotency_key="s" * 64)
        self.assertEqual(receipt.linear_issue_payload["sizingMetadata"], sizing_metadata)

        replay = self.client.post(
            "/api/v1/integrations/linear/issues",
            payload,
            format="json",
            **self.auth_headers,
        )
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()["idempotentReplay"])
        self.assertEqual(replay.json()["sizingMetadata"], sizing_metadata)
        self.assertEqual(mock_post.call_count, 3)

    @override_settings(LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE="required")
    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_required_studio_sizing_fails_closed_and_marks_receipt_failed(self, mock_post):
        from integrations.models import LinearIssueCreationReceipt

        mock_post.side_effect = [
            FakeLinearResponse(
                {
                    "data": {
                        "project": {
                            "id": "project-studio",
                            "name": "[Studio] Founder Games",
                        }
                    }
                }
            ),
            FakeLinearResponse({"data": {"issueLabels": {"nodes": []}}}),
        ]

        response = self.client.post(
            "/api/v1/integrations/linear/issues",
            {
                "title": "Ambiguous Studio task",
                "team_id": "team-1",
                "project_id": "project-studio",
                "idempotency_key": "f" * 64,
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("sizing_metadata", response.json()["detail"])
        receipt = LinearIssueCreationReceipt.objects.get(idempotency_key="f" * 64)
        self.assertEqual(receipt.status, LinearIssueCreationReceipt.Status.FAILED)
        self.assertIn("ValueError", receipt.last_error)

    @override_settings(LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE="required")
    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_project_rename_after_studio_preview_returns_conflict(self, mock_post):
        from integrations.models import LinearIssueCreationReceipt

        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "project": {
                        "id": "project-studio",
                        "name": "Founder Games",
                    }
                }
            }
        )
        response = self.client.post(
            "/api/v1/integrations/linear/issues",
            {
                "title": "Send the run sheet",
                "team_id": "team-1",
                "project_id": "project-studio",
                "label_ids": ["effort-s"],
                "idempotency_key": "r" * 64,
                "sizing_metadata": {
                    "effortLabel": "Small (S)",
                    "rationale": "The remaining edit should take about 45 minutes.",
                    "projectId": "project-studio",
                    "projectNameAtAssessment": "[Studio] Founder Games",
                    "rubricVersion": "studio-effort-v1",
                },
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "linear_studio_sizing_stale")
        receipt = LinearIssueCreationReceipt.objects.get(idempotency_key="r" * 64)
        self.assertEqual(receipt.status, LinearIssueCreationReceipt.Status.FAILED)

    def test_pending_idempotency_conflict_returns_409_and_receipt_can_be_read(self):
        from integrations.models import LinearIssueCreationReceipt

        receipt = LinearIssueCreationReceipt.objects.create(
            idempotency_key="p" * 64,
            request_payload={
                "title": "In flight",
                "team_id": "team-1",
                "sizing_metadata": {"effortLabel": "Small (S)"},
            },
        )

        conflict = self.client.post(
            "/api/v1/integrations/linear/issues",
            {
                "title": "In flight",
                "team_id": "team-1",
                "idempotency_key": "p" * 64,
            },
            format="json",
            **self.auth_headers,
        )
        lookup = self.client.get(
            f"/api/v1/integrations/linear/issues/receipts/{receipt.idempotency_key}",
            **self.auth_headers,
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "linear_issue_creation_in_progress")
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["status"], "pending")
        self.assertEqual(lookup.json()["sizingMetadata"]["effortLabel"], "Small (S)")


@override_settings(
    LINEAR_API_KEY="lin-api-key",
    ROO_API_KEY="roo-api-key",
    INTERNAL_API_KEY="",
    MLAI_API_KEY="",
)
class LinearProjectSizingContextApiTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()
        self.auth_headers = {"HTTP_X_API_KEY": "roo-api-key"}

    @patch("integrations.api_views_connectors.get_linear_project_sizing_context")
    def test_project_sizing_context_forwards_bounded_options(self, mock_context):
        mock_context.return_value = {
            "project": {"id": "project-studio", "name": "[Studio] Founder Games"},
            "projectUpdates": {"nodes": []},
            "activeIssues": {"nodes": []},
            "terminalReferences": {"nodes": []},
            "sizingPrecedents": {"nodes": []},
        }

        response = self.client.get(
            "/api/v1/integrations/linear/projects/project-studio/sizing-context"
            "?update_limit=7&active_issue_limit=45&terminal_issue_limit=12&precedent_limit=22",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project"]["name"], "[Studio] Founder Games")
        mock_context.assert_called_once_with(
            "project-studio",
            update_limit="7",
            active_issue_limit="45",
            terminal_issue_limit="12",
            precedent_limit="22",
        )

    @patch("integrations.services.linear_meeting_actions.list_issue_labels")
    @patch(
        "integrations.services.linear_meeting_actions._fetch_linear_project_sizing_detail"
    )
    def test_sizing_context_classifies_remaining_and_reference_work(
        self,
        mock_project_detail,
        mock_labels,
    ):
        from integrations.services.linear_meeting_actions import (
            get_linear_project_sizing_context,
        )

        mock_labels.return_value = [
            {"id": "effort-s", "name": "Small (S)", "team": None}
        ]
        mock_project_detail.return_value = (
            {
                "id": "project-studio",
                "name": "[Studio] Founder Games",
                "progress": 0.6,
                "projectUpdates": {
                    "nodes": [{"id": "update-1", "body": "The run sheet is drafted."}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "issues": {
                    "nodes": [
                        {
                            "id": "active-1",
                            "identifier": "STU-1",
                            "title": "Send the run sheet",
                            "state": {"name": "In Progress", "type": "started"},
                            "labels": {
                                "nodes": [{"id": "effort-s", "name": "Small (S)"}]
                            },
                            "relations": {
                                "nodes": [
                                    {
                                        "type": "blocks",
                                        "relatedIssue": {
                                            "id": "related-1",
                                            "identifier": "STU-2",
                                            "title": "Confirm venue",
                                            "state": {
                                                "name": "Todo",
                                                "type": "unstarted",
                                            },
                                        },
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            },
                            "inverseRelations": {"nodes": [], "pageInfo": {}},
                        },
                        {
                            "id": "done-1",
                            "identifier": "STU-3",
                            "title": "Draft the run sheet",
                            "state": {"name": "Done", "type": "completed"},
                            "labels": {"nodes": []},
                            "relations": {"nodes": [], "pageInfo": {}},
                            "inverseRelations": {"nodes": [], "pageInfo": {}},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
            True,
        )

        payload = get_linear_project_sizing_context("project-studio")

        self.assertEqual(payload["activeIssues"]["nodes"][0]["id"], "active-1")
        self.assertEqual(
            payload["activeIssues"]["nodes"][0]["relations"]["edges"][0]["type"],
            "blocks",
        )
        self.assertEqual(
            payload["terminalReferences"]["nodes"][0]["id"],
            "done-1",
        )
        self.assertEqual(
            payload["sizingPrecedents"]["nodes"][0]["id"],
            "active-1",
        )
        self.assertEqual(
            payload["effortLabelRegistry"]["expectedNames"],
            [
                "Extra Small (XS)",
                "Small (S)",
                "Medium (M)",
                "Large (L)",
                "Extra Large (XL)",
            ],
        )
