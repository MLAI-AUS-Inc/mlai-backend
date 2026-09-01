from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from integrations.services import linear_meeting_actions as linear_service
from integrations.api_views_connectors import (
    LinearChannelIssueDetailView,
    LinearChannelIssueListView,
    LinearChannelIssueStatusesView,
    LinearChannelIssueWriteView,
)


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
    LINEAR_WRITE_API_KEY="lin-write-key",
    LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=True,
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
        cache.clear()
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

    def test_channel_issue_views_use_separate_scoped_throttles(self):
        self.assertEqual(LinearChannelIssueListView.throttle_classes, [ScopedRateThrottle])
        self.assertEqual(
            LinearChannelIssueListView.throttle_scope,
            "linear_channel_issue_list",
        )
        self.assertEqual(LinearChannelIssueDetailView.throttle_classes, [ScopedRateThrottle])
        self.assertEqual(
            LinearChannelIssueDetailView.throttle_scope,
            "linear_channel_issue_detail",
        )
        self.assertEqual(LinearChannelIssueStatusesView.throttle_scope, "linear_channel_issue_statuses")
        self.assertEqual(LinearChannelIssueWriteView.throttle_scope, "linear_channel_issue_write")

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_statuses_are_live_and_team_scoped(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {"data": {"team": {"id": "team-tech", "states": {"nodes": [
                {"id": "state-todo", "name": "Todo", "type": "unstarted", "position": 1},
                {"id": "state-progress", "name": "In Progress", "type": "started", "position": 2},
            ]}}}}
        )

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/statuses",
            {"slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH", "requester_slack_id": "U123"},
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([state["name"] for state in response.json()["statuses"]], ["Todo", "In Progress"])
        self.assertEqual(mock_post.call_args.kwargs["json"]["variables"], {"teamId": "team-tech"})

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_immediate_status_write_rechecks_scope_and_updated_at(self, mock_post):
        issue_response = {"data": {"issue": {
            "id": "issue-29", "identifier": "TECH-29", "title": "Safe edits",
            "updatedAt": "2026-09-01T01:00:00.000Z", "archivedAt": None,
            "team": {"id": "team-tech", "key": "TECH", "name": "MLAI_TECH"},
            "state": {"id": "state-todo", "name": "Todo", "type": "unstarted"},
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "attachments": {"nodes": [], "pageInfo": {}},
            "relations": {"nodes": [], "pageInfo": {}}, "inverseRelations": {"nodes": [], "pageInfo": {}},
        }}}
        mock_post.side_effect = [
            FakeLinearResponse(issue_response),
            FakeLinearResponse(issue_response),
            FakeLinearResponse({"data": {"team": {"id": "team-tech", "states": {"nodes": [
                {"id": "state-progress", "name": "In Progress", "type": "started", "position": 2},
            ]}}}}),
            FakeLinearResponse({"data": {"issueUpdate": {"success": True, "issue": {
                "id": "issue-29", "identifier": "TECH-29", "title": "Safe edits",
                "updatedAt": "2026-09-01T01:00:01.000Z", "url": "https://linear.app/issue/TECH-29",
                "state": {"id": "state-progress", "name": "In Progress"},
            }}}}),
        ]

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "issue_identifier": "TECH-29",
                "operation": "set_status", "value": "In Progress",
                "expected_updated_at": "2026-09-01T01:00:00.000Z", "request_id": "Ev123",
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["issue"]["state"]["name"], "In Progress")
        mutation = mock_post.call_args_list[-1].kwargs["json"]
        self.assertEqual(mutation["operationName"], "LinearChannelIssueUpdate")
        self.assertEqual(mutation["variables"]["input"], {"stateId": "state-progress"})

    def test_issue_write_lock_serializes_distinct_requests_for_same_issue(self):
        binding = {
            "linear_team_id": "team-tech",
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
        }
        lock_key, owner = linear_service._claim_linear_channel_issue_write_lock(
            "issue-29", "Ev-lock-1", binding
        )
        self.addCleanup(
            linear_service._release_linear_channel_issue_write_lock,
            lock_key,
            owner,
        )

        with self.assertRaises(linear_service.LinearChannelIssueConflictError):
            linear_service._claim_linear_channel_issue_write_lock(
                "issue-29", "Ev-lock-2", binding
            )

    def test_expired_lock_owner_cannot_release_replacement_owner(self):
        binding = {
            "linear_team_id": "team-tech",
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
        }
        lock_key, old_owner = linear_service._claim_linear_channel_issue_write_lock(
            "issue-29", "Ev-old-owner", binding
        )
        replacement_owner = "replacement-owner-token"
        cache.set(lock_key, replacement_owner, timeout=600)
        self.addCleanup(cache.delete, lock_key)

        linear_service._release_linear_channel_issue_write_lock(lock_key, old_owner)

        self.assertEqual(cache.get(lock_key), replacement_owner)

    @override_settings(LINEAR_CHANNEL_ISSUE_WRITE_LOCK_SECONDS=30)
    def test_issue_write_lock_rejects_unsafe_short_ttl(self):
        self.assertEqual(linear_service._linear_channel_write_lock_ttl(), 600)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_write_rechecks_version_inside_issue_lock(self, mock_post):
        base_issue = {
            "id": "issue-29", "identifier": "TECH-29", "archivedAt": None,
            "team": {"id": "team-tech"}, "state": {"id": "state-todo"},
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "attachments": {"nodes": [], "pageInfo": {}},
            "relations": {"nodes": [], "pageInfo": {}},
            "inverseRelations": {"nodes": [], "pageInfo": {}},
        }
        mock_post.side_effect = [
            FakeLinearResponse({"data": {"issue": {**base_issue, "updatedAt": "version-1"}}}),
            FakeLinearResponse({"data": {"issue": {**base_issue, "updatedAt": "version-2"}}}),
        ]

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "issue_identifier": "TECH-29",
                "operation": "set_title", "value": "New title",
                "expected_updated_at": "version-1", "request_id": "Ev-race",
            }, format="json", **self.auth_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "linear_channel_issue_stale")
        self.assertEqual(mock_post.call_count, 2)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_ambiguous_partial_graphql_write_is_reported_as_uncertain(self, mock_post):
        mock_post.return_value = FakeLinearResponse({
            "data": {"issueUpdate": None},
            "errors": [{"message": "nested response failed"}],
        })

        with self.assertRaises(linear_service.LinearChannelIssueWriteUncertainError):
            linear_service._graphql_write(
                "mutation Test { issueUpdate { success } }",
                {"id": "issue-29", "input": {"title": "New"}},
                operation_name="LinearChannelIssueUpdate",
            )
        self.assertEqual(mock_post.call_count, 1)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_conclusive_partial_graphql_write_precedes_nested_rate_limit(self, mock_post):
        mock_post.return_value = FakeLinearResponse({
            "data": {"issueUpdate": {
                "success": True,
                "issue": {"id": "issue-29", "identifier": "TECH-29"},
            }},
            "errors": [{"message": "Rate limit hit on an optional nested field"}],
        })

        result = linear_service._graphql_write(
            "mutation Test { issueUpdate { success } }",
            {"id": "issue-29", "input": {"title": "New"}},
            operation_name="LinearChannelIssueUpdate",
        )

        self.assertTrue(result["issueUpdate"]["success"])

    @patch.object(linear_service.cache, "set", side_effect=RuntimeError("redis unavailable"))
    def test_receipt_completion_failure_is_reported_as_uncertain(self, _mock_set):
        with self.assertRaises(linear_service.LinearChannelIssueWriteUncertainError):
            linear_service._complete_linear_channel_write_request("receipt-key")

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_comment_and_duplicate_mutations_use_typed_linear_inputs(self, mock_post):
        mock_post.side_effect = [
            FakeLinearResponse({"data": {"commentCreate": {
                "success": True,
                "comment": {"id": "comment-1", "body": "Progress update"},
            }}}),
            FakeLinearResponse({"data": {"issueRelationCreate": {
                "success": True,
                "issueRelation": {"id": "relation-1", "type": "duplicate"},
            }}}),
        ]

        linear_service._create_linear_channel_issue_comment(
            "issue-29", "Progress update"
        )
        linear_service._create_linear_channel_issue_duplicate_relation(
            "issue-29", "issue-30"
        )

        comment_request = mock_post.call_args_list[0].kwargs["json"]
        self.assertEqual(comment_request["operationName"], "LinearChannelIssueComment")
        self.assertEqual(
            comment_request["variables"]["input"],
            {"issueId": "issue-29", "body": "Progress update"},
        )
        duplicate_request = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(duplicate_request["operationName"], "LinearChannelIssueDuplicate")
        self.assertEqual(
            duplicate_request["variables"]["input"],
            {
                "issueId": "issue-29",
                "relatedIssueId": "issue-30",
                "type": "duplicate",
            },
        )

    def test_supported_issue_update_operations_build_typed_inputs(self):
        issue = {
            "description": "Existing",
            "labels": {
                "nodes": [{"id": "label-old", "name": "Old"}],
                "pageInfo": {"hasNextPage": False},
            },
        }
        binding = {"linear_team_id": "team-tech"}
        self.assertEqual(
            linear_service._linear_channel_issue_update_input(
                issue=issue, binding=binding, operation="set_title", value="New title"
            ),
            {"title": "New title"},
        )
        self.assertEqual(
            linear_service._linear_channel_issue_update_input(
                issue=issue, binding=binding, operation="replace_description", value="Replacement"
            ),
            {"description": "Replacement"},
        )
        self.assertEqual(
            linear_service._linear_channel_issue_update_input(
                issue=issue, binding=binding, operation="append_description", value="Appendix"
            ),
            {"description": "Existing\n\nAppendix"},
        )
        scalar_cases = [
            ("set_priority", "high", {"priority": 2}),
            ("set_estimate", "8", {"estimate": 8}),
            ("set_estimate", "clear", {"estimate": None}),
            ("set_due_date", "2026-09-30", {"dueDate": "2026-09-30"}),
            ("set_due_date", "clear", {"dueDate": None}),
        ]
        for operation, value, expected in scalar_cases:
            with self.subTest(operation=operation, value=value):
                self.assertEqual(
                    linear_service._linear_channel_issue_update_input(
                        issue=issue, binding=binding, operation=operation, value=value
                    ),
                    expected,
                )

        for invalid_estimate in (1.5, "1.5", True):
            with self.subTest(invalid_estimate=invalid_estimate):
                with self.assertRaisesMessage(ValueError, "whole number"):
                    linear_service._linear_channel_issue_update_input(
                        issue=issue,
                        binding=binding,
                        operation="set_estimate",
                        value=invalid_estimate,
                    )

        with patch.object(
            linear_service,
            "_resolve_linear_channel_status",
            return_value={"id": "state-progress"},
        ):
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="set_status", value="In Progress"
                ),
                {"stateId": "state-progress"},
            )
        with patch.object(
            linear_service,
            "_list_linear_team_members",
            return_value=[{"id": "user-1", "name": "Alex"}],
        ):
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="set_assignee", value="Alex"
                ),
                {"assigneeId": "user-1"},
            )
        available_labels = [
            {"id": "label-old", "name": "Old", "team": {"id": "team-tech"}},
            {"id": "label-new", "name": "New", "team": {"id": "team-tech"}},
        ]
        with patch.object(linear_service, "list_issue_labels", return_value=available_labels):
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="add_label", value="New"
                ),
                {"labelIds": ["label-old", "label-new"]},
            )
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="remove_label", value="Old"
                ),
                {"labelIds": []},
            )
        with patch.object(linear_service, "_list_projects", return_value=[{
            "id": "project-1", "name": "Project One",
            "teams": {"nodes": [{"id": "team-tech"}]},
        }]):
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="set_project", value="Project One"
                ),
                {"projectId": "project-1"},
            )
        with patch.object(linear_service, "_list_linear_team_cycles", return_value=[{
            "id": "cycle-1", "name": "Cycle One", "number": 1,
        }]):
            self.assertEqual(
                linear_service._linear_channel_issue_update_input(
                    issue=issue, binding=binding, operation="set_cycle", value="Cycle One"
                ),
                {"cycleId": "cycle-1"},
            )

    @patch.object(linear_service, "_graphql")
    def test_assignee_catalogue_paginates_every_team_member(self, mock_graphql):
        mock_graphql.side_effect = [
            {"team": {
                "id": "team-tech",
                "members": {
                    "nodes": [{"id": "user-1", "name": "First", "active": True}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                },
            }},
            {"team": {
                "id": "team-tech",
                "members": {
                    "nodes": [{"id": "user-2", "name": "Second", "active": True}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }},
        ]

        members = linear_service._list_linear_team_members("team-tech")

        self.assertEqual([member["id"] for member in members], ["user-1", "user-2"])
        self.assertEqual(mock_graphql.call_args_list[0].args[1]["after"], None)
        self.assertEqual(mock_graphql.call_args_list[1].args[1]["after"], "cursor-1")

    @patch.object(linear_service, "_graphql")
    def test_cycle_catalogue_paginates_every_team_cycle(self, mock_graphql):
        mock_graphql.side_effect = [
            {"team": {
                "id": "team-tech",
                "cycles": {
                    "nodes": [{"id": "cycle-1", "name": "First"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                },
            }},
            {"team": {
                "id": "team-tech",
                "cycles": {
                    "nodes": [{"id": "cycle-2", "name": "Second"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }},
        ]

        cycles = linear_service._list_linear_team_cycles("team-tech")

        self.assertEqual([cycle["id"] for cycle in cycles], ["cycle-1", "cycle-2"])
        self.assertEqual(mock_graphql.call_args_list[0].args[1]["after"], None)
        self.assertEqual(mock_graphql.call_args_list[1].args[1]["after"], "cursor-1")

    def test_write_completion_log_attributes_slack_requester_and_channel(self):
        with self.assertLogs(
            "integrations.services.linear_meeting_actions", level="INFO"
        ) as captured:
            linear_service._linear_channel_write_result(
                {"identifier": "TECH-29", "updatedAt": "version-1"},
                {
                    "requester_slack_id": "U123",
                    "slack_workspace_id": "TMLAI",
                    "slack_channel_id": "CTECH",
                },
                "set_title",
                {"id": "issue-29"},
                "Ev-audit",
            )

        log_line = captured.output[0]
        self.assertIn("requester_slack_id=U123", log_line)
        self.assertIn("slack_workspace_id=TMLAI", log_line)
        self.assertIn("slack_channel_id=CTECH", log_line)

    @patch.object(
        linear_service,
        "_update_linear_channel_issue",
        side_effect=linear_service.LinearChannelIssueWriteUncertainError(
            "ambiguous transport"
        ),
    )
    @patch.object(linear_service, "_fetch_linear_channel_issue_detail")
    def test_uncertain_write_log_attributes_slack_requester_and_channel(
        self, mock_fetch, _mock_update
    ):
        mock_fetch.return_value = {
            "id": "issue-29",
            "identifier": "TECH-29",
            "title": "Safe edits",
            "updatedAt": "version-1",
            "archivedAt": None,
            "team": {"id": "team-tech"},
            "state": {"id": "state-todo"},
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }

        with self.assertLogs(
            "integrations.services.linear_meeting_actions", level="ERROR"
        ) as captured:
            with self.assertRaises(
                linear_service.LinearChannelIssueWriteUncertainError
            ):
                linear_service.write_linear_channel_issue({
                    "slack_workspace_id": "TMLAI",
                    "slack_channel_id": "CTECH",
                    "requester_slack_id": "U123",
                    "issue_identifier": "TECH-29",
                    "operation": "set_title",
                    "value": "New title",
                    "expected_updated_at": "version-1",
                    "request_id": "Ev-uncertain-audit",
                })

        uncertain_logs = [
            line for line in captured.output
            if "linear_channel_issue_write_uncertain" in line
        ]
        self.assertEqual(len(uncertain_logs), 1)
        self.assertIn("requester_slack_id=U123", uncertain_logs[0])
        self.assertIn("slack_channel_id=CTECH", uncertain_logs[0])

    def test_conclusive_rate_limit_releases_processing_receipt(self):
        binding = {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
        }
        receipt_key = linear_service._claim_linear_channel_write_request(
            "Ev-rate-limit", binding
        )

        with self.assertRaises(linear_service.LinearMeetingRateLimitError):
            linear_service._run_linear_channel_write_with_receipt(
                receipt_key,
                lambda: (_ for _ in ()).throw(
                    linear_service.LinearMeetingRateLimitError(5)
                ),
            )

        self.assertIsNone(cache.get(receipt_key))

    def test_label_edit_refuses_incomplete_current_label_page(self):
        issue = {
            "labels": {
                "nodes": [{"id": "label-old", "name": "Old"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
            }
        }
        with self.assertRaisesMessage(ValueError, "more than 100 labels"):
            linear_service._linear_channel_issue_update_input(
                issue=issue,
                binding={"linear_team_id": "team-tech"},
                operation="add_label",
                value="New",
            )

    @patch.object(
        linear_service,
        "list_issue_labels",
        return_value=[{"id": "label-1", "name": "Bug"}],
    )
    def test_label_edit_fails_explicitly_without_team_metadata(self, _mock_labels):
        with self.assertRaisesMessage(
            linear_service.LinearMeetingConfigurationError,
            "label team metadata is unavailable",
        ):
            linear_service._linear_channel_issue_update_input(
                issue={
                    "labels": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                    }
                },
                binding={"linear_team_id": "team-tech"},
                operation="add_label",
                value="Bug",
            )

    @override_settings(INTERNAL_API_KEY="internal-key")
    def test_immediate_write_rejects_non_roo_internal_key(self):
        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {}, format="json", HTTP_X_API_KEY="internal-key",
        )
        self.assertEqual(response.status_code, 401)

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_immediate_write_rejects_stale_issue_without_mutation(self, mock_post):
        mock_post.return_value = FakeLinearResponse({"data": {"issue": {
            "id": "issue-29", "identifier": "TECH-29", "updatedAt": "new-version",
            "archivedAt": None, "team": {"id": "team-tech"}, "state": {"id": "state-todo"},
            "labels": {"nodes": []}, "attachments": {"nodes": [], "pageInfo": {}},
            "relations": {"nodes": [], "pageInfo": {}}, "inverseRelations": {"nodes": [], "pageInfo": {}},
        }}})

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "issue_identifier": "TECH-29",
                "operation": "set_title", "value": "New title",
                "expected_updated_at": "old-version", "request_id": "Ev124",
            }, format="json", **self.auth_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "linear_channel_issue_stale")
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=False)
    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_immediate_write_kill_switch_makes_no_linear_call(self, mock_post):
        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "issue_identifier": "TECH-29",
                "operation": "set_title", "value": "New title",
                "expected_updated_at": "version", "request_id": "Ev125",
            }, format="json", **self.auth_headers,
        )
        self.assertEqual(response.status_code, 403)
        mock_post.assert_not_called()

    def test_immediate_write_rejects_arbitrary_fields(self):
        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/write",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "issue_identifier": "TECH-29",
                "operation": "set_title", "value": "New title",
                "expected_updated_at": "version", "request_id": "Ev126",
                "team_id": "team-other",
            }, format="json", **self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported Linear write fields", response.json()["detail"])

    @patch.object(
        ScopedRateThrottle,
        "THROTTLE_RATES",
        {
            "linear_channel_issue_list": "1/minute",
            "linear_channel_issue_detail": "1/minute",
        },
    )
    @patch(
        "integrations.api_views_connectors.list_linear_channel_issues",
        return_value={"list": {}, "issues": [], "pageInfo": {}},
    )
    def test_channel_issue_list_is_throttled(self, mock_list):
        cache.clear()
        self.addCleanup(cache.clear)
        request = {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
            "requester_slack_id": "U123",
        }

        first = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            request,
            format="json",
            REMOTE_ADDR="192.0.2.10",
            **self.auth_headers,
        )
        second = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            request,
            format="json",
            REMOTE_ADDR="192.0.2.10",
            **self.auth_headers,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(mock_list.call_count, 1)

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

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_list_all_statuses_uses_team_only(self, mock_post):
        mock_post.return_value = FakeLinearResponse({"data": {"issues": {
            "nodes": [{
                "id": "issue-29", "identifier": "TECH-29", "title": "In progress",
                "archivedAt": None, "state": {"id": "state-progress", "name": "In Progress"},
                "team": {"id": "team-tech", "name": "MLAI_TECH"},
            }], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}})

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123", "status": "all",
            }, format="json", **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        request_payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("stateId", request_payload["variables"])
        self.assertEqual(response.json()["list"]["stateName"], "All statuses")
        self.assertEqual(response.json()["list"]["displayName"], "MLAI_TECH · All statuses")

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_list_drops_archived_nodes(self, mock_post):
        mock_post.return_value = FakeLinearResponse({"data": {"issues": {
            "nodes": [{
                "id": "issue-29", "identifier": "TECH-29", "title": "Archived",
                "archivedAt": "2026-08-31T00:00:00Z",
                "state": {"id": "state-todo", "name": "Todo"},
                "team": {"id": "team-tech", "name": "MLAI_TECH"},
            }], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}})

        response = self.client.post(
            "/api/v1/integrations/linear/channel-issues/list",
            {
                "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
                "requester_slack_id": "U123",
            }, format="json", **self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["issues"], [])

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
                                "nodes": [
                                    {
                                        "type": "blocks",
                                        "relatedIssue": {
                                            "id": "issue-17",
                                            "identifier": "TECH-17",
                                            "title": "Allowed relation",
                                            "state": {"id": "state-todo", "name": "Todo"},
                                            "team": {"id": "team-tech", "name": "MLAI_TECH"},
                                        },
                                    },
                                    {
                                        "type": "related",
                                        "relatedIssue": {
                                            "id": "issue-18",
                                            "identifier": "TECH-18",
                                            "title": "Wrong state relation",
                                            "state": {"id": "state-progress", "name": "In Progress"},
                                            "team": {"id": "team-tech", "name": "MLAI_TECH"},
                                        },
                                    },
                                    {
                                        "type": "related",
                                        "relatedIssue": {
                                            "id": "issue-other",
                                            "identifier": "MLAI-1",
                                            "title": "Wrong team relation",
                                            "state": {"id": "state-todo", "name": "Todo"},
                                            "team": {"id": "team-other", "name": "MLAI"},
                                        },
                                    },
                                ],
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
        self.assertEqual(
            [edge["issue"]["identifier"] for edge in payload["issue"]["relations"]["edges"]],
            ["TECH-17", "TECH-18"],
        )
        self.assertEqual(payload["issue"]["relations"]["returned"], 2)
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

    @patch("integrations.services.linear_meeting_actions.http_requests.post")
    def test_channel_issue_detail_permits_other_status_in_bound_team(self, mock_post):
        mock_post.return_value = FakeLinearResponse(
            {
                "data": {
                    "issue": {
                        "id": "issue-16",
                        "identifier": "TECH-16",
                        "title": "Already in progress",
                        "state": {"id": "state-progress", "name": "In Progress"},
                        "team": {"id": "team-tech", "name": "MLAI_TECH"},
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
                "issue_identifier": "TECH-16",
                "include_comments": False,
            },
            format="json",
            **self.auth_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["issue"]["state"]["name"], "In Progress")
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
