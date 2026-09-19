"""Bounded, restartable GA history, independent of update generation.

Monthly unique counts and rates are queried directly at that grain. Separate
properties and selected events always remain separate series.
"""
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo
from time import monotonic

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from integrations.services.google_analytics import _fetch_run_report, _google_analytics_required_token
from startup_updates.models import GoogleAnalyticsPropertySelection, StartupMetricObservation, StartupProfile
from .progress import month_end, month_shift, number


def monthly_rows(report, metric_names, *, start, end):
    """Rows are already calendar-month aggregates; no client-side summation."""
    rows = report.get("rows") or []
    if int(report.get("rowCount") or len(rows)) > len(rows):
        raise ValidationError("Google Analytics returned incomplete history. Please retry.")
    result = []
    for row in rows:
        raw = str((row.get("dimensionValues") or [{}])[0].get("value", ""))
        try:
            month = date(int(raw[:4]), int(raw[4:6]), 1)
        except (ValueError, TypeError):
            continue
        if len(raw) != 6 or not start <= month <= end:
            continue
        for index, metric in enumerate(metric_names):
            values = row.get("metricValues") or []
            value = number(values[index].get("value")) if index < len(values) else None
            if value is not None:
                result.append((month, metric, value * 100 if metric == "engagementRate" else value))
    return result


def sync_progress_google_analytics(organization, request):
    property_id = str(request.get("propertyId") or "")
    selection = GoogleAnalyticsPropertySelection.objects.select_related("connection").filter(
        organization=organization, connection__organization=organization,
        property_id=property_id, selected=True,
    ).exclude(connection__status="disconnected").first()
    if selection is None:
        raise ValidationError("Choose a connected Google Analytics property for this startup.")
    profile, _ = StartupProfile.objects.get_or_create(organization=organization)
    config = profile.progress_configuration or {}
    expected_version = request.get("expectedVersion")
    if expected_version != config.get("version", 0):
        from .progress_views import ProgressConflict
        raise ProgressConflict()
    today = timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date()
    start = month_shift(today, -23)
    deadline = monotonic() + 45
    token = _google_analytics_required_token(selection.connection)
    def fetch(body):
        remaining = deadline - monotonic()
        if remaining < 2:
            raise ValidationError("Google Analytics took too long. Please retry; your saved history is unchanged.")
        return _fetch_run_report(token, property_id, body, timeout=(min(3, remaining / 2), min(10, remaining / 2)))
    base = {"dimensions": [{"name": "yearMonth"}], "dateRanges": [{"startDate": start.isoformat(), "endDate": today.isoformat()}], "limit": 100, "keepEmptyRows": False}
    names = ["totalUsers", "sessions", "newUsers", "engagementRate"]
    reports = [(names, "", "", fetch({**base, "metrics": [{"name": name} for name in names]}))]
    # Discover every observed event, with bounded pages and an explicit failure
    # instead of presenting a truncated top-ten list as the full catalogue.
    events, offset = [], 0
    while True:
        report = fetch({
            "dimensions": [{"name": "eventName"}], "metrics": [{"name": "eventCount"}],
            "dateRanges": base["dateRanges"], "limit": 250, "offset": offset,
            "orderBys": [{"dimension": {"dimensionName": "eventName"}}],
        })
        rows = report.get("rows") or []
        events.extend(str((row.get("dimensionValues") or [{}])[0].get("value") or "") for row in rows)
        offset += len(rows)
        if offset >= int(report.get("rowCount") or 0):
            break
        if not rows or offset >= 2000:
            raise ValidationError("This property has too many events for automatic discovery. Choose a narrower property in Connections.")
    mapping = request.get("eventName")
    existing = (config.get("ga_mappings") or {}).get(property_id, {})
    event_name = str(mapping if mapping is not None else existing.get("eventName") or "")
    event_label = str(request.get("eventLabel") or existing.get("label") or event_name).strip()[:70]
    if event_name:
        if event_name not in events:
            raise ValidationError("Choose an event observed in this property before naming your action.")
        names = ["eventCount", "totalUsers"]
        reports.append((names, event_name, event_label, fetch({
            **base, "metrics": [{"name": name} for name in names],
            "dimensionFilter": {"filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": event_name, "caseSensitive": True}}},
        })))
    synced_at = timezone.now()
    count = 0
    with transaction.atomic():
        profile = StartupProfile.objects.select_for_update().get(pk=profile.pk)
        config = dict(profile.progress_configuration or {})
        if config.get("version", 0) != expected_version:
            from .progress_views import ProgressConflict
            raise ProgressConflict()
        for names, event, label, report in reports:
            if [item.get("name") for item in report.get("metricHeaders", [])] != names:
                raise ValidationError("Google Analytics returned an unexpected report. Saved history is unchanged.")
            metadata = report.get("metadata") or {}
            source_zone = metadata.get("timeZone") or profile.reporting_timezone
            limitations = ["Google Analytics may revise recent data."]
            if metadata.get("subjectToThresholding"):
                limitations.append("Google Analytics applied privacy thresholds to this report.")
            if metadata.get("samplingMetadatas"):
                limitations.append("Google Analytics sampled this report.")
            if metadata.get("dataLossFromOtherRow"):
                limitations.append("Some high-cardinality detail is grouped into an other row by Google Analytics.")
            rows = monthly_rows(report, names, start=start, end=today)
            # Only this property/event is replaced atomically, preserving other
            # scopes and last-good data if an upstream query failed.
            existing_rows = StartupMetricObservation.objects.filter(organization=organization,
                source_provider="google_analytics", source_metadata__progress_definition_version=1,
                source_metadata__property_id=property_id, source_metadata__event_name=event)
            kept = []
            for month, metric, value in rows:
                key = "ga.actionUsers" if event and metric == "totalUsers" else "ga." + metric
                record, _ = existing_rows.update_or_create(metric_key=key, period_month=month, defaults={
                    "organization": organization, "source_provider": "google_analytics", "source_thread": None, "run": None,
                    "metric_name": label or metric, "value_number": Decimal(str(value)), "value_text": str(value),
                    "unit": "%" if metric == "engagementRate" else "count", "observed_at": synced_at, "confidence": 1,
                    "source_metadata": {"progress_definition_version": 1, "connection_id": selection.connection_id,
                        "property_id": property_id, "property_name": selection.property_display_name,
                        "event_name": event, "event_label": label, "period_start": month.isoformat(),
                        "period_end": min(month_end(month), today).isoformat(), "timezone": source_zone,
                        "limitations": limitations},
                })
                kept.append(record.pk)
                count += 1
            existing_rows.exclude(pk__in=kept).delete()
        config.setdefault("ga_events", {})[property_id] = sorted(set(filter(None, events)))
        config.setdefault("ga_mappings", {})[property_id] = {"eventName": event_name, "label": event_label}
        config["version"] = config.get("version", 0) + 1
        profile.progress_configuration = config
        profile.save(update_fields=["progress_configuration", "updated_at"])
    return {"status": "completed", "observations": count, "lastSyncedAt": synced_at.isoformat()}
