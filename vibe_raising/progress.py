"""Company-scoped progress series and server-owned update chart snapshots.

Observations are the existing source of truth. No provider calls or memo-derived
numbers occur on dashboard reads. Period metadata extends the existing monthly
index without losing exact coverage or mixing accounts and definitions.
"""
from __future__ import annotations

import calendar
import copy
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.conf import settings
from rest_framework.exceptions import ValidationError

from startup_updates.models import StartupMetricObservation, StartupProfile

CATEGORIES = ("audience", "product", "customers", "community", "delivery")
PROVIDER_NAMES = {"xero": "Xero", "financial": "Stripe", "google_analytics": "Google Analytics", "luma": "Luma", "founder_progress": "Founder provided"}
DEFINITIONS = {
    ("xero", "revenue"): ("Income", "customers", "currency", "sum", "Accounting income from Xero Profit and Loss, on an accrual basis."),
    ("xero", "monthlyCosts"): ("Costs", "customers", "currency", "sum", "Costs from the same Xero Profit and Loss reporting basis."),
    ("xero", "netProfitLoss"): ("Net result", "customers", "currency", "sum", "Accounting net result from Xero Profit and Loss."),
    ("financial", "revenue"): ("Paid invoice sales", "customers", "currency", "sum", "Paid Stripe invoice sales excluding tax. Not MRR or whole-business accounting revenue."),
    ("luma", "eventsRun"): ("Events held", "community", "events", "sum", "Ended events returned by the connected Luma calendar."),
    ("luma", "eventRegistrations"): ("Event registrations", "community", "registrations", "sum", "Registrations across ended events; a person attending twice is counted twice."),
    ("luma", "eventAttendees"): ("Recorded check-ins", "community", "check-ins", "sum", "Recorded check-ins across ended events, not unique people or inferred attendance."),
    ("luma", "eventCheckInRate"): ("Check-in rate", "community", "%", "ratio", "Recorded check-ins divided by registrations, for the same ended event set."),
    ("google_analytics", "ga.totalUsers"): ("Website users", "audience", "people", "unique", "GA4 total users for this property and calendar month. Website visitors are not necessarily product users."),
    ("google_analytics", "ga.sessions"): ("Website sessions", "audience", "sessions", "sum", "Sessions reported by GA4 for this property and period."),
    ("google_analytics", "ga.newUsers"): ("New website users", "audience", "people", "unique", "New users reported by GA4 for this property and month."),
    ("google_analytics", "ga.engagementRate"): ("Engagement rate", "audience", "%", "ratio", "Engaged sessions divided by sessions, calculated by GA4 for this month."),
    ("google_analytics", "ga.eventCount"): ("Selected action occurrences", "product", "actions", "sum", "Occurrences of the founder-selected event. Repeated actions by one person are counted separately."),
    ("google_analytics", "ga.actionUsers"): ("Selected action users", "product", "people", "unique", "Distinct GA4 users who triggered the selected event during this month."),
}


def month_end(value):
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def month_shift(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def number(value):
    try:
        result = Decimal(str(value))
        return float(result) if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def definition_for(observation, custom):
    provider, key = observation.source_provider, observation.metric_key
    meta = observation.source_metadata or {}
    if provider == "founder_progress":
        item = custom.get(key)
        if not item or meta.get("definition_version") != item.get("version"):
            return None
        return (item["label"], item["category"], item["unit"], item["aggregation"], item["definition"])
    definition = DEFINITIONS.get((provider, key))
    if not definition:
        return None
    if provider in {"xero", "financial"}:
        from startup_updates.services import _verified_chart_observation
        if meta.get("needs_confirmation") or not _verified_chart_observation(observation):
            return None
    elif provider == "google_analytics" and meta.get("progress_definition_version") != 1:
        return None
    elif provider == "luma":
        if meta.get("calculation_basis") != "luma_events":
            return None
        # Legacy zero check-ins do not prove that attendance was tracked.
        if key in {"eventAttendees", "eventCheckInRate"} and not meta.get("check_in_coverage"):
            return None
    return definition


def series_from_observations(observations, *, configuration, timezone_name, end_date):
    custom = {item["key"]: item for item in configuration.get("definitions", [])}
    grouped = {}
    for obs in observations:
        definition = definition_for(obs, custom)
        if not definition:
            continue
        meta = obs.source_metadata or {}
        label, category, unit, aggregation, explanation = definition
        if unit == "currency":
            unit = obs.unit
            if not unit or len(unit) != 3:
                continue
        try:
            start = date.fromisoformat(meta.get("period_start") or meta.get("report_start_date") or obs.period_month.isoformat())
            observed_date = obs.observed_at.astimezone(ZoneInfo(meta.get("timezone") or timezone_name)).date() if obs.observed_at else end_date
            default_end = min(month_end(obs.period_month), end_date, observed_date)
            end = date.fromisoformat(meta.get("period_end") or meta.get("report_end_date") or default_end.isoformat())
        except (ValueError, TypeError):
            continue
        if end > end_date or start > end or start.replace(day=1) != obs.period_month:
            continue
        scope = {
            "organization": str(getattr(obs, "organization_id", "")),
            "provider": obs.source_provider, "key": obs.metric_key, "unit": unit,
            "timezone": meta.get("timezone") or timezone_name,
            "account": str(meta.get("connection_id") or ",".join(str(key) for key in meta.get("connection_ids", []))),
            "property": str(meta.get("property_id") or ""),
            "event": str(meta.get("event_name") or ""),
            "version": meta.get("definition_version") or meta.get("progress_definition_version") or 1,
            "basis": meta.get("accounting_basis") or meta.get("basis") or meta.get("calculation_basis") or "",
        }
        identifier = "metric_" + hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
        if meta.get("event_name"):
            label = f"{meta.get('event_label') or meta['event_name']} · {'users' if obs.metric_key == 'ga.actionUsers' else 'occurrences'}"
        series = grouped.setdefault(identifier, {
            "id": identifier, "metricKey": obs.metric_key, "label": label, "category": category,
            "unit": unit, "aggregation": aggregation, "definition": explanation, "definitionVersion": scope["version"],
            "provider": obs.source_provider, "source": PROVIDER_NAMES.get(obs.source_provider, obs.source_provider),
            "scope": scope, "scopeLabel": meta.get("property_name") or "", "timezone": scope["timezone"],
            "readOnly": obs.source_provider != "founder_progress", "points": {},
            "limitations": list(meta.get("limitations") or []),
        })
        value = number(obs.value_number)
        key = obs.period_month.isoformat()
        point = {
            "date": key, "periodStart": start.isoformat(), "periodEnd": end.isoformat(),
            "value": value, "partial": end < month_end(obs.period_month),
            "status": "missing" if value is None else "partial" if end < month_end(obs.period_month) else "complete",
            "observedAt": obs.observed_at.isoformat() if obs.observed_at else None,
            "observationId": obs.pk,
        }
        previous = series["points"].get(key)
        # Latest observation wins, never add duplicate updates or source runs.
        if previous is None or (point["observedAt"] or "", obs.pk) > (previous["observedAt"] or "", previous["observationId"]):
            series["points"][key] = point
    result = []
    for item in grouped.values():
        item["points"] = sorted(item["points"].values(), key=lambda point: point["date"])
        item["readiness"] = "ready" if any(p["value"] is not None for p in item["points"]) else "needs_data"
        item["lastSyncedAt"] = max((p["observedAt"] or "" for p in item["points"]), default="") or None
        result.append(item)
    rank = {"revenue": 0, "ga.actionUsers": 1, "ga.totalUsers": 2, "eventRegistrations": 3}
    return sorted(result, key=lambda item: (rank.get(item["metricKey"], 10), item["source"], item["label"], item["id"]))


def get_progress_series(organization, *, end_date=None):
    profile, _ = StartupProfile.objects.get_or_create(organization=organization)
    end_date = end_date or timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date()
    observations = StartupMetricObservation.objects.filter(
        organization=organization, period_month__gte=month_shift(end_date, -23),
        period_month__lte=end_date, source_provider__in=PROVIDER_NAMES,
    ).order_by("period_month", "id")
    return series_from_observations(observations, configuration=profile.progress_configuration or {}, timezone_name=profile.reporting_timezone, end_date=end_date)


def validate_chart_specs(raw):
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValidationError("Choose up to 12 charts.")
    charts, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("Invalid chart selection.")
        ids = item.get("seriesIds")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(key, str) or len(key) > 80 for key in ids) or len(set(ids)) != len(ids):
            raise ValidationError("Choose one to three different series for each chart.")
        chart_id = str(item.get("id") or ids[0])
        if len(chart_id) > 100 or chart_id in seen:
            raise ValidationError("Chart identifiers must be unique.")
        seen.add(chart_id)
        months = item.get("months", 6)
        kind = item.get("type", "line")
        if months not in (3, 6, 12, 24) or kind not in ("line", "bar"):
            raise ValidationError("Choose a supported chart range and style.")
        charts.append({"id": chart_id, "seriesIds": ids, "months": months, "type": kind, "caption": str(item.get("caption") or "")[:280]})
    return charts


def compatible_series(items):
    if len(items) < 2:
        return True
    first = items[0]
    # Financial income/costs and matching Luma event counts are the only
    # combined recipes in this release. Matching units alone are insufficient.
    family = {item["metricKey"] for item in items}
    allowed = family <= {"revenue", "monthlyCosts"} or family <= {"eventRegistrations", "eventAttendees"}
    if not allowed:
        return False
    for item in items[1:]:
        for key in ("provider", "timezone"):
            if item[key] != first[key]:
                return False
        if family <= {"revenue", "monthlyCosts"} and item["unit"] != first["unit"]:
            return False
        for key in ("account", "property", "event", "basis", "version"):
            if item["scope"][key] != first["scope"][key]:
                return False
        if [(p["periodStart"], p["periodEnd"]) for p in item["points"]] != [(p["periodStart"], p["periodEnd"]) for p in first["points"]]:
            return False
    return True


def materialize_charts(specs, series, *, end_date):
    by_id = {item["id"]: item for item in series}
    result = []
    for spec in validate_chart_specs(specs):
        items = [by_id[key] for key in spec["seriesIds"] if key in by_id]
        if len(items) != len(spec["seriesIds"]) or any(item["readiness"] != "ready" for item in items):
            raise ValidationError("A selected metric is no longer available. Review your charts.")
        if not compatible_series(items):
            raise ValidationError("These metrics need separate charts because their units or reporting scopes differ.")
        start = month_shift(end_date, -(spec["months"] - 1)).isoformat()
        selected = []
        for item in items:
            item = copy.deepcopy(item)
            item["points"] = [p for p in item["points"] if start <= p["date"] and p["periodEnd"] <= end_date.isoformat()]
            if not any(p["value"] is not None for p in item["points"]):
                raise ValidationError("A selected chart has no values in this date range.")
            selected.append(item)
        result.append({"spec": spec, "series": selected, "cutoff": end_date.isoformat(), "schemaVersion": 1})
    return result


def charts_for_revision(draft, memo, current):
    """Accept selection only; never accept numerical chart payloads from clients."""
    previous = (current.structured_memo if current else {}).get("progress_charts")
    requested = memo.pop("_progress_chart_specs", None)
    memo.pop("progress_charts", None)
    if requested is None:
        return copy.deepcopy(previous)
    if not getattr(settings, "STARTUP_PROGRESS_ENABLED", False):
        raise ValidationError("Progress is not enabled. Existing chart selections are preserved.")
    specs = validate_chart_specs(requested)
    prior_by_id = {item["spec"]["id"]: item for item in previous or []}
    end = draft.update_date or month_end(draft.month)
    profile = StartupProfile.objects.get(organization=draft.organization)
    end = min(end, timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date())
    current_series = None
    result = []
    for spec in specs:
        prior = prior_by_id.get(spec["id"])
        if prior and prior["spec"] == spec and prior["cutoff"] <= end.isoformat():
            result.append(copy.deepcopy(prior))
        else:
            if current_series is None:
                current_series = get_progress_series(draft.organization, end_date=end)
            result.extend(materialize_charts([spec], current_series, end_date=end))
    return result


def public_chart_payload(charts):
    """Only selected aggregate numbers/definitions; no internal IDs or raw evidence."""
    if charts is None:
        return None
    result = copy.deepcopy(charts)
    for chart in result:
        for series in chart["series"]:
            series.pop("scope", None)
            series.pop("scopeLabel", None)
            for point in series["points"]:
                point.pop("observationId", None)
    return result
