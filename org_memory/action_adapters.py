from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol

from django.conf import settings

from .models import AgentActionType


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ActionAdapterError(RuntimeError):
    pass


class ActionExecutionUncertain(ActionAdapterError):
    """The provider may have committed the mutation; automatic retry is unsafe."""


@dataclass(frozen=True)
class ActionExecutionResult:
    result: dict
    reversal_payload: dict
    external_id: str = ""


class ActionAdapter(Protocol):
    action_type: str
    target_system: str

    def validate_payload(self, payload: Mapping) -> dict: ...

    def refresh_preconditions(self, proposal) -> dict: ...

    def execute(self, proposal) -> ActionExecutionResult: ...

    def validate_reversal(self, proposal) -> None: ...

    def reverse(self, proposal) -> dict: ...


def _object(value, *, field_name: str = "input_payload") -> dict:
    if not isinstance(value, Mapping):
        raise ActionAdapterError(f"{field_name} must be an object.")
    return dict(value)


def _strict_keys(payload: dict, allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ActionAdapterError(
            "Unsupported action fields: " + ", ".join(unknown) + "."
        )


def _text(
    payload: Mapping,
    key: str,
    *,
    required: bool = False,
    maximum: int,
) -> str:
    value = str(payload.get(key) or "").strip()
    if required and not value:
        raise ActionAdapterError(f"{key} is required.")
    if len(value) > maximum:
        raise ActionAdapterError(f"{key} is limited to {maximum} characters.")
    return value


def _string_list(payload: Mapping, key: str, *, maximum: int = 20) -> list[str]:
    value = payload.get(key) or []
    if not isinstance(value, list):
        raise ActionAdapterError(f"{key} must be a list.")
    normalized = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    if len(normalized) > maximum:
        raise ActionAdapterError(f"{key} is limited to {maximum} values.")
    return normalized


class DraftActionAdapter:
    def __init__(self, action_type: str):
        self.action_type = action_type
        self.target_system = {
            AgentActionType.DRAFT_GMAIL: "gmail",
            AgentActionType.DRAFT_SLACK_POST: "slack",
            AgentActionType.DRAFT_NOTION_UPDATE: "notion",
        }[action_type]

    def validate_payload(self, payload: Mapping) -> dict:
        value = _object(payload)
        if self.action_type == AgentActionType.DRAFT_GMAIL:
            _strict_keys(value, {"to", "cc", "bcc", "subject", "body"})
            to = _string_list(value, "to")
            cc = _string_list(value, "cc")
            bcc = _string_list(value, "bcc")
            if not to:
                raise ActionAdapterError("to must include at least one recipient.")
            if any(
                not EMAIL_PATTERN.fullmatch(address)
                for address in (*to, *cc, *bcc)
            ):
                raise ActionAdapterError("Email recipients must be valid addresses.")
            return {
                "to": to,
                "cc": cc,
                "bcc": bcc,
                "subject": _text(value, "subject", required=True, maximum=998),
                "body": _text(value, "body", required=True, maximum=50000),
            }
        if self.action_type == AgentActionType.DRAFT_SLACK_POST:
            _strict_keys(value, {"channel_id", "thread_ts", "text"})
            return {
                "channel_id": _text(
                    value, "channel_id", required=True, maximum=32
                ),
                "thread_ts": _text(value, "thread_ts", maximum=64),
                "text": _text(value, "text", required=True, maximum=40000),
            }
        if self.action_type == AgentActionType.DRAFT_NOTION_UPDATE:
            _strict_keys(value, {"page_id", "title", "body"})
            return {
                "page_id": _text(value, "page_id", required=True, maximum=255),
                "title": _text(value, "title", required=True, maximum=2000),
                "body": _text(value, "body", required=True, maximum=50000),
            }
        raise ActionAdapterError("Unsupported draft action type.")

    def refresh_preconditions(self, proposal) -> dict:
        return {
            "target_system": proposal.target_system,
            "mode": "local_draft",
            "external_write": False,
        }

    def execute(self, proposal) -> ActionExecutionResult:
        return ActionExecutionResult(
            result={
                "kind": "draft",
                "target_system": proposal.target_system,
                "draft": proposal.input_payload,
            },
            reversal_payload={},
        )

    def reverse(self, proposal) -> dict:
        raise ActionAdapterError("Local drafts do not require external reversal.")

    def validate_reversal(self, proposal) -> None:
        raise ActionAdapterError("Local drafts do not require external reversal.")


LINEAR_CREATE_PRECONDITION_QUERY = """
query MLAIActionCreatePreconditions($teamId: String!, $projectId: String!) {
  team(id: $teamId) { id key name }
  project(id: $projectId) { id name updatedAt }
}
"""

LINEAR_CREATE_TEAM_PRECONDITION_QUERY = """
query MLAIActionCreateTeamPreconditions($teamId: String!) {
  team(id: $teamId) { id key name }
}
"""

LINEAR_ISSUE_PRECONDITION_QUERY = """
query MLAIActionIssuePreconditions($issueId: String!) {
  issue(id: $issueId) {
    id identifier title description updatedAt priority dueDate
    team { id }
    project { id }
    assignee { id }
    state { id }
    labels { nodes { id } }
  }
}
"""

LINEAR_CREATE_MUTATION = """
mutation MLAICreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title url updatedAt }
  }
}
"""

LINEAR_UPDATE_MUTATION = """
mutation MLAIUpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier title url updatedAt }
  }
}
"""

LINEAR_ARCHIVE_MUTATION = """
mutation MLAIArchiveIssue($id: String!) {
  issueArchive(id: $id) { success }
}
"""


class LinearActionAdapter:
    target_system = "linear"

    def __init__(self, action_type: str):
        self.action_type = action_type

    def validate_payload(self, payload: Mapping) -> dict:
        value = _object(payload)
        common = {
            "title",
            "description",
            "assignee_id",
            "project_id",
            "priority",
            "due_date",
            "label_ids",
            "state_id",
        }
        if self.action_type == AgentActionType.CREATE_LINEAR_ISSUE:
            _strict_keys(value, common | {"team_id"})
            normalized = {
                "team_id": _text(value, "team_id", required=True, maximum=255),
                "title": _text(value, "title", required=True, maximum=1000),
                "description": _text(value, "description", maximum=50000),
                "assignee_id": _text(value, "assignee_id", maximum=255),
                "project_id": _text(value, "project_id", maximum=255),
                "due_date": _text(value, "due_date", maximum=32),
                "label_ids": _string_list(value, "label_ids", maximum=50),
                "state_id": _text(value, "state_id", maximum=255),
            }
        elif self.action_type == AgentActionType.UPDATE_LINEAR_ISSUE:
            _strict_keys(value, common | {"issue_id", "team_id"})
            normalized = {
                "issue_id": _text(
                    value, "issue_id", required=True, maximum=255
                ),
                "team_id": _text(value, "team_id", maximum=255),
                "title": _text(value, "title", maximum=1000),
                "description": _text(value, "description", maximum=50000),
                "assignee_id": _text(value, "assignee_id", maximum=255),
                "project_id": _text(value, "project_id", maximum=255),
                "due_date": _text(value, "due_date", maximum=32),
                "label_ids": _string_list(value, "label_ids", maximum=50),
                "state_id": _text(value, "state_id", maximum=255),
            }
            if not any(
                normalized[key]
                for key in (
                    "team_id",
                    "title",
                    "description",
                    "assignee_id",
                    "project_id",
                    "due_date",
                    "label_ids",
                    "state_id",
                )
            ) and value.get("priority") in (None, ""):
                raise ActionAdapterError(
                    "A Linear issue update requires at least one changed field."
                )
        else:
            raise ActionAdapterError("Unsupported Linear action type.")

        priority = value.get("priority")
        if priority in (None, ""):
            normalized["priority"] = None
        else:
            try:
                normalized["priority"] = int(priority)
            except (TypeError, ValueError) as exc:
                raise ActionAdapterError("priority must be an integer.") from exc
            if normalized["priority"] not in {0, 1, 2, 3, 4}:
                raise ActionAdapterError("priority must be between 0 and 4.")
        return normalized

    def _graphql(
        self,
        proposal,
        query: str,
        variables: dict,
        *,
        mutation: bool = False,
    ) -> dict:
        if not getattr(settings, "ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED", False):
            raise ActionAdapterError("Linear action execution is disabled.")
        configuration = proposal.configuration
        connection = configuration.connection if configuration else None
        if connection is None:
            raise ActionAdapterError(
                "Linear actions require an active organization connection."
            )
        try:
            from integrations.services.external_connectors import (
                linear_graphql_request,
            )

            return linear_graphql_request(connection, query, variables)
        except ActionAdapterError:
            raise
        except Exception as exc:
            if mutation:
                raise ActionExecutionUncertain(
                    "Linear may have committed the mutation; reconcile provider state."
                ) from exc
            raise ActionAdapterError(
                "Linear action adapter could not validate the live target."
            ) from exc

    def refresh_preconditions(self, proposal) -> dict:
        payload = proposal.input_payload
        if self.action_type == AgentActionType.CREATE_LINEAR_ISSUE:
            project_id = payload.get("project_id") or ""
            data = self._graphql(
                proposal,
                (
                    LINEAR_CREATE_PRECONDITION_QUERY
                    if project_id
                    else LINEAR_CREATE_TEAM_PRECONDITION_QUERY
                ),
                (
                    {"teamId": payload["team_id"], "projectId": project_id}
                    if project_id
                    else {"teamId": payload["team_id"]}
                ),
            )
            team = data.get("team") if isinstance(data.get("team"), dict) else None
            project = (
                data.get("project")
                if isinstance(data.get("project"), dict)
                else None
            )
            if team is None:
                raise ActionAdapterError("The Linear team no longer exists or is inaccessible.")
            if project_id and project is None:
                raise ActionAdapterError(
                    "The Linear project no longer exists or is inaccessible."
                )
            return {
                "target_system": "linear",
                "operation": self.action_type,
                "team": {
                    "id": str(team.get("id") or ""),
                    "key": str(team.get("key") or ""),
                },
                "project": (
                    {
                        "id": str(project.get("id") or ""),
                        "updated_at": str(project.get("updatedAt") or ""),
                    }
                    if project
                    else None
                ),
            }

        data = self._graphql(
            proposal,
            LINEAR_ISSUE_PRECONDITION_QUERY,
            {"issueId": payload["issue_id"]},
        )
        issue = data.get("issue") if isinstance(data.get("issue"), dict) else None
        if issue is None:
            raise ActionAdapterError("The Linear issue no longer exists or is inaccessible.")
        return {
            "target_system": "linear",
            "operation": self.action_type,
            "issue": _linear_issue_snapshot(issue),
        }

    def execute(self, proposal) -> ActionExecutionResult:
        if self.action_type == AgentActionType.CREATE_LINEAR_ISSUE:
            input_payload = _linear_mutation_input(proposal.input_payload, create=True)
            data = self._graphql(
                proposal,
                LINEAR_CREATE_MUTATION,
                {"input": input_payload},
                mutation=True,
            )
            mutation = (
                data.get("issueCreate")
                if isinstance(data.get("issueCreate"), dict)
                else {}
            )
            if not mutation.get("success"):
                raise ActionAdapterError("Linear issue creation returned success=false.")
            issue = mutation.get("issue") if isinstance(mutation.get("issue"), dict) else {}
            issue_id = str(issue.get("id") or "")
            if not issue_id:
                raise ActionExecutionUncertain(
                    "Linear reported success without an issue identifier."
                )
            return ActionExecutionResult(
                result=_linear_result(issue),
                reversal_payload={"operation": "archive_issue", "issue_id": issue_id},
                external_id=f"linear_issue:{issue_id}",
            )

        previous = dict((proposal.precondition_snapshot or {}).get("issue") or {})
        input_payload = _linear_mutation_input(proposal.input_payload, create=False)
        data = self._graphql(
            proposal,
            LINEAR_UPDATE_MUTATION,
            {"id": proposal.input_payload["issue_id"], "input": input_payload},
            mutation=True,
        )
        mutation = (
            data.get("issueUpdate")
            if isinstance(data.get("issueUpdate"), dict)
            else {}
        )
        if not mutation.get("success"):
            raise ActionAdapterError("Linear issue update returned success=false.")
        issue = mutation.get("issue") if isinstance(mutation.get("issue"), dict) else {}
        issue_id = str(issue.get("id") or proposal.input_payload["issue_id"])
        return ActionExecutionResult(
            result=_linear_result(issue),
            reversal_payload={
                "operation": "restore_issue",
                "issue_id": issue_id,
                "input": _linear_restore_input(previous),
            },
            external_id=f"linear_issue:{issue_id}",
        )

    def reverse(self, proposal) -> dict:
        reversal = _object(proposal.reversal_payload, field_name="reversal_payload")
        operation = str(reversal.get("operation") or "")
        issue_id = str(reversal.get("issue_id") or "")
        if operation == "archive_issue" and issue_id:
            data = self._graphql(
                proposal,
                LINEAR_ARCHIVE_MUTATION,
                {"id": issue_id},
                mutation=True,
            )
            result = (
                data.get("issueArchive")
                if isinstance(data.get("issueArchive"), dict)
                else {}
            )
            if not result.get("success"):
                raise ActionAdapterError("Linear issue reversal returned success=false.")
            return {"target_system": "linear", "issue_id": issue_id, "archived": True}
        if operation == "restore_issue" and issue_id:
            data = self._graphql(
                proposal,
                LINEAR_UPDATE_MUTATION,
                {"id": issue_id, "input": dict(reversal.get("input") or {})},
                mutation=True,
            )
            result = (
                data.get("issueUpdate")
                if isinstance(data.get("issueUpdate"), dict)
                else {}
            )
            if not result.get("success"):
                raise ActionAdapterError("Linear issue restoration returned success=false.")
            return {
                "target_system": "linear",
                "issue": _linear_result(
                    result.get("issue")
                    if isinstance(result.get("issue"), dict)
                    else {}
                ),
            }
        raise ActionAdapterError("This action has no supported reversal payload.")

    def validate_reversal(self, proposal) -> None:
        reversal = _object(proposal.reversal_payload, field_name="reversal_payload")
        issue_id = str(reversal.get("issue_id") or "")
        if not issue_id:
            raise ActionAdapterError("This action has no supported reversal target.")
        data = self._graphql(
            proposal,
            LINEAR_ISSUE_PRECONDITION_QUERY,
            {"issueId": issue_id},
        )
        issue = data.get("issue") if isinstance(data.get("issue"), dict) else None
        if issue is None:
            raise ActionAdapterError(
                "The Linear issue no longer exists or is inaccessible."
            )
        expected_updated_at = str(
            (proposal.result_payload or {}).get("updated_at") or ""
        )
        live_updated_at = str(issue.get("updatedAt") or "")
        if expected_updated_at and live_updated_at != expected_updated_at:
            raise ActionAdapterError(
                "The Linear issue changed after execution; automatic reversal is unsafe."
            )


def _linear_issue_snapshot(issue: Mapping) -> dict:
    labels = issue.get("labels") if isinstance(issue.get("labels"), Mapping) else {}
    label_nodes = labels.get("nodes") if isinstance(labels.get("nodes"), list) else []
    return {
        "id": str(issue.get("id") or ""),
        "identifier": str(issue.get("identifier") or ""),
        "title": str(issue.get("title") or ""),
        "description": str(issue.get("description") or ""),
        "updated_at": str(issue.get("updatedAt") or ""),
        "priority": issue.get("priority"),
        "due_date": str(issue.get("dueDate") or ""),
        "team_id": str((issue.get("team") or {}).get("id") or ""),
        "project_id": str((issue.get("project") or {}).get("id") or ""),
        "assignee_id": str((issue.get("assignee") or {}).get("id") or ""),
        "state_id": str((issue.get("state") or {}).get("id") or ""),
        "label_ids": sorted(
            str(row.get("id") or "")
            for row in label_nodes
            if isinstance(row, Mapping) and row.get("id")
        ),
    }


def _linear_mutation_input(payload: Mapping, *, create: bool) -> dict:
    mapping = {
        "team_id": "teamId",
        "title": "title",
        "description": "description",
        "assignee_id": "assigneeId",
        "project_id": "projectId",
        "priority": "priority",
        "due_date": "dueDate",
        "label_ids": "labelIds",
        "state_id": "stateId",
    }
    result = {}
    for source, target in mapping.items():
        value = payload.get(source)
        if value not in (None, "", []):
            result[target] = value
    if not create and not payload.get("team_id"):
        result.pop("teamId", None)
    return result


def _linear_restore_input(snapshot: Mapping) -> dict:
    mapping = {
        "team_id": "teamId",
        "title": "title",
        "description": "description",
        "assignee_id": "assigneeId",
        "project_id": "projectId",
        "priority": "priority",
        "due_date": "dueDate",
        "label_ids": "labelIds",
        "state_id": "stateId",
    }
    return {
        target: snapshot.get(source)
        for source, target in mapping.items()
        if source in snapshot
    }


def _linear_result(issue: Mapping) -> dict:
    return {
        "target_system": "linear",
        "id": str(issue.get("id") or ""),
        "identifier": str(issue.get("identifier") or ""),
        "title": str(issue.get("title") or ""),
        "url": str(issue.get("url") or ""),
        "updated_at": str(issue.get("updatedAt") or ""),
    }


class ActionAdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, ActionAdapter] = {}

    def register(self, adapter: ActionAdapter, *, replace: bool = False) -> None:
        action_type = str(adapter.action_type)
        if action_type not in AgentActionType.values:
            raise ValueError(f"Unsupported action adapter type: {action_type}")
        if action_type in self._adapters and not replace:
            raise ValueError(f"Action adapter is already registered: {action_type}")
        self._adapters[action_type] = adapter

    def get(self, action_type: str) -> ActionAdapter:
        try:
            return self._adapters[str(action_type)]
        except KeyError as exc:
            raise ActionAdapterError("No action adapter is registered for this type.") from exc


action_adapter_registry = ActionAdapterRegistry()
for draft_type in (
    AgentActionType.DRAFT_GMAIL,
    AgentActionType.DRAFT_SLACK_POST,
    AgentActionType.DRAFT_NOTION_UPDATE,
):
    action_adapter_registry.register(DraftActionAdapter(draft_type))
for linear_type in (
    AgentActionType.CREATE_LINEAR_ISSUE,
    AgentActionType.UPDATE_LINEAR_ISSUE,
):
    action_adapter_registry.register(LinearActionAdapter(linear_type))
