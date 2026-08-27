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
    LINEAR_CHANNEL_ISSUE_BINDINGS_JSON=(
        '{"TMLAI:CTECH": {'
        '"display_name": "MLAI_TECH · Todo", '
        '"team_name": "MLAI_TECH", '
        '"state_name": "Todo", '
        '"linear_team_id": "team-tech", '
        '"linear_state_id": "state-todo"}}'
    ),
)
class LinearMeetingActionsApiTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()
        self.auth_headers = {"HTTP_X_API_KEY": "roo-api-key"}

    def test_context_rejects_requests_without_roo_api_key(self):
        response = self.client.get("/api/v1/integrations/linear/meeting-context")

        self.assertEqual(response.status_code, 401)

    def test_channel_issue_list_rejects_requests_without_roo_api_key(self):
        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            {
                "slack_workspace_id": "TMLAI",
                "slack_channel_id": "CTECH",
                "requester_slack_id": "U123",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_list_uses_bound_team_and_state(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "issue-16",
                                "identifier": "TECH-16",
                                "title": "Roo jobs filtering",
                                "url": "https://linear.app/mlai-aus/issue/TECH-16",
                                "state": {
                                    "id": "state-todo",
                                    "name": "Todo",
                                    "type": "unstarted",
                                },
                                "team": {
                                    "id": "team-tech",
                                    "key": "TECH",
                                    "name": "MLAI_TECH",
                                },
                            },
                            {
                                "id": "issue-other",
                                "identifier": "MLAI-1",
                                "title": "Out of scope",
                                "state": {"id": "state-todo"},
                                "team": {"id": "team-other"},
                            },
                        ],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    }
                }
            }
        )

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            {
                "slack_workspace_id": "TMLAI",
                "slack_channel_id": "CTECH",
                "requester_slack_id": "U123",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [issue["identifier"] for issue in response.json()["issues"]],
            ["TECH-16"],
        )
        self.assertEqual(response.json()["list"]["displayName"], "MLAI_TECH · Todo")
        request_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(request_payload["operationName"], "LinearChannelIssueList")
        self.assertEqual(request_payload["variables"]["teamId"], "team-tech")
        self.assertEqual(request_payload["variables"]["stateId"], "state-todo")

    def test_channel_issue_list_rejects_unbound_channel(self):
        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            {
                "slack_workspace_id": "TMLAI",
                "slack_channel_id": "COTHER",
                "requester_slack_id": "U123",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["code"],
            "linear_channel_issue_access_denied",
        )

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_detail_returns_description_relations_and_comments(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse(
                {
                    "data": {
                        "issue": {
                            "id": "issue-16",
                            "identifier": "TECH-16",
                            "title": "Roo jobs filtering",
                            "description": "Full issue description",
                            "state": {
                                "id": "state-todo",
                                "name": "Todo",
                                "type": "unstarted",
                            },
                            "team": {
                                "id": "team-tech",
                                "key": "TECH",
                                "name": "MLAI_TECH",
                            },
                            "labels": {"nodes": [{"id": "label-1", "name": "Bug"}]},
                            "attachments": {
                                "nodes": [
                                    {
                                        "id": "attachment-1",
                                        "title": "GitHub issue",
                                        "url": "https://github.com/example/issue/1",
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            },
                            "relations": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            },
                            "inverseRelations": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    }
                }
            ),
            FakeLinearResponse(
                {
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "comment-1",
                                        "body": "First comment",
                                        "createdAt": "2026-08-27T00:00:00Z",
                                        "user": {"id": "user-1", "name": "CJ"},
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                }
            ),
        ]

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/detail",
            {
                "slack_workspace_id": "TMLAI",
                "slack_channel_id": "CTECH",
                "requester_slack_id": "U123",
                "issue_identifier": "TECH-16",
                "include_comments": True,
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["issue"]["description"], "Full issue description")
        self.assertEqual(payload["issue"]["labels"][0]["name"], "Bug")
        self.assertEqual(payload["issue"]["attachments"][0]["title"], "GitHub issue")
        self.assertEqual(payload["comments"][0]["body"], "First comment")
        self.assertFalse(payload["commentsTruncated"])
        self.assertEqual(
            [call.kwargs["json"]["operationName"] for call in mock_post.call_args_list],
            ["LinearChannelIssueDetail", "LinearChannelIssueComments"],
        )

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_detail_rejects_issue_from_another_team(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "issue": {
                        "id": "issue-other",
                        "identifier": "MLAI-1",
                        "title": "Private issue",
                        "team": {"id": "team-other", "name": "MLAI"},
                    }
                }
            }
        )

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/detail",
            {
                "slack_workspace_id": "TMLAI",
                "slack_channel_id": "CTECH",
                "requester_slack_id": "U123",
                "issue_identifier": "MLAI-1",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(mock_post.call_count, 1)

    def test_project_resolve_rejects_requests_without_roo_api_key(self):
        response = self.client.get(
            "/api/v1/integrations/linear/projects/resolve",
            {"query": "[Studio] Studynash"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_project_resolve_finds_normalized_inactive_project(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "projects": {
                        "nodes": [
                            {
                                "id": "project-study-nash",
                                "name": "[Studio] Study Nash",
                                "slugId": "studio-study-nash",
                                "completedAt": "2026-08-20T00:00:00Z",
                                "canceledAt": None,
                                "status": {"name": "Completed", "type": "completed"},
                                "teams": {
                                    "nodes": [
                                        {
                                            "id": "team-1",
                                            "key": "ENG",
                                            "name": "Engineering",
                                        }
                                    ]
                                },
                                "members": {"nodes": []},
                            },
                            {
                                "id": "project-other",
                                "name": "[Studio] Aaron AI",
                                "slugId": "studio-aaron-ai",
                                "status": {"name": "Started", "type": "started"},
                                "teams": {"nodes": [{"id": "team-1"}]},
                                "members": {"nodes": []},
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

        response = self.client.get(
            "/api/v1/integrations/linear/projects/resolve",
            {"query": "[Studio] Studynash"},
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "matched")
        self.assertEqual(response.json()["project"]["id"], "project-study-nash")
        self.assertEqual(response.json()["confidence"], 1.0)
        self.assertTrue(response.json()["isInactive"])
        request_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(request_payload["operationName"], "LinearProjects")
        self.assertTrue(request_payload["variables"]["includeArchived"])

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_project_resolve_fails_closed_on_ambiguous_containment(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "projects": {
                        "nodes": [
                            {
                                "id": "project-crm",
                                "name": "[Studio] Study Nash CRM",
                                "teams": {"nodes": []},
                                "members": {"nodes": []},
                            },
                            {
                                "id": "project-app",
                                "name": "[Studio] Study Nash App",
                                "teams": {"nodes": []},
                                "members": {"nodes": []},
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

        response = self.client.get(
            "/api/v1/integrations/linear/projects/resolve",
            {"query": "[Studio] Study Nash"},
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ambiguous")
        self.assertIsNone(response.json()["project"])
        self.assertEqual(response.json()["candidateCount"], 2)

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
            FakeLinearResponse(
                {
                    "data": {
                        "issueLabels": {
                            "nodes": [
                                {
                                    "id": "label-1",
                                    "name": "meeting-action",
                                    "color": "#4EA7FC",
                                    "archivedAt": None,
                                    "team": {
                                        "id": "team-1",
                                        "key": "ENG",
                                        "name": "Engineering",
                                    },
                                }
                            ]
                        }
                    }
                }
            ),
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
        self.assertEqual(payload["labels"][0]["team"]["id"], "team-1")
        self.assertIsNone(payload["labels"][0]["archivedAt"])
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
        label_request = mock_post.call_args_list[3].kwargs["json"]
        self.assertEqual(label_request["operationName"], "LinearIssueLabels")
        self.assertIn("archivedAt", label_request["query"])
        self.assertIn("team { id key name }", label_request["query"])
        self.assertNotIn("group { id name }", label_request["query"])

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
    def test_context_paginates_every_accessible_team(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse(
                {
                    "data": {
                        "teams": {
                            "nodes": [
                                {
                                    "id": "team-mlai",
                                    "key": "MLA",
                                    "name": "MLAI",
                                    "members": {"nodes": []},
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "team-cursor-1"},
                        }
                    }
                }
            ),
            FakeLinearResponse(
                {
                    "data": {
                        "teams": {
                            "nodes": [
                                {
                                    "id": "team-studio",
                                    "key": "STU",
                                    "name": "Studio",
                                    "members": {"nodes": []},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            ),
            FakeLinearResponse({"data": {"users": {"nodes": []}}}),
            FakeLinearResponse(
                {
                    "data": {
                        "projects": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            ),
            FakeLinearResponse(
                {
                    "data": {
                        "issueLabels": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            ),
            FakeLinearResponse({"data": {"issues": {"nodes": []}}}),
        ]

        response = self.client.get(
            "/api/v1/integrations/linear/meeting-context",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(team["id"], team["key"]) for team in response.json()["teams"]],
            [("team-mlai", "MLA"), ("team-studio", "STU")],
        )
        first_team_request = mock_post.call_args_list[0].kwargs["json"]
        second_team_request = mock_post.call_args_list[1].kwargs["json"]
        self.assertIsNone(first_team_request["variables"]["after"])
        self.assertEqual(second_team_request["variables"]["after"], "team-cursor-1")

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

    @patch("integrations.services.linear_meeting_actions._graphql")
    def test_sizing_context_retries_basic_query_when_linear_query_is_too_complex(
        self,
        mock_graphql,
    ):
        from integrations.services.linear_meeting_actions import (
            LinearMeetingGraphQLError,
            _fetch_linear_project_sizing_detail,
        )

        mock_graphql.side_effect = [
            LinearMeetingGraphQLError(
                "Query too complex",
                operation="LinearProjectSizingContext",
            ),
            {
                "project": {
                    "id": "project-studio",
                    "name": "[Studio] Founder Games",
                }
            },
        ]

        project, relations_available = _fetch_linear_project_sizing_detail(
            "project-studio",
            update_limit=5,
            issue_limit=70,
        )

        self.assertEqual(project["id"], "project-studio")
        self.assertFalse(relations_available)
        self.assertEqual(mock_graphql.call_count, 2)
        self.assertEqual(
            mock_graphql.call_args_list[0].kwargs["operation_name"],
            "LinearProjectSizingContext",
        )
        self.assertEqual(
            mock_graphql.call_args_list[1].kwargs["operation_name"],
            "LinearProjectSizingContextBasic",
        )
        self.assertIn("relations(first: 10)", mock_graphql.call_args_list[0].args[0])
        self.assertNotIn(
            "relations(first: 10)",
            mock_graphql.call_args_list[1].args[0],
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


@override_settings(
    LINEAR_API_KEY="lin-api-key",
    LINEAR_TASK_SIZING_ENFORCEMENT_MODE="required",
    LINEAR_PROJECT_SIZING_RUN_TTL_SECONDS=86400,
)
class LinearProjectSizingRunTests(TestCase):
    def _labels(self):
        names = (
            "Extra Small (XS)",
            "Small (S)",
            "Medium (M)",
            "Large (L)",
            "Extra Large (XL)",
        )
        return [
            {"id": f"effort-{index}", "name": name, "team": None}
            for index, name in enumerate(names)
        ]

    def _run_payload(self, *, mode="missing_only", original_labels=None):
        return {
            "project_id": "project-1",
            "project_name": "Aaron AI",
            "requested_by_slack_user_id": "USAM",
            "requested_by_linear_user_id": "linear-sam",
            "mode": mode,
            "model": "gpt-5.6-sol",
            "rubric_version": "project-effort-v2",
            "source_snapshot_at": "2026-08-23T01:00:00Z",
            "idempotency_key": "a" * 64,
            "items": [
                {
                    "issue_id": "issue-1",
                    "identifier": "ENG-1",
                    "title": "Send the interview invite",
                    "team_id": "team-1",
                    "expected_updated_at": "2026-08-23T00:00:00.000Z",
                    "original_labels": original_labels
                    if original_labels is not None
                    else [{"id": "meeting", "name": "meeting-action"}],
                    "effort_label": "Small (S)",
                    "rationale": "The scoped invitation should take about 45 minutes.",
                    "sizing_metadata": {
                        "project_id": "project-1",
                        "effort_label": "Small (S)",
                        "rationale": "The scoped invitation should take about 45 minutes.",
                    },
                }
            ],
        }

    @patch("integrations.services.linear_meeting_actions.list_issue_labels")
    @patch("integrations.services.linear_meeting_actions._get_linear_project_identity")
    def test_required_creation_sizing_applies_to_non_studio_projects(
        self,
        mock_project,
        mock_labels,
    ):
        from integrations.services.linear_meeting_actions import (
            _enforce_project_sizing,
        )

        mock_project.return_value = {"id": "project-1", "name": "Aaron AI"}
        mock_labels.return_value = self._labels()
        result = _enforce_project_sizing(
            {
                "project_id": "project-1",
                "team_id": "team-1",
                "idempotency_key": "z" * 64,
                "label_ids": ["effort-1"],
                "sizing_metadata": {
                    "projectId": "project-1",
                    "projectNameAtAssessment": "Aaron AI",
                    "rubricVersion": "project-effort-v2",
                    "effortLabel": "Small (S)",
                    "rationale": "The scoped task should take about 45 minutes.",
                },
            }
        )

        self.assertTrue(result["projectIssue"])
        self.assertTrue(result["valid"])

    @patch("integrations.services.linear_meeting_actions.list_issue_labels")
    @patch(
        "integrations.services.linear_meeting_actions._get_linear_project_sizing_authorization"
    )
    def test_preview_then_apply_preserves_unrelated_labels_and_replays(
        self,
        mock_project,
        mock_labels,
    ):
        from integrations.services.linear_meeting_actions import (
            apply_linear_project_sizing_run,
            create_linear_project_sizing_run,
        )

        mock_project.return_value = {
            "id": "project-1",
            "name": "Aaron AI",
            "lead": {"id": "linear-sam"},
            "members": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        mock_labels.return_value = self._labels()
        run = create_linear_project_sizing_run(self._run_payload())

        live_issue = {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Send the interview invite",
            "updatedAt": "2026-08-23T00:00:00.000Z",
            "project": {"id": "project-1"},
            "team": {"id": "team-1"},
            "state": {"type": "started"},
            "labels": {
                "nodes": [{"id": "meeting", "name": "meeting-action"}],
                "pageInfo": {"hasNextPage": False},
            },
        }
        with patch(
            "integrations.services.linear_meeting_actions._get_linear_issue_for_sizing",
            return_value=live_issue,
        ), patch(
            "integrations.services.linear_meeting_actions._update_linear_issue_labels",
            return_value={"id": "issue-1", "identifier": "ENG-1"},
        ) as mock_update:
            applied = apply_linear_project_sizing_run(
                run["id"],
                requested_by_slack_user_id="USAM",
            )
            replay = apply_linear_project_sizing_run(
                run["id"],
                requested_by_slack_user_id="USAM",
            )

        self.assertEqual(applied["status"], "completed")
        self.assertEqual(applied["counts"]["applied"], 1)
        self.assertEqual(replay["counts"]["applied"], 1)
        mock_update.assert_called_once_with(
            "issue-1",
            ["meeting", "effort-1"],
        )

    @patch("integrations.services.linear_meeting_actions.list_issue_labels")
    @patch(
        "integrations.services.linear_meeting_actions._get_linear_project_sizing_authorization"
    )
    def test_missing_only_repairs_multiple_effort_labels(
        self,
        mock_project,
        mock_labels,
    ):
        from integrations.services.linear_meeting_actions import (
            apply_linear_project_sizing_run,
            create_linear_project_sizing_run,
        )

        mock_project.return_value = {
            "id": "project-1",
            "name": "Aaron AI",
            "lead": {"id": "linear-sam"},
            "members": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        mock_labels.return_value = self._labels()
        original = [
            {"id": "meeting", "name": "meeting-action"},
            {"id": "old-xs", "name": "Extra Small (XS)"},
            {"id": "old-xl", "name": "Extra Large (XL)"},
        ]
        run = create_linear_project_sizing_run(
            self._run_payload(original_labels=original)
        )
        live_issue = {
            "id": "issue-1",
            "updatedAt": "2026-08-23T00:00:00.000Z",
            "project": {"id": "project-1"},
            "team": {"id": "team-1"},
            "state": {"type": "started"},
            "labels": {
                "nodes": original,
                "pageInfo": {"hasNextPage": False},
            },
        }
        with patch(
            "integrations.services.linear_meeting_actions._get_linear_issue_for_sizing",
            return_value=live_issue,
        ), patch(
            "integrations.services.linear_meeting_actions._update_linear_issue_labels",
            return_value={"id": "issue-1"},
        ) as mock_update:
            result = apply_linear_project_sizing_run(
                run["id"],
                requested_by_slack_user_id="USAM",
            )

        self.assertEqual(result["counts"]["applied"], 1)
        mock_update.assert_called_once_with(
            "issue-1",
            ["meeting", "effort-1"],
        )

    @patch("integrations.services.linear_meeting_actions.list_issue_labels")
    @patch(
        "integrations.services.linear_meeting_actions._get_linear_project_sizing_authorization"
    )
    def test_apply_skips_terminal_race_without_mutation(
        self,
        mock_project,
        mock_labels,
    ):
        from integrations.services.linear_meeting_actions import (
            apply_linear_project_sizing_run,
            create_linear_project_sizing_run,
        )

        mock_project.return_value = {
            "id": "project-1",
            "name": "Aaron AI",
            "lead": {"id": "linear-sam"},
            "members": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        mock_labels.return_value = self._labels()
        run = create_linear_project_sizing_run(self._run_payload())
        with patch(
            "integrations.services.linear_meeting_actions._get_linear_issue_for_sizing",
            return_value={
                "id": "issue-1",
                "project": {"id": "project-1"},
                "team": {"id": "team-1"},
                "state": {"type": "completed"},
                "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            },
        ), patch(
            "integrations.services.linear_meeting_actions._update_linear_issue_labels"
        ) as mock_update:
            result = apply_linear_project_sizing_run(
                run["id"],
                requested_by_slack_user_id="USAM",
            )

        self.assertEqual(result["counts"]["skipped_terminal"], 1)
        mock_update.assert_not_called()


@override_settings(LINEAR_API_KEY="lin-api-key")
class LinearProjectSizingInventoryTests(SimpleTestCase):
    @patch("integrations.services.linear_meeting_actions._graphql")
    def test_issue_inventory_forwards_cursor_and_returns_page_metadata(
        self,
        mock_graphql,
    ):
        from integrations.services.linear_meeting_actions import (
            get_linear_project_issue_page,
        )

        mock_graphql.return_value = {
            "project": {
                "id": "project-1",
                "name": "Aaron AI",
                "issues": {
                    "nodes": [{"id": "issue-101", "title": "Page two"}],
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": "cursor-2",
                    },
                },
            }
        }
        payload = get_linear_project_issue_page(
            "project-1",
            after="cursor-1",
            limit=50,
        )

        query = mock_graphql.call_args.args[0]
        variables = mock_graphql.call_args.args[1]
        self.assertIn("orderBy: createdAt", query)
        self.assertEqual(variables["after"], "cursor-1")
        self.assertEqual(payload["nodes"][0]["id"], "issue-101")
        self.assertTrue(payload["pageInfo"]["hasNextPage"])
        self.assertEqual(
            payload["terminalStateTypes"],
            ["canceled", "cancelled", "completed", "duplicate"],
        )

    @patch("integrations.services.linear_meeting_actions._graphql")
    def test_project_update_inventory_is_cursor_paginated(self, mock_graphql):
        from integrations.services.linear_meeting_actions import (
            get_linear_project_update_page,
        )

        mock_graphql.return_value = {
            "project": {
                "id": "project-1",
                "name": "Aaron AI",
                "projectUpdates": {
                    "nodes": [{"id": "update-26", "body": "More context"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
        payload = get_linear_project_update_page(
            "project-1",
            after="update-cursor-1",
            limit=25,
        )

        query = mock_graphql.call_args.args[0]
        variables = mock_graphql.call_args.args[1]
        self.assertIn("orderBy: createdAt", query)
        self.assertEqual(variables["after"], "update-cursor-1")
        self.assertEqual(payload["nodes"][0]["id"], "update-26")


@override_settings(
    LINEAR_API_KEY="lin-api-key",
    ROO_API_KEY="roo-api-key",
    INTERNAL_API_KEY="",
    MLAI_API_KEY="",
)
class LinearMeetingActionBatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.auth_headers = {"HTTP_X_API_KEY": "roo-api-key"}
        self.payload = {
            "requested_by_slack_user_id": "UDANIEL",
            "slack_channel_id": "C1",
            "slack_thread_ts": "1.1",
            "source_fingerprint": "a" * 64,
            "items": [
                {
                    "issue_input": {
                        "title": "Compare Apollo and Firmable",
                        "team_id": "team-1",
                        "project_id": "project-1",
                        "idempotency_key": "b" * 64,
                    },
                    "display": {
                        "title": "Compare Apollo and Firmable",
                        "project": "[Studio] Project Acquire",
                        "assignee": "callumpholt",
                    },
                    "reason": "Needs approval",
                }
            ],
        }

    def test_batch_survives_database_reload_and_approval_is_idempotent(self):
        created = self.client.post(
            "/api/v1/integrations/linear/action-batches",
            self.payload,
            format="json",
            **self.auth_headers,
        )
        self.assertEqual(created.status_code, 201)
        batch_id = created.json()["id"]
        item_id = created.json()["items"][0]["id"]

        detail = self.client.get(
            f"/api/v1/integrations/linear/action-batches/{batch_id}",
            **self.auth_headers,
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["items"][0]["status"], "pending")

        with patch(
            "integrations.services.linear_meeting_actions.create_linear_meeting_issue",
            return_value={
                "id": "issue-1",
                "identifier": "MLA-1",
                "url": "https://linear.app/issue/MLA-1",
            },
        ) as create_issue:
            decision = self.client.post(
                f"/api/v1/integrations/linear/action-batches/{batch_id}/decisions",
                {
                    "requested_by_slack_user_id": "UDANIEL",
                    "decision": "approve",
                    "item_ids": [item_id],
                },
                format="json",
                **self.auth_headers,
            )
            replay = self.client.post(
                f"/api/v1/integrations/linear/action-batches/{batch_id}/decisions",
                {
                    "requested_by_slack_user_id": "UDANIEL",
                    "decision": "approve",
                    "item_ids": [item_id],
                },
                format="json",
                **self.auth_headers,
            )

        self.assertEqual(decision.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(decision.json()["items"][0]["status"], "approved")
        self.assertEqual(replay.json()["items"][0]["status"], "approved")
        create_issue.assert_called_once()

    def test_identical_pending_batch_creation_reuses_the_durable_batch(self):
        first = self.client.post(
            "/api/v1/integrations/linear/action-batches",
            self.payload,
            format="json",
            **self.auth_headers,
        )
        replay = self.client.post(
            "/api/v1/integrations/linear/action-batches",
            self.payload,
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertEqual(first.json()["items"][0]["id"], replay.json()["items"][0]["id"])

    def test_batch_rejects_a_different_slack_user(self):
        created = self.client.post(
            "/api/v1/integrations/linear/action-batches",
            self.payload,
            format="json",
            **self.auth_headers,
        ).json()
        response = self.client.post(
            f"/api/v1/integrations/linear/action-batches/{created['id']}/decisions",
            {
                "requested_by_slack_user_id": "UOTHER",
                "decision": "approve",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "linear_meeting_action_conflict")

    def test_reject_all_closes_the_batch_without_creating_issues(self):
        created = self.client.post(
            "/api/v1/integrations/linear/action-batches",
            self.payload,
            format="json",
            **self.auth_headers,
        ).json()
        response = self.client.post(
            f"/api/v1/integrations/linear/action-batches/{created['id']}/decisions",
            {
                "requested_by_slack_user_id": "UDANIEL",
                "decision": "reject",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["items"][0]["status"], "rejected")
