from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from django.utils import timezone

from integrations.models import GoogleConnection
from integrations.services.gmail import get_refreshed_credentials


GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
BASELINE_GOOGLE_SCOPES = [GSC_SCOPE, GA4_SCOPE]


def google_connection_for_user(user):
    return GoogleConnection.objects.filter(user=user).first()


def google_connection_has_baseline_scope(connection: Optional[GoogleConnection]) -> bool:
    if not connection:
        return False
    scopes = set(str(connection.scope or "").split())
    return set(BASELINE_GOOGLE_SCOPES).issubset(scopes)


def google_baseline_connection_status(user) -> Dict[str, Any]:
    connection = google_connection_for_user(user)
    if not connection:
        return {
            "connected": False,
            "hasBaselineScopes": False,
            "email": None,
            "status": "needs_connection",
        }
    has_scopes = google_connection_has_baseline_scope(connection)
    return {
        "connected": True,
        "hasBaselineScopes": has_scopes,
        "email": connection.google_email,
        "status": "connected" if has_scopes else "needs_reconnect",
    }


def collect_verified_google_metrics(
    *,
    user,
    domain: str,
    ga4_property_id: Optional[str] = None,
    now=None,
) -> Dict[str, Any]:
    connection = google_connection_for_user(user)
    if not connection:
        return _needs_connection("Connect Google Search Console or GA4 to verify traffic.")
    if not google_connection_has_baseline_scope(connection):
        return _needs_connection("Reconnect Google with Search Console or Analytics read-only access.")

    metrics: Dict[str, Any] = {
        "status": "measured",
        "verified": True,
        "googleSearchConsole": {},
        "googleAnalytics": {},
    }
    source_status = {
        "googleSearchConsole": "unavailable",
        "googleAnalytics": "needs_connection" if not ga4_property_id else "unavailable",
    }
    reference = now or timezone.now()
    end_date = reference.date() - timedelta(days=1)
    start_28 = end_date - timedelta(days=27)
    start_90 = end_date - timedelta(days=89)

    gsc = _collect_search_console(connection, domain, start_28=start_28, start_90=start_90, end_date=end_date)
    metrics["googleSearchConsole"] = gsc
    source_status["googleSearchConsole"] = gsc.get("status", "unavailable")

    if ga4_property_id:
        ga4 = _collect_ga4(connection, ga4_property_id, start_28=start_28, end_date=end_date)
        metrics["googleAnalytics"] = ga4
        source_status["googleAnalytics"] = ga4.get("status", "unavailable")
    else:
        metrics["googleAnalytics"] = {
            "status": "needs_connection",
            "message": "Select or provide a GA4 property ID to include verified user metrics.",
        }

    measured_sources = [value for value in source_status.values() if value == "measured"]
    if not measured_sources:
        metrics["status"] = "needs_connection" if "needs_connection" in source_status.values() else "error"
    metrics["sourceStatus"] = source_status
    metrics["score"] = _traffic_score(metrics)
    return {"traffic": metrics, "sourceStatus": source_status}


def _needs_connection(message: str) -> Dict[str, Any]:
    return {
        "traffic": {
            "status": "needs_connection",
            "verified": False,
            "score": None,
            "message": message,
        },
        "sourceStatus": {
            "googleSearchConsole": "needs_connection",
            "googleAnalytics": "needs_connection",
        },
    }


def _collect_search_console(connection: GoogleConnection, domain: str, *, start_28, start_90, end_date) -> Dict[str, Any]:
    try:
        from googleapiclient.discovery import build

        service = build("searchconsole", "v1", credentials=get_refreshed_credentials(connection), cache_discovery=False)
        site_url = _match_search_console_site(service, domain)
        if not site_url:
            return {
                "status": "needs_connection",
                "message": "No verified Search Console property matched this domain.",
            }
        summary_28 = _query_search_console(service, site_url, start_28, end_date)
        summary_90 = _query_search_console(service, site_url, start_90, end_date)
        top_queries = _query_search_console(service, site_url, start_28, end_date, dimensions=["query"], row_limit=10)
        top_pages = _query_search_console(service, site_url, start_28, end_date, dimensions=["page"], row_limit=10)
        return {
            "status": "measured",
            "siteUrl": site_url,
            "last28Days": summary_28,
            "last90Days": summary_90,
            "topQueries": top_queries.get("rows", []),
            "topPages": top_pages.get("rows", []),
        }
    except Exception as exc:
        return {"status": "error", "message": f"Search Console lookup failed: {exc}"}


def _match_search_console_site(service, domain: str) -> Optional[str]:
    normalized = str(domain or "").strip().lower().removeprefix("www.")
    candidates = {
        f"sc-domain:{normalized}",
        f"https://{normalized}/",
        f"https://www.{normalized}/",
        f"http://{normalized}/",
        f"http://www.{normalized}/",
    }
    sites = service.sites().list().execute().get("siteEntry", [])
    for site in sites:
        site_url = str(site.get("siteUrl") or "")
        if site_url in candidates:
            return site_url
    for site in sites:
        site_url = str(site.get("siteUrl") or "")
        if normalized and normalized in site_url.lower():
            return site_url
    return None


def _query_search_console(service, site_url: str, start_date, end_date, *, dimensions=None, row_limit=1) -> Dict[str, Any]:
    body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "rowLimit": row_limit,
    }
    if dimensions:
        body["dimensions"] = dimensions
    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = response.get("rows") or []
    totals = {
        "clicks": 0,
        "impressions": 0,
        "ctr": 0,
        "position": 0,
        "rows": [],
    }
    for row in rows:
        payload = {
            "keys": row.get("keys") or [],
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0),
            "position": row.get("position", 0),
        }
        totals["rows"].append(payload)
        totals["clicks"] += payload["clicks"]
        totals["impressions"] += payload["impressions"]
    if rows:
        totals["ctr"] = sum(float(row.get("ctr") or 0) for row in rows) / len(rows)
        totals["position"] = sum(float(row.get("position") or 0) for row in rows) / len(rows)
    return totals


def _collect_ga4(connection: GoogleConnection, property_id: str, *, start_28, end_date) -> Dict[str, Any]:
    try:
        from googleapiclient.discovery import build

        clean_property_id = str(property_id or "").replace("properties/", "").strip()
        service = build("analyticsdata", "v1beta", credentials=get_refreshed_credentials(connection), cache_discovery=False)
        body = {
            "dateRanges": [{"startDate": start_28.isoformat(), "endDate": end_date.isoformat()}],
            "metrics": [
                {"name": "activeUsers"},
                {"name": "newUsers"},
                {"name": "sessions"},
                {"name": "screenPageViews"},
                {"name": "engagementRate"},
            ],
        }
        response = service.properties().runReport(property=f"properties/{clean_property_id}", body=body).execute()
        values = [metric.get("value") for metric in ((response.get("rows") or [{}])[0].get("metricValues") or [])]
        return {
            "status": "measured",
            "propertyId": clean_property_id,
            "last28Days": {
                "activeUsers": _number(values, 0),
                "newUsers": _number(values, 1),
                "sessions": _number(values, 2),
                "screenPageViews": _number(values, 3),
                "engagementRate": _number(values, 4),
            },
        }
    except Exception as exc:
        return {"status": "error", "message": f"GA4 lookup failed: {exc}"}


def _number(values, index: int) -> float:
    try:
        return float(values[index])
    except (IndexError, TypeError, ValueError):
        return 0


def _traffic_score(metrics: Dict[str, Any]) -> Optional[int]:
    gsc = metrics.get("googleSearchConsole") or {}
    ga4 = metrics.get("googleAnalytics") or {}
    if gsc.get("status") != "measured" and ga4.get("status") != "measured":
        return None
    clicks = ((gsc.get("last28Days") or {}).get("clicks") or 0) if gsc.get("status") == "measured" else 0
    users = ((ga4.get("last28Days") or {}).get("activeUsers") or 0) if ga4.get("status") == "measured" else 0
    score = min(100, 20 + min(40, clicks / 25) + min(40, users / 100))
    return int(round(score))
