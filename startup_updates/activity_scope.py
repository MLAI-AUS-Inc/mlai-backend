"""Automatic connector scope for Chat's recent-activity drafts."""
from django.utils.dateparse import parse_datetime

from integrations.services import external_connectors

CATALOG_PAGE_LIMIT = 20


def discover_activity_resources(user, organization, providers):
    """Discover authorized resources without changing legacy manual selections."""
    scope = {}
    catalogs = {
        "slack": (external_connectors.serialize_slack_channels, "channels", "channelId"),
        "google_analytics": (external_connectors.serialize_google_analytics_properties, "properties", "propertyId"),
    }
    for provider in set(providers).intersection(catalogs):
        fetch, rows_key, id_key = catalogs[provider]
        cursor = None
        seen = set()
        ids = []
        for _ in range(CATALOG_PAGE_LIMIT):
            page = fetch(user, organization=organization, cursor=cursor, limit=200)
            ids.extend(str(row[id_key]) for row in page.get(rows_key, []) if row.get(id_key))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise external_connectors.ConnectorConfigurationError(f"{provider} returned a repeated resource page. Please reconnect and try again.")
            seen.add(cursor)
        else:
            raise external_connectors.ConnectorConfigurationError(f"{provider} has too many resources to prepare in one request. Please try again later.")
        scope[provider] = list(dict.fromkeys(ids))
    if "linear" in providers:
        scope["linear"] = discover_linear_projects(user, organization)
    return scope


def activity_window(period):
    """Resolve the persisted half-open window used by a recent-activity run."""
    if not period:
        return None
    start, end = parse_datetime(period["start"]), parse_datetime(period["end"])
    if start is None or end is None or start >= end:
        return None
    return start, end


def message_in_activity_window(payload, period):
    """Exclude cached messages outside the run even when their thread is recent."""
    window = activity_window(period)
    if not window:
        return True
    from datetime import datetime, timezone
    raw = payload.get("posted_at")
    posted = parse_datetime(str(raw)) if raw else None
    if posted is None:
        try:
            posted = datetime.fromtimestamp(float(payload.get("message_ts")), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return False
    if posted.tzinfo is None:
        return False
    return window[0] <= posted < window[1]


def discover_linear_projects(user, organization):
    """Pin the accessible catalog; actual project activity is filtered per run."""
    from startup_updates.models import LinearProjectSelection
    connection = external_connectors._latest_linear_connection(user, organization)
    if connection is None:
        return []
    cursor, seen, ids = None, set(), []
    for _ in range(CATALOG_PAGE_LIMIT):
        payload = external_connectors._linear_graphql_request(connection, external_connectors.LINEAR_PROJECT_LIST_QUERY, {"first": external_connectors.LINEAR_PROJECT_CATALOG_PAGE_LIMIT, "after": cursor})
        projects = payload.get("projects") or {}
        for project in projects.get("nodes") or []:
            if not isinstance(project, dict) or not project.get("id"):
                continue
            project_id = str(project["id"])
            LinearProjectSelection.objects.update_or_create(connection=connection, linear_project_id=project_id, defaults={
                "user": user, "organization": organization, "project_name": project.get("name") or project_id, "raw_payload": project,
            })
            ids.append(project_id)
        page = projects.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return list(dict.fromkeys(ids))
        cursor = page.get("endCursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise external_connectors.ConnectorConfigurationError("Linear project discovery did not finish. Please try again.")


def linear_project_has_activity(project, period):
    """Test project or child activity against this run's historical time window."""
    window = activity_window(period)
    if not window:
        return True
    for key in ("createdAt", "updatedAt", "startedAt", "completedAt", "canceledAt"):
        stamp = parse_datetime(str((project.raw_payload or {}).get(key) or ""))
        if stamp and stamp.tzinfo and window[0] <= stamp < window[1]:
            return True
    bounds = {"updated_at_linear__gte": window[0], "updated_at_linear__lt": window[1]}
    return project.issues.filter(**bounds).exists() or project.project_updates.filter(**bounds).exists()


def prepare_activity_classification(run_request, provider, queryset):
    """Reclassify the scoped cached inputs once per run without deleting evidence."""
    prepared = list(run_request.get("activity_prepared_sources") or [])
    if not run_request.get("activity_window_days") or provider in prepared:
        return run_request
    queryset.update(relevance_label="pending", relevance_score=0, relevance_reason="",
        needs_extraction=False, extraction_hints={}, classified_at=None, extraction_status="hydrated")
    return {**run_request, "activity_prepared_sources": [*prepared, provider]}
