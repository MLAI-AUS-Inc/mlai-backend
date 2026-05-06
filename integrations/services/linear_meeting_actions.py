from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from integrations import http_client as http_requests


LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearMeetingConfigurationError(Exception):
    pass


class LinearMeetingGraphQLError(Exception):
    pass


class LinearMeetingRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int = 1):
        self.retry_after_seconds = max(int(retry_after_seconds or 1), 1)
        super().__init__(f"Linear rate limit exceeded; retry after {self.retry_after_seconds}s.")


def get_linear_meeting_context() -> dict[str, Any]:
    return {
        "teams": list_teams(),
        "users": list_users(),
        "projects": list_active_projects(),
        "labels": list_issue_labels(),
        "recentIssues": list_recent_open_issues(),
    }


def list_teams(limit: int = 100) -> list[dict[str, Any]]:
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
    data = _graphql(query, {"first": limit})
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
    data = _graphql(query, {"first": limit})
    return [user for user in _nodes(data, "users") if user.get("active") is not False]


def list_active_projects(limit: int = 100) -> list[dict[str, Any]]:
    query = """
    query LinearProjects($first: Int!) {
      projects(first: $first) {
        nodes {
          id
          name
          slugId
          url
          state
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
          members {
            nodes {
              id
              name
              displayName
              email
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"first": limit})
    inactive_states = {"completed", "canceled", "cancelled", "archived"}
    return [
        project
        for project in _nodes(data, "projects")
        if str(project.get("state") or "").lower() not in inactive_states
    ]


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
    data = _graphql(query, {"first": limit})
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
    data = _graphql(query, {"first": limit})
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
    data = _graphql(mutation, {"input": input_data})
    result = data.get("issueCreate") if isinstance(data.get("issueCreate"), dict) else {}
    if not result.get("success"):
        raise LinearMeetingGraphQLError("Linear issueCreate returned success=false.")
    issue = result.get("issue")
    return issue if isinstance(issue, dict) else {}


def _copy_non_empty(source: dict[str, Any], target: dict[str, Any], source_key: str, target_key: str) -> None:
    value = source.get(source_key)
    if value not in (None, ""):
        target[target_key] = value


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
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
            json={"query": query, "variables": variables or {}},
            timeout=(connect_timeout, read_timeout),
        )
        if response.status_code == 429:
            raise LinearMeetingRateLimitError(_retry_after_seconds(response))
        response.raise_for_status()
    except LinearMeetingRateLimitError:
        raise
    except http_requests.RequestException as exc:
        raise LinearMeetingGraphQLError(f"Linear GraphQL request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise LinearMeetingGraphQLError("Linear GraphQL returned an invalid JSON response.") from exc
    if not isinstance(payload, dict):
        raise LinearMeetingGraphQLError("Linear GraphQL returned an invalid response.")

    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        message = _graphql_error_message(errors)
        if _graphql_errors_are_rate_limited(errors, message):
            raise LinearMeetingRateLimitError(1)
        raise LinearMeetingGraphQLError(message)

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
