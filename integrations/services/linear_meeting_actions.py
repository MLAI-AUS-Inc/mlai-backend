from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import http_client as http_requests
from integrations.models import LinearIssueCreationReceipt


LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
STUDIO_PROJECT_PREFIX = "[Studio]"
STUDIO_EFFORT_LABELS = (
    "Extra Small (XS)",
    "Small (S)",
    "Medium (M)",
    "Large (L)",
    "Extra Large (XL)",
)
TERMINAL_WORKFLOW_TYPES = {"completed", "canceled", "cancelled", "duplicate"}
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


class LinearMeetingIdempotencyConflictError(Exception):
    pass


class LinearMeetingSizingConflictError(Exception):
    pass


def get_linear_meeting_context() -> dict[str, Any]:
    teams = list_teams()
    users = list_users()
    projects = list_active_projects(teams=teams)
    labels = list_issue_labels()
    recent_issues = list_recent_open_issues()
    projects = _enrich_projects_with_recent_issues(projects, recent_issues)
    return {
        "teams": teams,
        "users": users,
        "projects": projects,
        "labels": labels,
        "recentIssues": recent_issues,
    }


def get_linear_project_sizing_context(
    project_id: str,
    *,
    update_limit: int = 5,
    active_issue_limit: int = 40,
    terminal_issue_limit: int = 10,
    precedent_limit: int = 20,
) -> dict[str, Any]:
    """Return bounded project evidence for Roo's Studio effort estimator."""
    project_id = str(project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required.")
    update_limit = _bounded_limit(update_limit, default=5, maximum=10)
    active_issue_limit = _bounded_limit(active_issue_limit, default=40, maximum=60)
    terminal_issue_limit = _bounded_limit(terminal_issue_limit, default=10, maximum=20)
    precedent_limit = _bounded_limit(precedent_limit, default=20, maximum=30)
    issue_limit = min(active_issue_limit + terminal_issue_limit + precedent_limit, 100)

    project, relations_available = _fetch_linear_project_sizing_detail(
        project_id,
        update_limit=update_limit,
        issue_limit=issue_limit,
    )
    if not project:
        raise ValueError("Linear project was not found.")

    raw_updates = _connection_nodes(project.get("projectUpdates"))
    raw_issues = _connection_nodes(project.get("issues"))
    updates = raw_updates[:update_limit]
    active_issues: list[dict[str, Any]] = []
    terminal_issues: list[dict[str, Any]] = []
    precedents: list[dict[str, Any]] = []
    for issue in raw_issues:
        normalized = _normalize_sizing_issue(issue, relations_available=relations_available)
        workflow_type = str(((issue.get("state") or {}).get("type")) or "").lower()
        effort_labels = [
            label
            for label in _connection_nodes(issue.get("labels"))
            if str(label.get("name") or "") in STUDIO_EFFORT_LABELS
        ]
        if workflow_type in TERMINAL_WORKFLOW_TYPES:
            if len(terminal_issues) < terminal_issue_limit:
                terminal_issues.append(normalized)
        elif len(active_issues) < active_issue_limit:
            active_issues.append(normalized)
        if effort_labels and len(precedents) < precedent_limit:
            precedents.append(normalized)

    issue_page_info = _page_info(project.get("issues"))
    update_page_info = _page_info(project.get("projectUpdates"))
    issue_source_truncated = bool(issue_page_info.get("hasNextPage")) or len(raw_issues) >= issue_limit
    label_registry = list_issue_labels()
    effort_label_registry = [
        label for label in label_registry if str(label.get("name") or "") in STUDIO_EFFORT_LABELS
    ]
    return {
        "project": {
            key: value
            for key, value in project.items()
            if key not in {"projectUpdates", "issues"}
        },
        "projectUpdates": _bounded_section(
            updates,
            requested=update_limit,
            source_page_info=update_page_info,
            source_count=len(raw_updates),
        ),
        "activeIssues": _bounded_section(
            active_issues,
            requested=active_issue_limit,
            source_page_info=issue_page_info,
            source_count=len(raw_issues),
            source_truncated=issue_source_truncated,
        ),
        "terminalReferences": _bounded_section(
            terminal_issues,
            requested=terminal_issue_limit,
            source_page_info=issue_page_info,
            source_count=len(raw_issues),
            source_truncated=issue_source_truncated,
        ),
        "sizingPrecedents": _bounded_section(
            precedents,
            requested=precedent_limit,
            source_page_info=issue_page_info,
            source_count=len(raw_issues),
            source_truncated=issue_source_truncated,
        ),
        "effortLabelRegistry": {
            "nodes": effort_label_registry,
            "returned": len(effort_label_registry),
            "expectedNames": list(STUDIO_EFFORT_LABELS),
            "complete": {str(item.get("name") or "") for item in effort_label_registry}
            == set(STUDIO_EFFORT_LABELS),
        },
        "relationsAvailable": relations_available,
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
    page_size = _bounded_limit(limit, default=100, maximum=100)
    query = """
    query LinearProjects($first: Int!, $after: String) {
      projects(first: $first, after: $after) {
        nodes {
          id
          name
          slugId
          url
          description
          content
          slackChannelId
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
          lastUpdate {
            id
            url
            body
            health
            createdAt
            updatedAt
            user {
              id
              name
              displayName
              email
            }
          }
          teams {
            nodes {
              id
              key
              name
            }
          }
          members(first: 50) {
            nodes {
              id
              name
              displayName
              email
              active
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    projects: list[dict[str, Any]] = []
    cursor: str | None = None
    use_basic_query = False
    for _page_number in range(20):
        variables = {"first": page_size, "after": cursor}
        try:
            data = _graphql(
                _basic_projects_query() if use_basic_query else query,
                variables,
                operation_name="LinearProjectsBasic" if use_basic_query else "LinearProjects",
            )
        except LinearMeetingGraphQLError as exc:
            if use_basic_query or not _project_context_query_unsupported(exc):
                raise
            logger.warning(
                "linear_meeting_actions_project_context_unavailable operation=%s detail=%s",
                exc.operation,
                str(exc),
            )
            use_basic_query = True
            cursor = None
            projects = []
            continue
        projects.extend(_nodes(data, "projects"))
        page_info = _page_info(data.get("projects"))
        cursor = str(page_info.get("endCursor") or "").strip() or None
        if not page_info.get("hasNextPage") or not cursor:
            break
    inactive_states = {"completed", "canceled", "cancelled", "archived"}
    active_projects = [
        project
        for project in projects
        if not _project_is_inactive(project, inactive_states)
    ]
    return _enrich_projects_with_members(active_projects, teams or [])


def list_issue_labels(limit: int = 100) -> list[dict[str, Any]]:
    page_size = _bounded_limit(limit, default=100, maximum=100)
    query = """
    query LinearIssueLabels($first: Int!, $after: String) {
      issueLabels(first: $first, after: $after) {
        nodes {
          id
          name
          color
          archivedAt
          team { id key name }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    basic_query = """
    query LinearIssueLabelsBasic($first: Int!, $after: String) {
      issueLabels(first: $first, after: $after) {
        nodes { id name }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    labels: list[dict[str, Any]] = []
    cursor: str | None = None
    use_basic_query = False
    for _page_number in range(20):
        try:
            data = _graphql(
                basic_query if use_basic_query else query,
                {"first": page_size, "after": cursor},
                operation_name="LinearIssueLabelsBasic" if use_basic_query else "LinearIssueLabels",
            )
        except LinearMeetingGraphQLError as exc:
            message = str(exc).lower()
            if use_basic_query or not any(
                field in message for field in ("archivedat", "team", "color")
            ):
                raise
            logger.warning("linear_meeting_actions_label_metadata_unavailable detail=%s", str(exc))
            use_basic_query = True
            cursor = None
            labels = []
            continue
        labels.extend(_nodes(data, "issueLabels"))
        page_info = _page_info(data.get("issueLabels"))
        cursor = str(page_info.get("endCursor") or "").strip() or None
        if not page_info.get("hasNextPage") or not cursor:
            break
    return labels


def list_recent_open_issues(limit: int = 100) -> list[dict[str, Any]]:
    query = """
    query LinearRecentIssues($first: Int!) {
      issues(first: $first, orderBy: updatedAt) {
        nodes {
          id
          identifier
          title
          description
          updatedAt
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
    payload = dict(payload)
    title = str(payload.get("title") or "").strip()
    team_id = str(payload.get("team_id") or "").strip()
    if not title:
        raise ValueError("title is required.")
    if not team_id:
        raise ValueError("team_id is required.")

    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    receipt = None
    if idempotency_key:
        if not re.fullmatch(r"[A-Za-z0-9:_-]{16,64}", idempotency_key):
            raise ValueError("idempotency_key must be 16-64 safe characters.")
        receipt, replay, claimed_payload = _claim_linear_issue_receipt(idempotency_key, payload)
        if replay is not None:
            return {**replay, "idempotentReplay": True}
        payload = claimed_payload

    try:
        title = str(payload.get("title") or "").strip()
        team_id = str(payload.get("team_id") or "").strip()
        if not title or not team_id:
            raise ValueError("Stored idempotent Linear issue payload is invalid.")

        enforcement = _enforce_studio_sizing(payload)
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
        issue_payload = issue if isinstance(issue, dict) else {}
        sizing_metadata = payload.get("sizing_metadata")
        if isinstance(sizing_metadata, dict):
            issue_payload = {**issue_payload, "sizingMetadata": dict(sizing_metadata)}
        if enforcement:
            issue_payload = {**issue_payload, "sizingEnforcement": enforcement}
    except Exception as exc:
        _fail_linear_issue_receipt(receipt, exc)
        raise
    if receipt is not None:
        LinearIssueCreationReceipt.objects.filter(pk=receipt.pk).update(
            status=LinearIssueCreationReceipt.Status.COMPLETED,
            linear_issue_payload=issue_payload,
            last_error="",
            updated_at=timezone.now(),
        )
    return issue_payload


def get_linear_issue_receipt(idempotency_key: str) -> dict[str, Any]:
    idempotency_key = str(idempotency_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9:_-]{16,64}", idempotency_key):
        raise ValueError("idempotency_key must be 16-64 safe characters.")
    try:
        receipt = LinearIssueCreationReceipt.objects.get(idempotency_key=idempotency_key)
    except LinearIssueCreationReceipt.DoesNotExist:
        return {"status": "not_found", "idempotencyKey": idempotency_key}
    issue_payload = (
        dict(receipt.linear_issue_payload)
        if isinstance(receipt.linear_issue_payload, dict)
        else {}
    )
    sizing_metadata = issue_payload.get("sizingMetadata")
    if not isinstance(sizing_metadata, dict):
        request_payload = (
            receipt.request_payload if isinstance(receipt.request_payload, dict) else {}
        )
        raw_sizing_metadata = request_payload.get("sizing_metadata")
        sizing_metadata = (
            dict(raw_sizing_metadata) if isinstance(raw_sizing_metadata, dict) else None
        )
    return {
        "status": receipt.status,
        "idempotencyKey": receipt.idempotency_key,
        "issue": issue_payload or None,
        "sizingMetadata": sizing_metadata,
        "lastError": receipt.last_error if receipt.status == receipt.Status.FAILED else "",
        "updatedAt": receipt.updated_at.isoformat(),
    }


def _enforce_studio_sizing(payload: dict[str, Any]) -> dict[str, Any] | None:
    mode = str(
        getattr(settings, "LINEAR_STUDIO_SIZING_ENFORCEMENT_MODE", "off") or "off"
    ).strip().lower()
    if mode not in {"off", "audit", "required"}:
        logger.error("linear_studio_sizing_invalid_enforcement_mode mode=%s", mode)
        mode = "off"
    if mode == "off":
        return None

    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        return {"mode": mode, "studioProject": False, "valid": True}
    try:
        project = _get_linear_project_identity(project_id)
    except Exception as exc:
        if mode == "required":
            raise
        logger.warning(
            "linear_studio_sizing_audit project_lookup_failed=true project_id=%s detail=%s",
            project_id,
            str(exc),
        )
        return {
            "mode": mode,
            "studioProject": None,
            "valid": False,
            "violations": ["Current project metadata could not be verified."],
        }
    if not project:
        if mode == "required":
            raise ValueError("Linear project was not found.")
        logger.warning(
            "linear_studio_sizing_audit project_not_found=true project_id=%s",
            project_id,
        )
        return {
            "mode": mode,
            "studioProject": None,
            "valid": False,
            "violations": ["Current project metadata could not be found."],
        }
    current_project_name = str(project.get("name") or "")
    sizing_metadata = payload.get("sizing_metadata")
    sizing_metadata = dict(sizing_metadata) if isinstance(sizing_metadata, dict) else {}
    assessed_project_name = str(
        sizing_metadata.get("projectNameAtAssessment")
        or sizing_metadata.get("project_name_at_assessment")
        or ""
    )
    assessed_project_id = str(
        sizing_metadata.get("projectId")
        or sizing_metadata.get("project_id")
        or ""
    )
    was_studio = assessed_project_name.startswith(STUDIO_PROJECT_PREFIX)
    is_studio = current_project_name.startswith(STUDIO_PROJECT_PREFIX)
    if was_studio and not is_studio:
        conflict = LinearMeetingSizingConflictError(
            "The project changed after the Studio effort preview; rerun the Slack command."
        )
        if mode == "required":
            raise conflict
        logger.warning(
            "linear_studio_sizing_audit stale_preview=true project_id=%s assessed_name=%r current_name=%r",
            project_id,
            assessed_project_name,
            current_project_name,
        )
    if not is_studio:
        return {"mode": mode, "studioProject": False, "valid": not was_studio}

    violations: list[str] = []
    if not str(payload.get("idempotency_key") or "").strip():
        violations.append("Studio issues require an idempotency_key.")
    if not sizing_metadata:
        violations.append("Studio issues require sizing_metadata.")
    if not assessed_project_id:
        violations.append("sizing_metadata must identify the assessed project.")
    elif assessed_project_id != project_id:
        violations.append("The effort assessment belongs to a different project.")
    if not assessed_project_name:
        violations.append("sizing_metadata must include the assessed project name.")
    elif assessed_project_name != current_project_name:
        violations.append("The Studio project name changed after the effort assessment.")
    if not str(
        sizing_metadata.get("rubricVersion")
        or sizing_metadata.get("rubric_version")
        or ""
    ).strip():
        violations.append("sizing_metadata must include the sizing rubric version.")

    effort_label = str(
        sizing_metadata.get("effortLabel")
        or sizing_metadata.get("effort_label")
        or ""
    )
    rationale = str(sizing_metadata.get("rationale") or "").strip()
    if effort_label not in STUDIO_EFFORT_LABELS:
        violations.append("sizing_metadata must contain one valid effort label.")
    if not _valid_effort_rationale(rationale):
        violations.append("sizing_metadata rationale must be one sentence of at most 280 characters.")

    try:
        labels = list_issue_labels()
    except Exception as exc:
        if mode == "required":
            raise
        logger.warning(
            "linear_studio_sizing_audit label_lookup_failed=true project_id=%s detail=%s",
            project_id,
            str(exc),
        )
        return {
            "mode": mode,
            "studioProject": True,
            "valid": False,
            "violations": [
                *violations,
                "Current effort label records could not be verified.",
            ],
        }
    raw_label_ids = payload.get("label_ids") or []
    if isinstance(raw_label_ids, str):
        raw_label_ids = [raw_label_ids]
    label_ids = {str(item).strip() for item in raw_label_ids if str(item).strip()}
    selected_effort_labels = [
        label
        for label in labels
        if str(label.get("id") or "") in label_ids
        and str(label.get("name") or "") in STUDIO_EFFORT_LABELS
    ]
    if len(selected_effort_labels) != 1:
        violations.append("Studio issues must include exactly one effort label.")
    elif str(selected_effort_labels[0].get("name") or "") != effort_label:
        violations.append("The applied effort label does not match sizing_metadata.")
    elif not _label_is_compatible_with_team(
        selected_effort_labels[0],
        str(payload.get("team_id") or ""),
    ):
        violations.append("The selected effort label is not compatible with the issue team.")

    if violations:
        detail = " ".join(violations)
        if mode == "required":
            raise ValueError(detail)
        logger.warning(
            "linear_studio_sizing_audit valid=false project_id=%s detail=%s",
            project_id,
            detail,
        )
        return {
            "mode": mode,
            "studioProject": True,
            "valid": False,
            "violations": violations,
        }
    return {"mode": mode, "studioProject": True, "valid": True}


def _get_linear_project_identity(project_id: str) -> dict[str, Any]:
    query = """
    query LinearProjectIdentity($id: String!) {
      project(id: $id) { id name }
    }
    """
    data = _graphql(
        query,
        {"id": project_id},
        operation_name="LinearProjectIdentity",
    )
    project = data.get("project")
    return project if isinstance(project, dict) else {}


def _label_is_compatible_with_team(label: dict[str, Any], team_id: str) -> bool:
    label_team = label.get("team")
    if not isinstance(label_team, dict) or not str(label_team.get("id") or ""):
        return True
    return str(label_team.get("id") or "") == str(team_id or "")


def _valid_effort_rationale(rationale: str) -> bool:
    if not rationale or len(rationale) > 280 or "\n" in rationale:
        return False
    sentence_endings = re.findall(r"[.!?](?:[\"')\]]+)?(?=\s|$)", rationale)
    return len(sentence_endings) <= 1


def _fail_linear_issue_receipt(
    receipt: LinearIssueCreationReceipt | None,
    exc: Exception,
) -> None:
    if receipt is None:
        return
    LinearIssueCreationReceipt.objects.filter(pk=receipt.pk).update(
        status=LinearIssueCreationReceipt.Status.FAILED,
        last_error=f"{exc.__class__.__name__}: {exc}"[:2000],
        updated_at=timezone.now(),
    )


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


def _fetch_linear_project_sizing_detail(
    project_id: str,
    *,
    update_limit: int,
    issue_limit: int,
) -> tuple[dict[str, Any], bool]:
    query_with_relations = """
    query LinearProjectSizingContext(
      $id: String!,
      $issueFirst: Int!,
      $updateFirst: Int!
    ) {
      project(id: $id) {
        id
        name
        description
        content
        createdAt
        updatedAt
        startDate
        targetDate
        startedAt
        completedAt
        canceledAt
        priority
        health
        progress
        scope
        url
        status { name type }
        lead { id name displayName email }
        teams(first: 10) { nodes { id key name } }
        projectUpdates(first: $updateFirst) {
          nodes {
            id body health createdAt updatedAt url
            user { id name displayName email }
          }
          pageInfo { hasNextPage endCursor }
        }
        issues(first: $issueFirst) {
          nodes {
            id identifier title description priority priorityLabel estimate dueDate
            createdAt updatedAt startedAt completedAt canceledAt url
            state { id name type }
            team { id key name }
            assignee { id name displayName email }
            labels(first: 20) { nodes { id name } }
            relations(first: 10) {
              nodes {
                type
                issue { id identifier title state { name type } }
                relatedIssue { id identifier title state { name type } }
              }
              pageInfo { hasNextPage endCursor }
            }
            inverseRelations(first: 10) {
              nodes {
                type
                issue { id identifier title state { name type } }
                relatedIssue { id identifier title state { name type } }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    variables = {
        "id": project_id,
        "issueFirst": issue_limit,
        "updateFirst": update_limit,
    }
    try:
        data = _graphql(
            query_with_relations,
            variables,
            operation_name="LinearProjectSizingContext",
        )
        project = data.get("project")
        return (project if isinstance(project, dict) else {}), True
    except LinearMeetingGraphQLError as exc:
        message = str(exc).lower()
        relation_error = "query too complex" in message or (
            any(
                field in message
                for field in ("relations", "inverserelations", "relatedissue", "issue")
            )
            and "cannot query field" in message
        )
        if not relation_error:
            raise
        logger.warning("linear_sizing_relations_unavailable detail=%s", str(exc))
    data = _graphql(
        _project_sizing_query_without_relations(),
        variables,
        operation_name="LinearProjectSizingContextBasic",
    )
    project = data.get("project")
    return (project if isinstance(project, dict) else {}), False


def _project_sizing_query_without_relations() -> str:
    return """
    query LinearProjectSizingContextBasic(
      $id: String!,
      $issueFirst: Int!,
      $updateFirst: Int!
    ) {
      project(id: $id) {
        id name description content createdAt updatedAt startDate targetDate
        startedAt completedAt canceledAt priority health progress scope url
        status { name type }
        lead { id name displayName email }
        teams(first: 10) { nodes { id key name } }
        projectUpdates(first: $updateFirst) {
          nodes {
            id body health createdAt updatedAt url
            user { id name displayName email }
          }
          pageInfo { hasNextPage endCursor }
        }
        issues(first: $issueFirst) {
          nodes {
            id identifier title description priority priorityLabel estimate dueDate
            createdAt updatedAt startedAt completedAt canceledAt url
            state { id name type }
            team { id key name }
            assignee { id name displayName email }
            labels(first: 20) { nodes { id name } }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """


def _normalize_sizing_issue(
    issue: dict[str, Any],
    *,
    relations_available: bool,
) -> dict[str, Any]:
    normalized = dict(issue)
    if not relations_available:
        normalized["relations"] = {
            "edges": [],
            "returned": 0,
            "hasMore": False,
            "truncated": False,
            "available": False,
        }
        return normalized

    edges: list[dict[str, Any]] = []
    for connection_name, inverse in (("relations", False), ("inverseRelations", True)):
        connection = issue.get(connection_name)
        for relation in _connection_nodes(connection):
            relation_type = str(relation.get("type") or "")
            if inverse and relation_type == "blocks":
                relation_type = "blocked-by"
            related_issue = relation.get("issue") if inverse else relation.get("relatedIssue")
            if not isinstance(related_issue, dict):
                related_issue = relation.get("relatedIssue") or relation.get("issue")
            edges.append(
                {
                    "type": relation_type,
                    "issue": related_issue if isinstance(related_issue, dict) else {},
                }
            )
    has_more = any(
        bool(_page_info(issue.get(name)).get("hasNextPage"))
        for name in ("relations", "inverseRelations")
    )
    normalized.pop("inverseRelations", None)
    normalized["relations"] = {
        "edges": edges,
        "returned": len(edges),
        "hasMore": has_more,
        "truncated": has_more,
        "available": True,
    }
    return normalized


def _bounded_section(
    nodes: list[dict[str, Any]],
    *,
    requested: int,
    source_page_info: dict[str, Any],
    source_count: int,
    source_truncated: bool | None = None,
) -> dict[str, Any]:
    truncated = (
        bool(source_page_info.get("hasNextPage"))
        if source_truncated is None
        else bool(source_truncated)
    )
    return {
        "nodes": nodes,
        "returned": len(nodes),
        "requested": requested,
        "sourceReturned": source_count,
        "pageInfo": {
            "hasNextPage": bool(source_page_info.get("hasNextPage")),
            "endCursor": source_page_info.get("endCursor"),
        },
        "truncated": truncated or len(nodes) >= requested,
    }


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _connection_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _page_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    page_info = value.get("pageInfo")
    return page_info if isinstance(page_info, dict) else {}


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


def _project_context_query_unsupported(exc: LinearMeetingGraphQLError) -> bool:
    message = str(exc).lower()
    contextual_fields = {"content", "description", "members", "slackchannelid"}
    return "query too complex" in message or (
        any(field in message for field in contextual_fields)
        and ("cannot query field" in message or "unknown argument" in message)
    )


def _basic_projects_query() -> str:
    return """
    query LinearProjectsBasic($first: Int!, $after: String) {
      projects(first: $first, after: $after) {
        nodes {
          id
          name
          slugId
          url
          completedAt
          canceledAt
          status { name type }
          lead { id name displayName email }
          lastUpdate {
            id url body health createdAt updatedAt
            user { id name displayName email }
          }
          teams { nodes { id key name } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """


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
        direct_members = [
            member
            for member in _nodes(project, "members")
            if member.get("active") is not False
        ]
        if direct_members:
            lead = project.get("lead") if isinstance(project.get("lead"), dict) else None
            participants = direct_members + ([lead] if lead else [])
            enriched_project["members"] = {"nodes": _dedupe_users(participants)}
            enriched_project["membersSource"] = "project"
            enriched_projects.append(enriched_project)
            continue
        members: list[dict[str, Any]] = []
        project_teams = _nodes(project, "teams")
        for team in project_teams:
            members.extend(team_members.get(str(team.get("id") or ""), []))

        lead = project.get("lead") if isinstance(project.get("lead"), dict) else None
        if lead:
            members.append(lead)
        enriched_project["members"] = {"nodes": _dedupe_users(members)}
        enriched_project["membersSource"] = "team_fallback"
        enriched_projects.append(enriched_project)
    return enriched_projects


def _enrich_projects_with_recent_issues(
    projects: list[dict[str, Any]],
    recent_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues_by_project: dict[str, list[dict[str, Any]]] = {}
    for issue in recent_issues:
        project_id = str(((issue.get("project") or {}).get("id")) or "")
        if not project_id:
            continue
        issues_by_project.setdefault(project_id, []).append(issue)

    enriched: list[dict[str, Any]] = []
    for project in projects:
        item = dict(project)
        item["recentIssues"] = issues_by_project.get(str(project.get("id") or ""), [])[:20]
        enriched.append(item)
    return enriched


def _claim_linear_issue_receipt(
    idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[LinearIssueCreationReceipt, dict[str, Any] | None, dict[str, Any]]:
    with transaction.atomic():
        receipt, created = LinearIssueCreationReceipt.objects.select_for_update().get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "status": LinearIssueCreationReceipt.Status.PENDING,
                "request_payload": dict(payload),
            },
        )
        if not created and receipt.status == LinearIssueCreationReceipt.Status.COMPLETED:
            replay = (
                dict(receipt.linear_issue_payload)
                if isinstance(receipt.linear_issue_payload, dict)
                else {}
            )
            return receipt, replay, dict(receipt.request_payload or {})
        if not created and receipt.status == LinearIssueCreationReceipt.Status.PENDING:
            pending_ttl_seconds = max(
                int(
                    getattr(
                        settings,
                        "LINEAR_STUDIO_RECEIPT_PENDING_TTL_SECONDS",
                        300,
                    )
                    or 300
                ),
                1,
            )
            stale_before = timezone.now() - timedelta(seconds=pending_ttl_seconds)
            if receipt.updated_at >= stale_before:
                raise LinearMeetingIdempotencyConflictError(
                    "An identical Linear issue creation is already in progress."
                )
            logger.warning(
                "linear_issue_receipt_reclaim_stale idempotency_key=%s age_seconds=%s",
                idempotency_key,
                int((timezone.now() - receipt.updated_at).total_seconds()),
            )
        if not created:
            receipt.status = LinearIssueCreationReceipt.Status.PENDING
            receipt.linear_issue_payload = {}
            receipt.last_error = ""
            receipt.save(
                update_fields=[
                    "status",
                    "linear_issue_payload",
                    "last_error",
                    "updated_at",
                ]
            )
        claimed_payload = (
            dict(receipt.request_payload)
            if isinstance(receipt.request_payload, dict)
            else dict(payload)
        )
        return receipt, None, claimed_payload


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
