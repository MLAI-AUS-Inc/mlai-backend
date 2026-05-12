from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from integrations import http_client as http_requests


LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
logger = logging.getLogger(__name__)


class LinearMeetingConfigurationError(Exception):
    pass


class LinearMeetingGraphQLError(Exception):
    def __init__(self, message: str, *, operation: str | None = None):
        self.operation = operation
        super().__init__(message)


class LinearMeetingRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int = 1):
        self.retry_after_seconds = max(int(retry_after_seconds or 1), 1)
        super().__init__(f"Linear rate limit exceeded; retry after {self.retry_after_seconds}s.")


def get_linear_meeting_context() -> dict[str, Any]:
    teams = list_teams()
    return {
        "teams": teams,
        "users": list_users(),
        "projects": list_active_projects(teams=teams),
        "labels": list_issue_labels(),
        "recentIssues": list_recent_open_issues(),
    }


def list_teams(limit: int = 100, member_limit: int = 50) -> list[dict[str, Any]]:
    member_limit = max(min(int(member_limit or 50), 50), 1)
    query = """
    query LinearTeamsWithMembers($first: Int!, $memberFirst: Int!) {
      teams(first: $first) {
        nodes {
          id
          key
          name
          members(first: $memberFirst) {
            nodes {
              id
              name
              displayName
              email
              active
            }
          }
        }
      }
    }
    """
    try:
        data = _graphql(
            query,
            {"first": limit, "memberFirst": member_limit},
            operation_name="LinearTeamsWithMembers",
        )
        return _nodes(data, "teams")
    except LinearMeetingGraphQLError as exc:
        if not _team_members_query_unsupported(exc):
            raise
        logger.warning(
            "linear_meeting_actions_team_members_unavailable operation=%s detail=%s",
            exc.operation,
            str(exc),
        )

    query = """
    query LinearTeams($first: Int!) {
      teams(first: $first) {
        nodes {
          id
          key
          name
        }
      }
    }
    """
    data = _graphql(query, {"first": limit}, operation_name="LinearTeams")
    return _nodes(data, "teams")


def list_users(limit: int = 250) -> list[dict[str, Any]]:
    query = """
    query LinearUsers($first: Int!) {
      users(first: $first) {
        nodes {
          id
          name
          displayName
          email
          active
        }
      }
    }
    """
    data = _graphql(query, {"first": limit}, operation_name="LinearUsers")
    return [user for user in _nodes(data, "users") if user.get("active") is not False]


def list_active_projects(limit: int = 100, teams: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    query = """
    query LinearProjects($first: Int!) {
      projects(first: $first) {
        nodes {
          id
          name
          slugId
          url
          completedAt
          canceledAt
          status {
            name
            type
          }
          lead {
            id
            name
            displayName
            email
          }
          teams {
            nodes {
              id
              key
              name
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"first": limit}, operation_name="LinearProjects")
    inactive_states = {"completed", "canceled", "cancelled", "archived"}
    active_projects = [
        project
        for project in _nodes(data, "projects")
        if not _project_is_inactive(project, inactive_states)
    ]
    return _enrich_projects_with_members(active_projects, teams or [])


def list_issue_labels(limit: int = 100) -> list[dict[str, Any]]:
    query = """
    query LinearIssueLabels($first: Int!) {
      issueLabels(first: $first) {
        nodes {
          id
          name
        }
      }
    }
    """
    data = _graphql(query, {"first": limit}, operation_name="LinearIssueLabels")
    return _nodes(data, "issueLabels")


def list_recent_open_issues(limit: int = 100) -> list[dict[str, Any]]:
    query = """
    query LinearRecentIssues($first: Int!) {
      issues(first: $first) {
        nodes {
          id
          identifier
          title
          url
          state {
            name
            type
          }
          project {
            id
            name
          }
          assignee {
            id
            name
            displayName
            email
          }
        }
      }
    }
    """
    data = _graphql(query, {"first": limit}, operation_name="LinearRecentIssues")
    closed_types = {"completed", "canceled", "cancelled"}
    return [
        issue
        for issue in _nodes(data, "issues")
        if str((issue.get("state") or {}).get("type") or "").lower() not in closed_types
    ]


def create_linear_meeting_issue(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    team_id = str(payload.get("team_id") or "").strip()
    if not title:
        raise ValueError("title is required.")
    if not team_id:
        raise ValueError("team_id is required.")

    input_data: dict[str, Any] = {
        "title": title,
        "teamId": team_id,
    }
    _copy_non_empty(payload, input_data, "description", "description")
    _copy_non_empty(payload, input_data, "assignee_id", "assigneeId")
    _copy_non_empty(payload, input_data, "project_id", "projectId")
    _copy_non_empty(payload, input_data, "due_date", "dueDate")

    priority = payload.get("priority")
    if priority not in (None, ""):
        try:
            input_data["priority"] = int(priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("priority must be an integer.") from exc

    raw_label_ids = payload.get("label_ids") or []
    if isinstance(raw_label_ids, str):
        raw_label_ids = [raw_label_ids]
    label_ids = [
        str(label_id).strip()
        for label_id in raw_label_ids
        if str(label_id).strip()
    ]
    if label_ids:
        input_data["labelIds"] = label_ids

    mutation = """
    mutation CreateLinearMeetingIssue($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue {
          id
          identifier
          title
          url
        }
      }
    }
    """
    data = _graphql(mutation, {"input": input_data}, operation_name="CreateLinearMeetingIssue")
    result = data.get("issueCreate") if isinstance(data.get("issueCreate"), dict) else {}
    if not result.get("success"):
        raise LinearMeetingGraphQLError(
            "Linear issueCreate returned success=false.",
            operation="CreateLinearMeetingIssue",
        )
    issue = result.get("issue")
    return issue if isinstance(issue, dict) else {}


def create_linear_meeting_project_update(payload: dict[str, Any]) -> dict[str, Any]:
    project_id = str(payload.get("project_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not project_id:
        raise ValueError("project_id is required.")
    if not body:
        raise ValueError("body is required.")

    health = str(payload.get("health") or "onTrack").strip()
    valid_health_values = {"onTrack", "atRisk", "offTrack"}
    if health not in valid_health_values:
        raise ValueError("health must be one of: onTrack, atRisk, offTrack.")

    mutation = """
    mutation CreateLinearMeetingProjectUpdate($input: ProjectUpdateCreateInput!) {
      projectUpdateCreate(input: $input) {
        success
        projectUpdate {
          id
          url
          body
          health
          createdAt
          project {
            id
            name
          }
        }
      }
    }
    """
    data = _graphql(
        mutation,
        {"input": {"projectId": project_id, "body": body, "health": health}},
        operation_name="CreateLinearMeetingProjectUpdate",
    )
    result = data.get("projectUpdateCreate") if isinstance(data.get("projectUpdateCreate"), dict) else {}
    if not result.get("success"):
        raise LinearMeetingGraphQLError(
            "Linear projectUpdateCreate returned success=false.",
            operation="CreateLinearMeetingProjectUpdate",
        )
    project_update = result.get("projectUpdate")
    return project_update if isinstance(project_update, dict) else {}


def _copy_non_empty(source: dict[str, Any], target: dict[str, Any], source_key: str, target_key: str) -> None:
    value = source.get(source_key)
    if value not in (None, ""):
        target[target_key] = value


def _team_members_query_unsupported(exc: LinearMeetingGraphQLError) -> bool:
    message = str(exc).lower()
    return (
        "query too complex" in message
        or (
            "members" in message
            and (
                "cannot query field" in message
                or "unknown argument" in message
                or "field" in message
            )
        )
    )


def _project_is_inactive(project: dict[str, Any], inactive_states: set[str]) -> bool:
    if project.get("completedAt") or project.get("canceledAt"):
        return True
    status_data = project.get("status") if isinstance(project.get("status"), dict) else {}
    status_name = str(status_data.get("name") or "").lower()
    status_type = str(status_data.get("type") or "").lower()
    return status_name in inactive_states or status_type in inactive_states


def _enrich_projects_with_members(
    projects: list[dict[str, Any]],
    teams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    team_members = _team_members_by_id(teams)
    enriched_projects: list[dict[str, Any]] = []
    for project in projects:
        enriched_project = dict(project)
        members: list[dict[str, Any]] = []
        project_teams = _nodes(project, "teams")
        for team in project_teams:
            members.extend(team_members.get(str(team.get("id") or ""), []))

        lead = project.get("lead") if isinstance(project.get("lead"), dict) else None
        if lead:
            members.append(lead)
        enriched_project["members"] = {"nodes": _dedupe_users(members)}
        enriched_projects.append(enriched_project)
    return enriched_projects


def _team_members_by_id(teams: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for team in teams:
        team_id = str(team.get("id") or "")
        if not team_id:
            continue
        mapping[team_id] = [
            member
            for member in _nodes(team, "members")
            if member.get("active") is not False
        ]
    return mapping


def _dedupe_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for user in users:
        if not isinstance(user, dict):
            continue
        key = str(user.get("id") or user.get("email") or user.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(user)
    return deduped


def _graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    operation_name: str,
) -> dict[str, Any]:
    api_key = _linear_api_key()
    connect_timeout = float(getattr(settings, "LINEAR_API_CONNECT_TIMEOUT_SECONDS", 3) or 3)
    read_timeout = float(getattr(settings, "LINEAR_API_READ_TIMEOUT_SECONDS", 20) or 20)

    try:
        response = http_requests.post(
            LINEAR_GRAPHQL_URL,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": variables or {},
                "operationName": operation_name,
            },
            timeout=(connect_timeout, read_timeout),
        )
        if response.status_code == 429:
            raise LinearMeetingRateLimitError(_retry_after_seconds(response))
    except LinearMeetingRateLimitError:
        raise
    except http_requests.RequestException as exc:
        raise LinearMeetingGraphQLError(
            f"Linear GraphQL request failed: {exc}",
            operation=operation_name,
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        try:
            response.raise_for_status()
        except http_requests.RequestException as status_exc:
            raise LinearMeetingGraphQLError(
                f"Linear GraphQL request failed: {status_exc}",
                operation=operation_name,
            ) from status_exc
        raise LinearMeetingGraphQLError(
            "Linear GraphQL returned an invalid JSON response.",
            operation=operation_name,
        ) from exc
    if not isinstance(payload, dict):
        raise LinearMeetingGraphQLError(
            "Linear GraphQL returned an invalid response.",
            operation=operation_name,
        )

    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        message = _graphql_error_message(errors)
        if _graphql_errors_are_rate_limited(errors, message):
            raise LinearMeetingRateLimitError(1)
        raise LinearMeetingGraphQLError(message, operation=operation_name)

    try:
        response.raise_for_status()
    except http_requests.RequestException as exc:
        raise LinearMeetingGraphQLError(
            f"Linear GraphQL request failed: {exc}",
            operation=operation_name,
        ) from exc

    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _linear_api_key() -> str:
    api_key = str(getattr(settings, "LINEAR_API_KEY", "") or "").strip()
    if not api_key:
        raise LinearMeetingConfigurationError(
            "Backend Linear meeting actions are not configured. Set LINEAR_API_KEY on mlai-backend."
        )
    return api_key


def _graphql_error_message(errors: list[Any]) -> str:
    messages: list[str] = []
    for error in errors:
        if isinstance(error, dict):
            messages.append(str(error.get("message") or error))
        else:
            messages.append(str(error))
    return "; ".join(message for message in messages if message) or "Linear GraphQL request failed."


def _graphql_errors_are_rate_limited(errors: list[Any], message: str) -> bool:
    if "rate limit" in message.lower() or "rate_limit" in message.lower():
        return True
    for error in errors:
        if not isinstance(error, dict):
            continue
        extension = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
        http_extension = extension.get("http") if isinstance(extension.get("http"), dict) else {}
        status_value = extension.get("status") or http_extension.get("status")
        code = str(extension.get("code") or "").lower()
        if str(status_value) == "429" or "rate" in code:
            return True
    return False


def _retry_after_seconds(response) -> int:
    raw_retry_after = str(response.headers.get("Retry-After") or "").strip()
    if raw_retry_after:
        try:
            return max(int(float(raw_retry_after)), 1)
        except (TypeError, ValueError):
            pass
    raw_reset = str(response.headers.get("X-RateLimit-Requests-Reset") or "").strip()
    if raw_reset:
        try:
            reset_value = float(raw_reset)
            if reset_value > 10_000_000_000:
                reset_value = reset_value / 1000
            return max(int(reset_value - timezone.now().timestamp()), 1)
        except (TypeError, ValueError):
            pass
    return 1


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]
