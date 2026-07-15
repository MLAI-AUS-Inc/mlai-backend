from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build

from content_analytics.models import (
    SearchConsoleAccessMethod,
    SearchConsoleProperty,
    SearchConsolePropertyStatus,
)
from content_factory.google_baseline import GSC_SCOPE
from integrations.models import GoogleConnection
from integrations.services.gmail import get_refreshed_credentials


class SearchConsoleConfigurationError(RuntimeError):
    pass


class SearchConsoleVerificationError(RuntimeError):
    pass


def _service_account_info() -> dict[str, Any]:
    raw = str(getattr(settings, "GOOGLE_SEARCH_CONSOLE_SERVICE_ACCOUNT_JSON", "") or "").strip()
    file_path = str(getattr(settings, "GOOGLE_SEARCH_CONSOLE_SERVICE_ACCOUNT_FILE", "") or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SearchConsoleConfigurationError("Google Search Console service-account JSON is invalid.") from exc
        if not isinstance(payload, dict):
            raise SearchConsoleConfigurationError("Google Search Console service-account JSON must be an object.")
        return payload
    if file_path:
        try:
            payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SearchConsoleConfigurationError("Unable to read Google Search Console service-account file.") from exc
        if not isinstance(payload, dict):
            raise SearchConsoleConfigurationError("Google Search Console service-account file must contain an object.")
        return payload
    return {}


def service_account_email() -> str:
    try:
        return str(_service_account_info().get("client_email") or "").strip()
    except SearchConsoleConfigurationError:
        return ""


def service_account_is_configured() -> bool:
    return bool(service_account_email())


def _service_account_credentials():
    info = _service_account_info()
    if not info:
        raise SearchConsoleConfigurationError("Google Search Console service-account credentials are not configured.")
    return service_account.Credentials.from_service_account_info(info, scopes=[GSC_SCOPE])


def _search_console_service(*, access_method: str, google_connection: GoogleConnection | None = None):
    if access_method == SearchConsoleAccessMethod.SERVICE_ACCOUNT:
        credentials = _service_account_credentials()
    elif access_method == SearchConsoleAccessMethod.OAUTH:
        if google_connection is None:
            raise SearchConsoleConfigurationError("A Google OAuth connection is required.")
        scopes = set(str(google_connection.scope or "").split())
        if GSC_SCOPE not in scopes:
            raise SearchConsoleConfigurationError("Reconnect Google with Search Console read-only access.")
        credentials = get_refreshed_credentials(google_connection)
    else:
        raise SearchConsoleConfigurationError("Unsupported Search Console access method.")
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def _normalized_domain(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sc-domain:"):
        text = text.removeprefix("sc-domain:")
    parsed = urlsplit(text if "://" in text else f"//{text}")
    return (parsed.hostname or parsed.path.split("/", 1)[0]).removeprefix("www.").rstrip(".")


def _exact_site_candidates(domain: str) -> list[str]:
    normalized = _normalized_domain(domain)
    return [
        f"sc-domain:{normalized}",
        f"https://{normalized}/",
        f"https://www.{normalized}/",
        f"http://{normalized}/",
        f"http://www.{normalized}/",
    ]


def _available_sites(service) -> list[dict[str, Any]]:
    payload = service.sites().list().execute() or {}
    return [row for row in payload.get("siteEntry") or [] if isinstance(row, dict)]


def verify_search_console_property(
    *,
    organization,
    requested_site_url: str = "",
    user=None,
    access_method: str = "",
) -> SearchConsoleProperty:
    requested_site_url = str(requested_site_url or "").strip()
    exact_candidates = _exact_site_candidates(organization.domain)
    if requested_site_url and requested_site_url not in exact_candidates:
        raise SearchConsoleVerificationError(
            "The requested Search Console property does not exactly match this organization's domain."
        )
    method = str(access_method or "").strip()
    google_connection = None
    if not method:
        method = (
            SearchConsoleAccessMethod.SERVICE_ACCOUNT
            if service_account_is_configured()
            else SearchConsoleAccessMethod.OAUTH
        )
    if method == SearchConsoleAccessMethod.OAUTH:
        google_connection = GoogleConnection.objects.filter(user=user).first() if user and user.is_authenticated else None
    service = _search_console_service(access_method=method, google_connection=google_connection)
    sites = _available_sites(service)
    sites_by_url = {str(row.get("siteUrl") or "").strip(): row for row in sites}

    if requested_site_url:
        matched = sites_by_url.get(requested_site_url)
        if matched is None:
            raise SearchConsoleVerificationError("That exact Search Console property is not available to this connection.")
    else:
        matched = None
        for candidate in exact_candidates:
            if candidate in sites_by_url:
                matched = sites_by_url[candidate]
                break
        if matched is None:
            account_hint = service_account_email() if method == SearchConsoleAccessMethod.SERVICE_ACCOUNT else "the connected Google account"
            raise SearchConsoleVerificationError(
                f"No exact Search Console property for {organization.domain} is shared with {account_hint}."
            )

    site_url = str(matched.get("siteUrl") or "").strip()
    permission = str(matched.get("permissionLevel") or "").strip()
    prop, _created = SearchConsoleProperty.objects.update_or_create(
        organization=organization,
        defaults={
            "site_url": site_url,
            "access_method": method,
            "google_connection": google_connection,
            "status": SearchConsolePropertyStatus.VERIFIED,
            "permission_level": permission,
            "service_account_email": service_account_email() if method == SearchConsoleAccessMethod.SERVICE_ACCOUNT else "",
            "sync_enabled": True,
            "last_verified_at": timezone.now(),
            "last_error": "",
        },
    )
    return prop


def service_for_property(prop: SearchConsoleProperty):
    return _search_console_service(
        access_method=prop.access_method,
        google_connection=prop.google_connection,
    )


def canonical_article_url(article) -> str:
    for value in (article.canonical_url, article.live_url):
        text = str(value or "").strip()
        if text:
            return text
    path = str(article.canonical_path or "").strip()
    if not path:
        path = f"/{article.slug.strip('/')}" if article.slug else ""
    if not path:
        return ""
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://{article.organization.domain.strip('/')}{path}"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _query_page_range(
    service,
    *,
    site_url: str,
    canonical_url: str,
    start_date: date,
    end_date: date,
    dimensions: list[str] | None = None,
    row_limit: int = 1,
) -> list[dict[str, Any]]:
    if end_date < start_date:
        raise ValueError("Search Console end date cannot be before the start date.")
    body: dict[str, Any] = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "rowLimit": max(1, min(int(row_limit), 25000)),
        "dimensionFilterGroups": [
            {
                "groupType": "and",
                "filters": [
                    {"dimension": "page", "operator": "equals", "expression": canonical_url},
                ],
            }
        ],
    }
    if dimensions:
        body["dimensions"] = dimensions
    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute() or {}
    return [row for row in response.get("rows") or [] if isinstance(row, dict)]


def _query_page(
    service,
    *,
    site_url: str,
    canonical_url: str,
    day: date,
    dimensions: list[str] | None = None,
    row_limit: int = 1,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that still need a single-day query."""

    return _query_page_range(
        service,
        site_url=site_url,
        canonical_url=canonical_url,
        start_date=day,
        end_date=day,
        dimensions=dimensions,
        row_limit=row_limit,
    )


@dataclass(frozen=True)
class SearchConsoleArticleDay:
    aggregate: dict[str, Decimal]
    queries: list[dict[str, Any]]


@dataclass(frozen=True)
class SearchConsoleArticleWindow:
    days: dict[date, SearchConsoleArticleDay]


def _empty_article_day() -> SearchConsoleArticleDay:
    return SearchConsoleArticleDay(
        aggregate={
            "clicks": Decimal("0"),
            "impressions": Decimal("0"),
            "ctr": Decimal("0"),
            "position": Decimal("0"),
        },
        queries=[],
    )


def _row_date(row: dict[str, Any], *, key_index: int = 0) -> date | None:
    keys = row.get("keys") or []
    try:
        raw = str(keys[key_index]).strip()
    except (IndexError, TypeError):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def fetch_article_window(
    prop: SearchConsoleProperty,
    article,
    start_date: date,
    end_date: date,
    *,
    canonical_url: str = "",
    query_limit: int | None = None,
    service=None,
) -> SearchConsoleArticleWindow:
    """Fetch one article window using two Search Analytics API queries.

    Aggregate and top-query rows are both dimensioned by date so a sync window
    does not fan out into an article-by-day request loop. Missing aggregate days
    are represented explicitly with zeros, allowing callers to overwrite stale
    local rows when Search Console returns no data for a day.
    """

    if end_date < start_date:
        raise ValueError("Search Console end date cannot be before the start date.")
    canonical_url = str(canonical_url or "").strip() or canonical_article_url(article)
    if not canonical_url:
        raise ValueError("A canonical article URL is required for Search Console synchronization.")
    service = service or service_for_property(prop)
    window_day_count = (end_date - start_date).days + 1
    configured_limit = (
        query_limit
        if query_limit is not None
        else getattr(settings, "CONTENT_ANALYTICS_GSC_QUERY_LIMIT", 100)
    )
    per_day_query_limit = max(0, min(int(configured_limit), 1000))

    aggregate_rows = _query_page_range(
        service,
        site_url=prop.site_url,
        canonical_url=canonical_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=["date"],
        row_limit=window_day_count,
    )
    query_rows = _query_page_range(
        service,
        site_url=prop.site_url,
        canonical_url=canonical_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=["date", "query"],
        row_limit=max(1, min(window_day_count * max(per_day_query_limit, 1), 25000)),
    )

    days: dict[date, SearchConsoleArticleDay] = {}
    current = start_date
    while current <= end_date:
        days[current] = _empty_article_day()
        current += timedelta(days=1)

    for row in aggregate_rows:
        row_day = _row_date(row)
        if row_day not in days:
            continue
        days[row_day] = SearchConsoleArticleDay(
            aggregate={
                "clicks": _decimal(row.get("clicks")),
                "impressions": _decimal(row.get("impressions")),
                "ctr": _decimal(row.get("ctr")),
                "position": _decimal(row.get("position")),
            },
            queries=[],
        )

    queries_by_day: dict[date, dict[str, dict[str, Any]]] = {
        day: {} for day in days
    }
    for row in query_rows:
        row_day = _row_date(row)
        if row_day not in days:
            continue
        keys = row.get("keys") or []
        query = str(keys[1] if len(keys) > 1 else "").strip()
        if not query:
            continue
        query = query[:1024]
        queries_by_day[row_day][query] = {
            "query": query,
            "clicks": _decimal(row.get("clicks")),
            "impressions": _decimal(row.get("impressions")),
            "ctr": _decimal(row.get("ctr")),
            "position": _decimal(row.get("position")),
        }

    for day, query_map in queries_by_day.items():
        ranked_queries = sorted(
            query_map.values(),
            key=lambda row: (
                -row["clicks"],
                -row["impressions"],
                str(row["query"]).casefold(),
            ),
        )[:per_day_query_limit]
        days[day] = SearchConsoleArticleDay(
            aggregate=days[day].aggregate,
            queries=ranked_queries,
        )

    return SearchConsoleArticleWindow(days=days)


def fetch_article_day(
    prop: SearchConsoleProperty,
    article,
    day: date,
    *,
    canonical_url: str = "",
    query_limit: int | None = None,
    service=None,
) -> SearchConsoleArticleDay:
    return fetch_article_window(
        prop,
        article,
        day,
        day,
        canonical_url=canonical_url,
        query_limit=query_limit,
        service=service,
    ).days[day]
