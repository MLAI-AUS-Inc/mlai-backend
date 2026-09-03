#!/usr/bin/env python3
"""Validate deployment-managed configuration for Roo's Linear issue reader."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid


SLACK_BINDING_RE = re.compile(r"^T[A-Z0-9]+:C[A-Z0-9]+$")
REQUIRED_BINDING_FIELDS = (
    "display_name",
    "team_name",
    "state_name",
    "linear_team_id",
    "linear_state_id",
)


def fail(message: str) -> None:
    raise SystemExit(message)


api_key = os.environ.get("LINEAR_API_KEY", "").strip()
if len(api_key) < 32:
    fail("LINEAR_API_KEY must be configured as a repository secret with at least 32 characters")

raw_required_team_keys = os.environ.get("LINEAR_MEETING_REQUIRED_TEAM_KEYS", "")
required_team_keys = {
    value.strip().upper()
    for value in raw_required_team_keys.split(",")
    if value.strip()
}
if not required_team_keys or any(
    not re.fullmatch(r"[A-Z0-9_-]+", value) for value in required_team_keys
):
    fail(
        "LINEAR_MEETING_REQUIRED_TEAM_KEYS must be a non-empty comma-separated "
        "list of Linear team keys"
    )

raw_bindings = os.environ.get("LINEAR_CHANNEL_ISSUE_BINDINGS_JSON", "")
try:
    bindings = json.loads(raw_bindings)
except json.JSONDecodeError as exc:
    fail(f"LINEAR_CHANNEL_ISSUE_BINDINGS_JSON must be valid JSON: {exc.msg}")

if not isinstance(bindings, dict) or not bindings:
    fail("LINEAR_CHANNEL_ISSUE_BINDINGS_JSON must be a non-empty JSON object")

for slack_binding, binding in bindings.items():
    if not isinstance(slack_binding, str) or not SLACK_BINDING_RE.fullmatch(slack_binding):
        fail("Linear channel binding keys must have the form T<workspace>:C<channel>")
    if not isinstance(binding, dict):
        fail(f"Linear channel binding {slack_binding} must be a JSON object")
    for field in REQUIRED_BINDING_FIELDS:
        if not isinstance(binding.get(field), str) or not binding[field].strip():
            fail(f"Linear channel binding {slack_binding} requires non-empty {field}")
    for field in ("linear_team_id", "linear_state_id"):
        try:
            uuid.UUID(binding[field])
        except ValueError:
            fail(f"Linear channel binding {slack_binding} has invalid {field}")

max_comments = os.environ.get("LINEAR_CHANNEL_ISSUE_MAX_COMMENTS", "")
if not re.fullmatch(r"[1-9][0-9]*", max_comments):
    fail("LINEAR_CHANNEL_ISSUE_MAX_COMMENTS must be a positive integer")


def visible_linear_team_keys() -> set[str]:
    query = """
    query LinearDeploymentTeams($first: Int!, $after: String) {
      teams(first: $first, after: $after) {
        nodes { key }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    visible: set[str] = set()
    cursor = None
    for _page_number in range(20):
        body = json.dumps(
            {
                "query": query,
                "operationName": "LinearDeploymentTeams",
                "variables": {"first": 100, "after": cursor},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=body,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            fail(
                "Could not verify LINEAR_API_KEY team access against Linear: "
                f"{exc.__class__.__name__}"
            )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            fail("Linear rejected LINEAR_API_KEY while verifying required team access")
        connection = payload.get("data", {}).get("teams", {})
        for node in connection.get("nodes", []):
            key = str(node.get("key") or "").strip().upper()
            if key:
                visible.add(key)
        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return visible
        next_cursor = str(page_info.get("endCursor") or "").strip()
        if not next_cursor or next_cursor == cursor:
            fail("Linear team-access verification pagination did not advance")
        cursor = next_cursor
    fail("Linear team-access verification exceeded the 20-page safety limit")


if os.environ.get("LINEAR_VERIFY_TEAM_ACCESS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    missing_team_keys = sorted(required_team_keys - visible_linear_team_keys())
    if missing_team_keys:
        fail(
            "LINEAR_API_KEY cannot access required Linear teams: "
            + ", ".join(missing_team_keys)
            + ". Recreate it with read/write permission and "
            "'All teams you have access to'."
        )
