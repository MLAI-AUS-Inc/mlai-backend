"""Automatic connector selection and calendar-month evidence boundaries."""
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


def monthly_source_period(month, *, timezone_name="UTC", as_of=None):
    """Return the selected local calendar month as a half-open source window."""
    from datetime import date, datetime, time, timezone
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(timezone_name)
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("The reporting cutoff must include a timezone.")
    month = month.replace(day=1)
    next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    start = datetime.combine(month, time.min, zone)
    end = min(datetime.combine(next_month, time.min, zone), now.astimezone(zone))
    if end <= start:
        raise ValueError("Choose today or an earlier reporting month.")
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": timezone_name, "end_exclusive": True}


def run_activity_period(request):
    """Bound legacy rolling/custom run windows to their represented month."""
    from datetime import date, timedelta
    period = request.get("narrative_period") or {}
    month = request.get("target_month") or request.get("current_month")
    if not month:
        return period or None
    cutoff = parse_datetime(str(period.get("end") or ""))
    if cutoff is None:
        cutoff = parse_datetime(str(request.get("backfill_window_end") or ""))
        if cutoff is not None:
            cutoff += timedelta(microseconds=1)
    result = monthly_source_period(date.fromisoformat(str(month)),
        timezone_name=request.get("reporting_timezone") or period.get("timezone") or "UTC", as_of=cutoff)
    # Explicit narrower ranges remain valid; rolling starts never cross a month.
    start = parse_datetime(str(period.get("start") or ""))
    if start is not None and start.tzinfo is not None and parse_datetime(result["start"]) < start < parse_datetime(result["end"]):
        result["start"] = start.isoformat()
    return result


def run_uses_month_scope(request):
    """Reject reuse of immutable evidence captured with pre-monthly source rules."""
    try:
        period = request.get("narrative_period")
        if not period or not (request.get("target_month") or request.get("current_month")):
            return False
        original = activity_window(period)
        bounded = activity_window(run_activity_period(request))
        return original == bounded and request.get("source_period_contract") == "calendar_month_v1"
    except (KeyError, TypeError, ValueError):
        return False


def require_month_source_contract(request, *, snapshot=None):
    """Stop an old worker before it returns or saves incompatible frozen evidence."""
    from startup_updates.revisions import RevisionConflict
    message = "This run used an older source range. Cancel it and draft this month again."
    if not run_uses_month_scope(request):
        raise RevisionConflict(message)
    month = str(request.get("target_month") or request.get("current_month"))
    if request.get("draft_months") != [month]:
        raise RevisionConflict(message)
    if snapshot is None:
        return
    payload = snapshot.payload or {}
    try:
        valid = (
            snapshot.month.isoformat() == month
            and payload.get("source_period_contract") == "calendar_month_v1"
            and (payload.get("period") or {}).get("month") == month
            and activity_window(payload.get("narrative_period")) == activity_window(request["narrative_period"])
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RevisionConflict(message)


def activity_window(period):
    """Resolve a persisted, timezone-aware half-open evidence window."""
    if not period:
        return None
    start, end = parse_datetime(str(period.get("start") or "")), parse_datetime(str(period.get("end") or ""))
    if start is None or end is None or start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("The source period must have ordered timestamps with timezones.")
    return start, end


def message_in_activity_window(payload, period):
    """Exclude cached messages outside a run, including cross-month thread replies."""
    window = activity_window(period)
    if not window:
        return True
    from datetime import datetime, timezone
    raw = payload.get("posted_at") or payload.get("internal_date") or payload.get("last_edited_time")
    posted = parse_datetime(str(raw)) if raw else None
    if posted is None:
        try:
            posted = datetime.fromtimestamp(float(payload.get("message_ts")), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return False
    if posted.tzinfo is None:
        return False
    return window[0] <= posted < window[1]


def cached_notion_month_version(page, versions, period):
    """Reuse a retained in-month page version after later edits, never newer text."""
    page_id = str(page.get("id") or "")
    candidates = [bundle for bundle in (versions or {}).values()
        if isinstance(bundle, dict) and str(bundle.get("notion_page_id") or "") == page_id
        and message_in_activity_window(bundle, period)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: parse_datetime(item["last_edited_time"]))


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
    if not run_request.get("narrative_period") or provider in prepared:
        return run_request
    queryset.update(relevance_label="pending", relevance_score=0, relevance_reason="",
        needs_extraction=False, extraction_hints={}, classified_at=None, extraction_status="hydrated")
    return {**run_request, "activity_prepared_sources": [*prepared, provider]}
