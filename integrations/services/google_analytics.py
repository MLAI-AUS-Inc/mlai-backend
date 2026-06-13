"""Google Analytics 4 data-source service for startup monthly updates.

This module pulls GA4 report bundles for the founder-selected properties via the
GA4 Data API (`properties.runReport`), shapes them into the bundle contract the
Valley harness consumes, and owns the per-run JSON store used by the
`/google-analytics/*` startup-update endpoints.

GA is treated as deterministic web/product analytics context only. The LLM
extraction/curation/draft layers (in Valley) decide monthly-update inclusion; this
module never invents numbers.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any, Optional

from django.utils import timezone

from integrations import http_client as requests
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.external_connectors import (
    ConnectorRateLimitError,
    _google_analytics_required_token,
)
from startup_updates.models import GoogleAnalyticsPropertySelection

logger = logging.getLogger(__name__)

GA4_DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
GA_RUN_STORE_KEY = "startup_update_runs"
GA_REPORT_ROW_LIMIT = 10

# Each spec is one GA4 runReport. `dimension` is the single breakdown dimension
# (None for headline totals only). `metrics` are GA4 Data API metric names.
GA_REPORT_SPECS: list[dict[str, Any]] = [
    {
        "report_type": "traffic_overview",
        "dimension": None,
        "metrics": ["sessions", "totalUsers", "newUsers", "screenPageViews", "engagementRate"],
    },
    {
        "report_type": "acquisition_channels",
        "dimension": "sessionDefaultChannelGroup",
        "metrics": ["sessions", "totalUsers", "keyEvents"],
    },
    {
        "report_type": "top_pages",
        "dimension": "pagePath",
        "metrics": ["screenPageViews", "totalUsers"],
    },
    {
        "report_type": "key_events",
        "dimension": "eventName",
        "metrics": ["keyEvents", "eventCount"],
    },
    {
        "report_type": "engagement",
        "dimension": None,
        "metrics": ["averageSessionDuration", "engagedSessions", "userEngagementDuration"],
    },
]

# Metrics whose movement is most update-worthy; boosts the heuristic score.
_HIGH_SIGNAL_METRICS = {"keyEvents", "sessions", "totalUsers", "newUsers"}
_HIGH_SIGNAL_REPORT_TYPES = {"acquisition_channels", "key_events"}


# ---------------------------------------------------------------------------
# Connection + run-store helpers
# ---------------------------------------------------------------------------
def resolve_google_analytics_connection_for_run(run) -> Optional[ExternalServiceConnection]:
    run_request = run.run_request or {}
    connection_id = run_request.get("google_analytics_connection_id")
    if connection_id:
        connection = (
            ExternalServiceConnection.objects.filter(
                id=connection_id,
                provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
            )
            .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
            .first()
        )
        if connection is not None:
            return connection
    return None


def get_ga_run_store(connection: ExternalServiceConnection, run_id: str) -> dict[str, Any]:
    cursor = connection.sync_cursor if isinstance(connection.sync_cursor, dict) else {}
    runs = cursor.get(GA_RUN_STORE_KEY) if isinstance(cursor.get(GA_RUN_STORE_KEY), dict) else {}
    store = runs.get(run_id) if isinstance(runs.get(run_id), dict) else {}
    store.setdefault("reports", {})
    store.setdefault("classifications", {})
    store.setdefault("extracted_report_ids", [])
    store.setdefault("done_property_ids", [])
    return store


def save_ga_run_store(connection: ExternalServiceConnection, run_id: str, store: dict[str, Any]) -> None:
    cursor = dict(connection.sync_cursor) if isinstance(connection.sync_cursor, dict) else {}
    runs = dict(cursor.get(GA_RUN_STORE_KEY)) if isinstance(cursor.get(GA_RUN_STORE_KEY), dict) else {}
    runs[run_id] = store
    cursor[GA_RUN_STORE_KEY] = runs
    connection.sync_cursor = cursor
    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])


# ---------------------------------------------------------------------------
# Date windows
# ---------------------------------------------------------------------------
def _parse_month_start(raw: Any) -> date:
    text = str(raw or "").strip()
    if text:
        try:
            parsed = date.fromisoformat(text[:10])
            return parsed.replace(day=1)
        except ValueError:
            pass
    today = timezone.now().date()
    return today.replace(day=1)


def _month_bounds(month_start: date) -> tuple[date, date]:
    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    return month_start, month_start.replace(day=last_day)


def _prior_month_start(month_start: date) -> date:
    if month_start.month == 1:
        return month_start.replace(year=month_start.year - 1, month=12, day=1)
    return month_start.replace(month=month_start.month - 1, day=1)


def period_bounds_for_run(run_request: dict[str, Any]) -> tuple[str, str, str, str]:
    current_start = _parse_month_start(run_request.get("current_month"))
    cur_start, cur_end = _month_bounds(current_start)
    prev_start, prev_end = _month_bounds(_prior_month_start(current_start))
    return (cur_start.isoformat(), cur_end.isoformat(), prev_start.isoformat(), prev_end.isoformat())


# ---------------------------------------------------------------------------
# GA4 Data API
# ---------------------------------------------------------------------------
def _retry_after_seconds(response) -> int:
    raw = str(getattr(response, "headers", {}).get("Retry-After") or "").strip()
    if raw:
        try:
            return max(int(float(raw)), 1)
        except (TypeError, ValueError):
            pass
    return 30


def _build_run_report_body(spec: dict[str, Any], start_date: str, end_date: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "dimensions": [{"name": spec["dimension"]}] if spec.get("dimension") else [],
        "metrics": [{"name": metric} for metric in spec["metrics"]],
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "metricAggregations": ["TOTAL"],
        "limit": GA_REPORT_ROW_LIMIT,
        "keepEmptyRows": False,
    }
    if spec.get("dimension") and spec["metrics"]:
        body["orderBys"] = [{"metric": {"metricName": spec["metrics"][0]}, "desc": True}]
    return body


def _fetch_run_report(access_token: str, property_id: str, body: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{GA4_DATA_API_BASE}/properties/{property_id}:runReport",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        json=body,
        timeout=(3, 30),
    )
    if getattr(response, "status_code", 200) == 429:
        raise ConnectorRateLimitError(_retry_after_seconds(response))
    response.raise_for_status()
    return response.json() if response.content else {}


def _to_number(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _metric_headers(report: dict[str, Any]) -> list[str]:
    return [str(header.get("name") or "") for header in report.get("metricHeaders") or []]


def _extract_totals(report: dict[str, Any]) -> dict[str, Optional[float]]:
    headers = _metric_headers(report)
    totals_rows = report.get("totals") or []
    totals: dict[str, Optional[float]] = {}
    if totals_rows:
        metric_values = totals_rows[0].get("metricValues") or []
        for index, header in enumerate(headers):
            value = metric_values[index].get("value") if index < len(metric_values) else None
            totals[header] = _to_number(value)
        return totals
    # Fallback: sum additive metrics across rows when GA omits totals.
    for header in headers:
        totals[header] = None
    for row in report.get("rows") or []:
        metric_values = row.get("metricValues") or []
        for index, header in enumerate(headers):
            value = _to_number(metric_values[index].get("value")) if index < len(metric_values) else None
            if value is None:
                continue
            totals[header] = (totals[header] or 0.0) + value
    return totals


def _extract_rows(report: dict[str, Any], dimension: Optional[str]) -> list[dict[str, Any]]:
    if not dimension:
        return []
    headers = _metric_headers(report)
    rows: list[dict[str, Any]] = []
    for row in report.get("rows") or []:
        dimension_values = row.get("dimensionValues") or []
        metric_values = row.get("metricValues") or []
        entry: dict[str, Any] = {dimension: dimension_values[0].get("value") if dimension_values else ""}
        for index, header in enumerate(headers):
            entry[header] = metric_values[index].get("value") if index < len(metric_values) else None
        rows.append(entry)
    return rows


def _format_number(value: Optional[float]) -> Any:
    if value is None:
        return None
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 4)


def _build_metric_summary(
    spec: dict[str, Any],
    current: dict[str, Any],
    prior: dict[str, Any],
) -> list[dict[str, Any]]:
    current_totals = _extract_totals(current)
    prior_totals = _extract_totals(prior)
    summary: list[dict[str, Any]] = []
    for metric in spec["metrics"]:
        cur_value = current_totals.get(metric)
        prior_value = prior_totals.get(metric)
        delta = None
        delta_pct = None
        if cur_value is not None and prior_value is not None:
            delta = cur_value - prior_value
            if prior_value:
                delta_pct = round(delta / prior_value, 4)
        summary.append(
            {
                "metric_name": metric,
                "value": _format_number(cur_value),
                "prior_value": _format_number(prior_value),
                "delta": _format_number(delta),
                "delta_pct": delta_pct,
                "unit": "",
            }
        )
    return summary


def _heuristic_score(report_type: str, metric_summary: list[dict[str, Any]]) -> tuple[int, list[str]]:
    score = 20
    reasons: list[str] = []
    if report_type in _HIGH_SIGNAL_REPORT_TYPES:
        score += 25
        reasons.append("conversion_or_acquisition_report")
    movement_found = False
    for metric in metric_summary:
        delta_pct = metric.get("delta_pct")
        if delta_pct is None:
            continue
        if abs(delta_pct) >= 0.25:
            boost = 25 if metric.get("metric_name") in _HIGH_SIGNAL_METRICS else 15
            score += boost
            reasons.append(f"large_movement:{metric.get('metric_name')}")
            movement_found = True
            break
    if not movement_found:
        reasons.append("no_material_movement")
    return min(score, 100), reasons


def _build_report_bundle(
    *,
    property_id: str,
    property_name: str,
    spec: dict[str, Any],
    current: dict[str, Any],
    prior: dict[str, Any],
    period_start: str,
    period_end: str,
    comparison_start: str,
    comparison_end: str,
) -> dict[str, Any]:
    metric_summary = _build_metric_summary(spec, current, prior)
    rows = _extract_rows(current, spec.get("dimension"))
    full_row_count = int(current.get("rowCount") or len(rows))
    omitted_row_count = max(full_row_count - len(rows), 0)
    heuristic_score, heuristic_reasons = _heuristic_score(spec["report_type"], metric_summary)
    compression_notes = []
    if omitted_row_count:
        compression_notes.append(f"Top {len(rows)} of {full_row_count} rows retained.")
    return {
        "ga_report_id": f"ga:property:{property_id}:{spec['report_type']}:{period_start}",
        "property_id": property_id,
        "property_name": property_name,
        "report_type": spec["report_type"],
        "date_range_start": period_start,
        "date_range_end": period_end,
        "comparison_range_start": comparison_start,
        "comparison_range_end": comparison_end,
        "dimensions": [spec["dimension"]] if spec.get("dimension") else [],
        "metric_summary": metric_summary,
        "rows": rows,
        "row_count": len(rows),
        "heuristic_score": heuristic_score,
        "heuristic_reasons": heuristic_reasons,
        "relevance_label": "pending",
        "relevance_score": 0.0,
        "relevance_reason": "",
        "extraction_hints": {},
        "omitted_row_count": omitted_row_count,
        "compression_notes": compression_notes,
    }


def _property_display_name(connection: ExternalServiceConnection, property_id: str) -> str:
    selection = GoogleAnalyticsPropertySelection.objects.filter(
        connection=connection,
        property_id=property_id,
    ).first()
    if selection and selection.property_display_name:
        return selection.property_display_name
    return property_id


# ---------------------------------------------------------------------------
# Backfill orchestration (one property per call)
# ---------------------------------------------------------------------------
def run_google_analytics_backfill(run) -> dict[str, Any]:
    connection = resolve_google_analytics_connection_for_run(run)
    if connection is None:
        return {
            "status": "source_unavailable",
            "source": "google_analytics",
            "source_unavailable": True,
            "code": "google_analytics_source_unavailable",
            "warning": "Google Analytics connection is unavailable for this run.",
            "reports_synced": 0,
            "properties_synced": 0,
            "has_more": False,
        }

    run_request = run.run_request or {}
    property_ids = [
        str(property_id).strip()
        for property_id in (run_request.get("google_analytics_property_ids") or [])
        if str(property_id).strip()
    ]
    store = get_ga_run_store(connection, run.run_id)
    done = set(store.get("done_property_ids") or [])

    if not property_ids:
        store["done_property_ids"] = []
        save_ga_run_store(connection, run.run_id, store)
        return {
            "status": "completed",
            "source": "google_analytics",
            "reports_synced": len(store.get("reports") or {}),
            "properties_synced": 0,
            "metrics_synced": 0,
            "has_more": False,
            "warnings": ["No Google Analytics property is selected for this run."],
        }

    pending = [property_id for property_id in property_ids if property_id not in done]
    if not pending:
        return {
            "status": "completed",
            "source": "google_analytics",
            "reports_synced": len(store.get("reports") or {}),
            "properties_synced": len(done & set(property_ids)),
            "metrics_synced": sum(len(b.get("metric_summary") or []) for b in (store.get("reports") or {}).values()),
            "has_more": False,
            "warnings": [],
        }

    property_id = pending[0]
    access_token = _google_analytics_required_token(connection)
    cur_start, cur_end, prev_start, prev_end = period_bounds_for_run(run_request)
    property_name = _property_display_name(connection, property_id)

    reports = dict(store.get("reports") or {})
    try:
        for spec in GA_REPORT_SPECS:
            current = _fetch_run_report(access_token, property_id, _build_run_report_body(spec, cur_start, cur_end))
            prior = _fetch_run_report(access_token, property_id, _build_run_report_body(spec, prev_start, prev_end))
            bundle = _build_report_bundle(
                property_id=property_id,
                property_name=property_name,
                spec=spec,
                current=current,
                prior=prior,
                period_start=cur_start,
                period_end=cur_end,
                comparison_start=prev_start,
                comparison_end=prev_end,
            )
            reports[bundle["ga_report_id"]] = bundle
    except ConnectorRateLimitError as exc:
        store["reports"] = reports
        save_ga_run_store(connection, run.run_id, store)
        return {
            "status": "rate_limited",
            "source": "google_analytics",
            "reports_synced": len(reports),
            "properties_synced": len(done & set(property_ids)),
            "has_more": True,
            "retry_after_seconds": exc.retry_after_seconds,
            "warnings": [],
        }

    done.add(property_id)
    store["reports"] = reports
    store["done_property_ids"] = sorted(done & set(property_ids))
    save_ga_run_store(connection, run.run_id, store)

    remaining = [property_id for property_id in property_ids if property_id not in done]
    has_more = bool(remaining)
    return {
        "status": "syncing" if has_more else "completed",
        "source": "google_analytics",
        "reports_synced": len(reports),
        "properties_synced": len(done & set(property_ids)),
        "metrics_synced": sum(len(b.get("metric_summary") or []) for b in reports.values()),
        "properties": [{"propertyId": pid} for pid in property_ids],
        "has_more": has_more,
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Classification / extraction batch selection over the run store
# ---------------------------------------------------------------------------
def build_classification_batch(store: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    reports = store.get("reports") or {}
    classifications = store.get("classifications") or {}
    pending = [
        bundle
        for report_id, bundle in reports.items()
        if report_id not in classifications
    ]
    pending.sort(key=lambda bundle: bundle.get("heuristic_score", 0), reverse=True)
    return pending[: max(int(limit or 0), 0)] if limit else pending


def apply_classification_results(store: dict[str, Any], results: list[dict[str, Any]]) -> int:
    classifications = dict(store.get("classifications") or {})
    updated = 0
    for item in results or []:
        report_id = str(item.get("ga_report_id") or "").strip()
        if not report_id:
            continue
        classifications[report_id] = {
            "relevance_label": item.get("relevance_label"),
            "relevance_score": item.get("relevance_score"),
            "relevance_reason": item.get("relevance_reason", ""),
            "needs_extraction": bool(item.get("needs_extraction")),
            "extraction_hints": item.get("extraction_hints") or {},
        }
        updated += 1
    store["classifications"] = classifications
    return updated


def build_extraction_batch(
    store: dict[str, Any],
    limit: int,
    *,
    extractable_labels: set[str],
) -> list[dict[str, Any]]:
    reports = store.get("reports") or {}
    classifications = store.get("classifications") or {}
    extracted = set(store.get("extracted_report_ids") or [])
    pending: list[dict[str, Any]] = []
    for report_id, bundle in reports.items():
        if report_id in extracted:
            continue
        classification = classifications.get(report_id)
        if not classification:
            continue
        if str(classification.get("relevance_label") or "") not in extractable_labels:
            continue
        if not classification.get("needs_extraction"):
            continue
        merged = dict(bundle)
        merged["relevance_label"] = classification.get("relevance_label")
        merged["relevance_score"] = classification.get("relevance_score") or 0.0
        merged["relevance_reason"] = classification.get("relevance_reason") or ""
        merged["extraction_hints"] = classification.get("extraction_hints") or {}
        pending.append(merged)
    pending.sort(key=lambda bundle: bundle.get("heuristic_score", 0), reverse=True)
    return pending[: max(int(limit or 0), 0)] if limit else pending
