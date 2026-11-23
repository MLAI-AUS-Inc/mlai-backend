"""Read scoped project inventories and maintain the integration-owned Linear resource link."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

from integrations import http_client


class ProjectAccessUnconfirmed(RuntimeError):
    """The provider failed to prove current identity, access, or a complete response."""


class ProjectNotFound(ProjectAccessUnconfirmed):
    """A targeted read reported missing. Only a matching signed removal can confirm it."""


class ProjectAccessRevoked(ProjectAccessUnconfirmed):
    """The scoped installation no longer has access to a previously readable project."""


class ProjectExcluded(ProjectAccessRevoked):
    """The project falls outside the configured teams or is a synthetic backlog."""


class MirrorIdentityUnconfirmed(ProjectAccessUnconfirmed):
    """An imported project cannot yet be matched against the migration ledger."""


_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_LINEAR_SCOPE = "query ManagedChannelWorkspace { organization { id } }"
_PAGE = "pageInfo { hasNextPage endCursor }"
_PROJECT_FIELDS = """
    id name url updatedAt archivedAt trashed status { type name }
    health lastUpdate { id url updatedAt }
    slackChannelId teams(first: 10, includeArchived: true) {
      nodes { id private } pageInfo { hasNextPage endCursor }
    }
"""
_LINEAR_LIST = """
  query ManagedChannelProjects($after: String) {
    organization { id }
    projects(first: 25, includeArchived: true, after: $after) {
      nodes { %s } %s
    }
  }
""" % (_PROJECT_FIELDS, _PAGE)
_LINEAR_ONE = """
  query ManagedChannelProject($id: String!) {
    organization { id }
    project(id: $id) { %s }
  }
""" % _PROJECT_FIELDS
_LINEAR_TEAMS = """
  query ManagedChannelProjectTeams($id: String!, $after: String!) {
    organization { id }
    project(id: $id) {
      id teams(first: 50, after: $after, includeArchived: true) { nodes { id private } %s }
    }
  }
""" % _PAGE
_LINEAR_LINKS = """
  query ManagedChannelLinks($id: String!, $after: String) {
    organization { id }
    project(id: $id) {
      id externalLinks(first: 100, after: $after) { nodes { id label url } %s }
    }
  }
""" % _PAGE
_CREATE_LINK = """
  mutation ManagedChannelLinkCreate($input: EntityExternalLinkCreateInput!) {
    entityExternalLinkCreate(input: $input) { success entityExternalLink { id label url } }
  }
"""
_UPDATE_LINK = """
  mutation ManagedChannelLinkUpdate($id: String!, $input: EntityExternalLinkUpdateInput!) {
    entityExternalLinkUpdate(id: $id, input: $input) { success entityExternalLink { id label url } }
  }
"""


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("Expected a UUID string")
    return str(UUID(value))


def _secret(config, key, default=None):
    value = os.environ.get(config.get(key, default) or "")
    if not value:
        raise ProjectAccessUnconfirmed(f"Missing managed project credential for {key}")
    return value


def _origin(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("Expected an HTTPS origin")
    return f"https://{parts.netloc}"


def _safe_linear_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.netloc != "linear.app"
            or not parts.path.startswith("/") or parts.fragment.startswith("//")):
        raise ValueError("Invalid Linear project link")
    return value


def _json_response(response, *, targeted=False):
    if targeted and response.status_code == 404:
        raise ProjectNotFound("Targeted project lookup returned 404")
    if response.status_code != 200:
        raise ProjectAccessUnconfirmed(f"Project provider returned HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise ProjectAccessUnconfirmed("Expected a project provider object")
    return body


def _linear(config, query, variables, *, mutation=False, targeted=False):
    token = _secret(config, "api_key_env", "LINEAR_API_KEY")
    response = http_client.post(
        _LINEAR_ENDPOINT, headers={"Authorization": token}, json={"query": query, "variables": variables},
        allow_redirects=False, timeout=25,
    )
    # GraphQL returns both HTTP 200 and HTTP 400 for errors. Never use partial data.
    if targeted and response.status_code == 403:
        _linear(config, _LINEAR_SCOPE, {})
        raise ProjectAccessRevoked("Linear denied access to this project")
    if targeted and response.status_code in (200, 400):
        body = response.json()
        errors = body.get("errors") or []
        if len(errors) == 1 and errors[0].get("path") == ["project"]:
            extension = errors[0].get("extensions") or {}
            code = extension.get("code")
            if code in {"FORBIDDEN", "PERMISSION_DENIED"}:
                _linear(config, _LINEAR_SCOPE, {})
                raise ProjectAccessRevoked("Linear denied access to this project")
            if code in {"ENTITY_NOT_FOUND", "NOT_FOUND"} or extension.get("type") == "not found":
                # An inaccessible organization or a rotated token is not evidence of a removal.
                _linear(config, _LINEAR_SCOPE, {})
                raise ProjectNotFound("Linear reports the targeted project is missing")
    body = _json_response(response)
    if body.get("errors") or not isinstance(body.get("data"), dict):
        raise ProjectAccessUnconfirmed("Incomplete Linear GraphQL response")
    data = body["data"]
    if not mutation and _uuid((data.get("organization") or {}).get("id")) != _uuid(config["scope"]):
        raise PermissionError("Linear workspace differs from configured scope")
    return data


def _next_cursor(page, seen, *, plane=False):
    more_key, cursor_key = ("next_page_results", "next_cursor") if plane else ("hasNextPage", "endCursor")
    more = page.get(more_key)
    if not isinstance(more, bool):
        raise ProjectAccessUnconfirmed("Missing provider pagination state")
    if not more:
        return None
    cursor = page.get(cursor_key)
    if not isinstance(cursor, str) or not cursor or cursor in seen:
        raise ProjectAccessUnconfirmed("Invalid or repeated provider cursor")
    seen.add(cursor)
    return cursor


def _rows(connection, field):
    rows = connection.get(field)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ProjectAccessUnconfirmed("Incomplete provider list")
    return rows


def _configured_ids(config, key):
    values = config.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"Expected a project policy ID list for {key}")
    return {_uuid(value) for value in values}


def _linear_teams(config, project_id, connection):
    rows, seen = [], set()
    while True:
        rows.extend(_rows(connection, "nodes"))
        cursor = _next_cursor(connection["pageInfo"], seen)
        if cursor is None:
            return rows
        project = _linear(config, _LINEAR_TEAMS, {"id": project_id, "after": cursor}).get("project")
        if not project or _uuid(project["id"]) != project_id:
            raise ProjectAccessUnconfirmed("Linear did not confirm the owner of the project teams")
        connection = project["teams"]


def _linear_snapshot(config, row):
    project_id = _uuid(row["id"])
    team_rows = _linear_teams(config, project_id, row["teams"])
    allowed = _configured_ids(config, "team_ids")
    actual = {_uuid(team["id"]) for team in team_rows}
    if not allowed:
        raise ValueError("Linear team allowlist is empty")
    if not allowed.intersection(actual):
        raise ProjectExcluded("No enabled Linear team owns this project")
    is_public = actual.issubset(allowed) and all(team.get("private") is False for team in team_rows)
    # Linear omits a private team the installation cannot see. Team-wide public promotion
    # requires independently verified coverage of the workspace's private teams.
    team_policy = (config.get("full_team_visibility_verified") is True
                   and actual.issubset(_configured_ids(config, "public_team_ids")))
    publish = is_public and (team_policy or project_id in _configured_ids(config, "public_project_ids"))
    name = row["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing Linear project title")
    state = row["status"]["type"]
    status = "removed" if row.get("trashed") else "archived" if row.get("archivedAt") else state
    snapshot = {
        "external_id": project_id, "title": name, "url": "" if status == "removed" else _safe_linear_url(row["url"]),
        "status": status, "source_status": row["status"]["name"], "source_updated_at": row["updatedAt"],
        "visibility": "public" if is_public else "private",
        "public_authorized": publish,
    }
    if status != "removed" and row.get("lastUpdate"):
        update = row["lastUpdate"]
        snapshot["latest_project_update"] = {
            "id": _uuid(update["id"]), "url": _safe_linear_url(update["url"]), "updated_at": update["updatedAt"],
            "health": row.get("health"),
        }
    if config.get("slack_workspace_id") and row.get("slackChannelId"):
        snapshot["slack_workspace_id"] = config["slack_workspace_id"]
        snapshot["slack_channel_id"] = row["slackChannelId"]
    return snapshot


def _plane(config, path, *, params=None, targeted=False):
    slug = config["workspace_slug"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
        raise ValueError("Invalid Plane workspace slug")
    origin = _origin(config.get("api_origin", "https://plane.mlai.au"))
    headers = {"X-API-Key": _secret(config, "api_key_env", "PLANE_API_KEY")}
    if config.get("cf_access_client_id_env") or config.get("cf_access_client_secret_env"):
        headers["CF-Access-Client-Id"] = _secret(config, "cf_access_client_id_env")
        headers["CF-Access-Client-Secret"] = _secret(config, "cf_access_client_secret_env")
    response = http_client.get(
        f"{origin}/api/v1/workspaces/{slug}/{path}", headers=headers, params=params,
        allow_redirects=False, timeout=25,
    )
    if targeted and response.status_code in (403, 404):
        # This self-hosted API routes by mutable workspace slug. Verify the UUID
        # under the same credentials before classifying a missing/restricted lookup.
        auth = _plane(config, "projects/", params={"per_page": 1, "fields": "id,workspace"})
        evidence = _rows(auth, "results")
        if not evidence or _uuid(evidence[0].get("workspace")) != _uuid(config["scope"]):
            raise ProjectAccessUnconfirmed("Unable to confirm the Plane workspace")
        if response.status_code == 403:
            raise ProjectAccessRevoked("Plane denied access to this project")
    return _json_response(response, targeted=targeted)


def _import_map(config):
    try:
        mapping = config.get("linear_project_map", {})
        if config.get("linear_project_map_path"):
            with Path(config["linear_project_map_path"]).open() as checkpoint:
                mapping = json.load(checkpoint)["projects"]
        if not isinstance(mapping, dict):
            raise ValueError("Expected a project identity map")
    except (OSError, ValueError, KeyError) as exc:
        raise MirrorIdentityUnconfirmed("Invalid or unavailable project identity map") from exc
    return mapping


def _plane_alias(config, row):
    if row.get("external_source") != config.get("mirror_namespace", "linear-mlai-20260907"):
        return None
    source_id = row.get("external_id")
    if isinstance(source_id, str) and source_id.startswith("backlog:"):
        raise ProjectExcluded("Synthetic Linear backlog")
    try:
        source_id = _uuid(source_id)
    except ValueError as exc:
        raise MirrorIdentityUnconfirmed("Incomplete imported project identity") from exc
    target = _import_map(config).get(source_id)
    if isinstance(target, dict):
        target = target.get("id")
    if not config.get("linear_scope") or target != row["id"]:
        raise MirrorIdentityUnconfirmed("Plane external source is absent from the verified import mapping")
    return {"provider": "linear", "scope": _uuid(config["linear_scope"]), "external_id": source_id}


def _plane_snapshot(config, row, requested_id):
    project_id = _uuid(row["id"])
    if project_id != requested_id or _uuid(row["workspace"]) != _uuid(config["scope"]):
        raise PermissionError("Plane project or workspace identity differs from the requested identity")
    name = row["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing Plane project title")
    visibility = {0: "private", 2: "public"}.get(row.get("network"), "unknown")
    # Plane's public network means workspace-public. The operator explicitly chooses
    # whether those projects may have an open MLAI discussion.
    publish = visibility == "public" and (config.get("public_workspace_projects") is True
                                          or project_id in _configured_ids(config, "public_project_ids"))
    origin = _origin(config.get("public_origin", "https://plane.mlai.au"))
    status = "removed" if row.get("deleted_at") else "archived" if row["archived_at"] else "active"
    snapshot = {
        "external_id": project_id, "title": name, "status": status,
        "visibility": visibility, "public_authorized": publish,
        "url": "" if status == "removed" else f"{origin}/{config['workspace_slug']}/projects/{project_id}/issues/",
        "source_updated_at": row["updated_at"],
    }
    alias = _plane_alias(config, row)
    if alias is not None:
        snapshot["alias"] = alias
    return snapshot


def get_snapshot(provider, config, external_id):
    """Read and qualify one project. A missing or unauthorized lookup never implies deletion."""
    project_id = _uuid(external_id)
    if provider == "plane":
        row = _plane(config, f"projects/{project_id}/", targeted=True)
        return _plane_snapshot(config, row, project_id)
    if provider == "linear":
        row = _linear(config, _LINEAR_ONE, {"id": project_id}, targeted=True).get("project")
        if not row or _uuid(row["id"]) != project_id:
            raise ProjectAccessUnconfirmed("Linear did not confirm the requested project")
        return _linear_snapshot(config, row)
    raise ValueError("Unknown project provider")


def inventory(provider, config):
    """Return a complete scoped inventory. Cursor failures never produce partial results."""
    if provider not in {"linear", "plane"}:
        raise ValueError("Unknown project provider")
    if provider == "plane" and config.get("linear_project_map_path"):
        # One consistent import checkpoint per scan rather than one large file read for every project.
        try:
            mapping = _import_map(config)
        except MirrorIdentityUnconfirmed:
            mapping = {}
        config = {**config, "linear_project_map": mapping, "linear_project_map_path": None}
    result, seen_cursors, seen_ids = [], set(), set()
    cursor = None
    while True:
        if provider == "linear":
            connection = _linear(config, _LINEAR_LIST, {"after": cursor})["projects"]
            rows = _rows(connection, "nodes")
            cursor = _next_cursor(connection["pageInfo"], seen_cursors)
        else:
            params = {"include_archived": "true", "per_page": 100}
            if cursor:
                params["cursor"] = cursor
            page = _plane(config, "projects-lite/", params=params)
            rows = _rows(page, "results")
            cursor = _next_cursor(page, seen_cursors, plane=True)
        for row in rows:
            project_id = _uuid(row["id"])
            if project_id in seen_ids:
                raise ProjectAccessUnconfirmed("Duplicate project during inventory")
            seen_ids.add(project_id)
            try:
                snapshot = _linear_snapshot(config, row) if provider == "linear" else get_snapshot(provider, config, project_id)
            except ProjectExcluded:
                continue
            except MirrorIdentityUnconfirmed:
                # This resource exists, but creating an entity could duplicate the Linear discussion.
                # The worker persists a targeted hold and retries independently of other projects.
                snapshot = {"external_id": project_id, "hold_reason": "mirror_identity_unconfirmed"}
            result.append(snapshot)
        if cursor is None:
            return result


def decode_webhook(provider, config, headers, body):
    """Validate the signed scope and return a refresh hint, or None for irrelevant resources."""
    if provider not in {"plane", "linear"}:
        raise ValueError("Unknown project provider")
    headers = {k.lower(): v for k, v in headers.items()}
    prefix = "linear" if provider == "linear" else "x-plane"
    key = _secret(config, "webhook_secret_env", f"MANAGED_CHANNEL_{provider.upper()}_WEBHOOK_SECRET")
    expected = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    signature = headers.get(f"{prefix}-signature", "")
    if not isinstance(signature, str) or not signature.isascii() or not hmac.compare_digest(expected, signature):
        raise PermissionError("Invalid project webhook signature")
    data = json.loads(body)
    scope_key = "organizationId" if provider == "linear" else "workspace_id"
    if _uuid(data[scope_key]) != _uuid(config["scope"]):
        raise PermissionError("Project webhook outside configured workspace")
    delivery_id = _uuid(headers.get(f"{prefix}-delivery"))
    event = data.get("type") if provider == "linear" else data.get("event")
    if headers.get(f"{prefix}-event") != event:
        raise ValueError("Event header differs from signed event identity")
    record = data.get("data") or {}
    if provider == "linear":
        timestamp = data.get("webhookTimestamp")
        if type(timestamp) is not int or abs(time.time() * 1000 - timestamp) > 60_000:
            raise PermissionError("Stale Linear webhook")
        if event not in {"Project", "ProjectUpdate"} or data.get("action") not in {"create", "update", "remove"}:
            return None
        if event == "ProjectUpdate":
            return delivery_id, _uuid(record.get("projectId")), "refresh"
        removed = data["action"] == "remove"
        project_id = record.get("id")
    elif data.get("version") == "v2":
        if delivery_id != _uuid(data.get("delivery_id")):
            raise ValueError("Plane webhook delivery identity mismatch")
        if event not in {"project.created", "project.updated", "project.archived", "project.deleted", "project.unarchived"}:
            return None
        project_id = data.get("entity_id")
        if record.get("id") and _uuid(record["id"]) != _uuid(project_id):
            raise ValueError("Plane webhook entity identity mismatch")
        # V2 delivery_id changes on retry. event_id is signed and stable.
        delivery_id = _uuid(data.get("event_id"))
        removed = event == "project.deleted"
    else:
        if event != "project" or data.get("action") not in {"create", "created", "update", "updated", "delete", "deleted"}:
            return None
        project_id = record.get("id")
        removed = data["action"] in {"deleted", "delete"}
    return delivery_id, _uuid(project_id), "removed" if removed else "refresh"


def _project_links(config, project_id):
    links, seen = [], set()
    cursor = None
    while True:
        data = _linear(config, _LINEAR_LINKS, {"id": project_id, "after": cursor})
        project = data.get("project")
        if not project or _uuid(project["id"]) != project_id:
            raise ProjectAccessUnconfirmed("Unverified Linear link parent")
        connection = project["externalLinks"]
        links.extend(_rows(connection, "nodes"))
        cursor = _next_cursor(connection["pageInfo"], seen)
        if cursor is None:
            return links


def sync_backlink(provider, config, external_id, channel_id):
    """Ensure one Linear project resource for the caller's verified shared room."""
    if provider == "plane":
        raise NotImplementedError("The installed Plane API has no supported project resource link mutation")
    if provider != "linear":
        raise ValueError("Unknown project provider")
    project_id, channel_id = _uuid(external_id), _uuid(str(channel_id))
    # Recheck project access and configured team scope before any outbound write.
    project = get_snapshot(provider, config, project_id)
    if project["status"] in {"removed", "archived"}:
        raise ProjectAccessUnconfirmed("Unavailable Linear project")
    base = _origin(config.get("chat_origin", "https://chat.mlai.au"))
    url = f"{base}/channels/{channel_id}"
    identity = f"mlai-managed-channel-v1:{config['community']}:{config['scope']}:{project_id}:{channel_id}"
    # Linear accepts caller-supplied UUIDv4-shaped IDs. Replaying a lost write uses the same ID.
    link_id = str(UUID(bytes=hashlib.sha256(identity.encode()).digest()[:16], version=4))
    links = _project_links(config, project_id)
    exact = [link for link in links if link["url"] == url]
    if len(exact) > 1:
        raise ProjectAccessUnconfirmed("Multiple Linear resources already reference this room")
    if exact:
        return _uuid(exact[0]["id"])
    desired = {"label": "MLAI Chat", "url": url}
    if any(link["id"] == link_id for link in links):
        mutation, field, variables = _UPDATE_LINK, "entityExternalLinkUpdate", {"id": link_id, "input": desired}
    else:
        mutation, field = _CREATE_LINK, "entityExternalLinkCreate"
        variables = {"input": {**desired, "projectId": project_id, "id": link_id}}
    response = _linear(config, mutation, variables, mutation=True)[field]
    if response.get("success") is not True or response.get("entityExternalLink", {}).get("id") != link_id:
        raise ProjectAccessUnconfirmed("Linear did not confirm the resource mutation")
    if not any(link["id"] == link_id and link["url"] == url for link in _project_links(config, project_id)):
        raise ProjectAccessUnconfirmed("Could not verify the Linear project resource")
    return link_id
