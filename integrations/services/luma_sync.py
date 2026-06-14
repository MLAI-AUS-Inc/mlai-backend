from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from organizations.models import Organization
from startup_updates.metric_catalog import startup_update_metric_label
from startup_updates.models import StartupMetricObservation

from ..models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from .luma import (
    MELBOURNE_TIMEZONE,
    LumaAttendeeReportService,
    LumaConfigurationError,
)

logger = logging.getLogger(__name__)

# Stored in StartupMetricObservation.source_provider; matches the provider value.
LUMA_METRIC_SOURCE = "luma"

EVENTS_RUN_METRIC_KEY = "eventsRun"
EVENT_REGISTRATIONS_METRIC_KEY = "eventRegistrations"


def sync_luma_connection(connection: ExternalServiceConnection) -> dict[str, Any]:
    """Pull a founder's Luma events and publish monthly event metrics.

    Mirrors the finance connector: fetch from the provider, aggregate by month,
    upsert StartupMetricObservation rows (idempotent), then update connection
    state. Raises LumaConfigurationError / LumaAPIError on failure so the sync
    dispatcher can mark the connection as errored.
    """
    if connection.provider != ExternalServiceProvider.LUMA:
        raise LumaConfigurationError("Connection is not a Luma connection.")
    if not connection.access_token:
        raise LumaConfigurationError("Luma connection is missing an API key.")

    organization = connection.organization
    if organization is None:
        raise LumaConfigurationError("Luma connection is not linked to an organization.")

    service = LumaAttendeeReportService(api_key=connection.access_token)
    events = service.collect_ended_event_registrations()

    synced_at = timezone.now()
    with transaction.atomic():
        metrics = publish_luma_event_metrics(
            organization=organization,
            events=events,
            observed_at=synced_at,
        )
        months_synced = len({metric.period_month for metric in metrics})
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.sync_cursor = {
            **(connection.sync_cursor or {}),
            "last_synced_at": synced_at.isoformat(),
            "luma_events_synced": len(events),
            "luma_months_synced": months_synced,
        }
        connection.save(
            update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"]
        )

    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "eventsSynced": len(events),
        "events_synced": len(events),
        "monthsSynced": months_synced,
        "months_synced": months_synced,
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
    }


def publish_luma_event_metrics(
    *,
    organization: Organization,
    events: list[dict[str, Any]],
    observed_at: Optional[datetime] = None,
    timezone_name: str = MELBOURNE_TIMEZONE,
) -> list[StartupMetricObservation]:
    """Aggregate Luma events by month and upsert event-count / registration metrics.

    ``events`` is the list returned by
    ``LumaAttendeeReportService.collect_ended_event_registrations`` — each item is
    ``{"event": <raw>, "start_at": <datetime>, "registration_count": <int>}``.
    """
    observed_at = observed_at or timezone.now()
    tz = ZoneInfo(timezone_name)

    buckets: dict[date, dict[str, Any]] = defaultdict(
        lambda: {"events": 0, "registrations": 0, "event_ids": [], "event_names": []}
    )
    for item in events:
        start_at = item.get("start_at")
        if start_at is None:
            continue
        local_start = start_at.astimezone(tz)
        month = date(local_start.year, local_start.month, 1)
        bucket = buckets[month]
        bucket["events"] += 1
        bucket["registrations"] += int(item.get("registration_count") or 0)
        event = item.get("event") or {}
        event_id = str(event.get("id") or "").strip()
        if event_id:
            bucket["event_ids"].append(event_id)
        event_name = str(event.get("name") or "").strip()
        if event_name:
            bucket["event_names"].append(event_name)

    metrics: list[StartupMetricObservation] = []
    for month, values in sorted(buckets.items()):
        event_count = values["events"]
        registration_count = values["registrations"]
        event_ids = list(dict.fromkeys(values["event_ids"]))
        for metric_key, value in (
            (EVENTS_RUN_METRIC_KEY, event_count),
            (EVENT_REGISTRATIONS_METRIC_KEY, registration_count),
        ):
            metric, _created = StartupMetricObservation.objects.update_or_create(
                organization=organization,
                run=None,
                metric_key=metric_key,
                period_month=month,
                source_provider=LUMA_METRIC_SOURCE,
                defaults={
                    "metric_name": startup_update_metric_label(metric_key),
                    "value_text": str(value),
                    "value_number": Decimal(value),
                    "unit": "",
                    "observed_at": observed_at,
                    "confidence": 1.0,
                    "source_record_ids": event_ids,
                    "source_metadata": {
                        "calculation_basis": "luma_events",
                        "event_count": event_count,
                        "event_names": values["event_names"],
                    },
                    "summary": _metric_summary(metric_key, value, event_count, month),
                },
            )
            metrics.append(metric)
    return metrics


def _metric_summary(metric_key: str, value: int, event_count: int, month: date) -> str:
    label = month.strftime("%B %Y")
    if metric_key == EVENTS_RUN_METRIC_KEY:
        noun = "event" if value == 1 else "events"
        return f"{value} {noun} run in {label} (from connected Luma calendar)."
    noun = "registration" if value == 1 else "registrations"
    event_noun = "event" if event_count == 1 else "events"
    return (
        f"{value} {noun} across {event_count} {event_noun} in {label} "
        "(from connected Luma calendar)."
    )
