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
from startup_updates.metric_catalog import LUMA_METRIC_KEYS, startup_update_metric_label
from startup_updates.models import LumaEventSelection, StartupMetricObservation

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
EVENT_ATTENDEES_METRIC_KEY = "eventAttendees"
EVENT_CHECK_IN_RATE_METRIC_KEY = "eventCheckInRate"


def resolve_selected_metric_keys(connection: ExternalServiceConnection) -> list[str]:
    """Founder-selected metric keys (in canonical order); defaults to all when unset."""
    raw = (connection.provider_metadata or {}).get("selected_metrics")
    if isinstance(raw, list):
        chosen = {str(item) for item in raw}
        keys = [key for key in LUMA_METRIC_KEYS if key in chosen]
        if keys:
            return keys
    return list(LUMA_METRIC_KEYS)


def selected_event_ids(connection: ExternalServiceConnection) -> Optional[set[str]]:
    """Selected event ids, or None when nothing is selected (→ count all events)."""
    ids = {
        str(event_id).strip()
        for event_id in LumaEventSelection.objects.filter(
            connection=connection, selected=True
        ).values_list("event_id", flat=True)
        if str(event_id).strip()
    }
    return ids or None


def sync_luma_connection(connection: ExternalServiceConnection) -> dict[str, Any]:
    """Pull a founder's selected Luma events and publish their chosen monthly metrics.

    Fetches attendance for the selected events (all past events when none are
    selected), aggregates by month, upserts the selected StartupMetricObservation
    rows, and removes any Luma rows no longer in the computed set. Raises
    LumaConfigurationError / LumaAPIError on failure so the dispatcher can mark
    the connection errored.
    """
    if connection.provider != ExternalServiceProvider.LUMA:
        raise LumaConfigurationError("Connection is not a Luma connection.")
    if not connection.access_token:
        raise LumaConfigurationError("Luma connection is missing an API key.")

    organization = connection.organization
    if organization is None:
        raise LumaConfigurationError("Luma connection is not linked to an organization.")

    event_ids = selected_event_ids(connection)
    metric_keys = resolve_selected_metric_keys(connection)

    service = LumaAttendeeReportService(api_key=connection.access_token)
    events = service.collect_ended_event_attendance(event_ids=event_ids)

    synced_at = timezone.now()
    with transaction.atomic():
        catalog_events = upsert_luma_event_catalog(
            connection=connection,
            events=events,
            synced_at=synced_at,
        )
        metrics = publish_luma_event_metrics(
            organization=organization,
            events=events,
            selected_metrics=metric_keys,
            observed_at=synced_at,
        )
        # Drop Luma rows that are no longer part of the freshly-computed set
        # (deselected metrics, or months whose events are no longer selected).
        kept_ids = [metric.id for metric in metrics]
        StartupMetricObservation.objects.filter(
            organization=organization,
            source_provider=LUMA_METRIC_SOURCE,
            run__isnull=True,
        ).exclude(id__in=kept_ids).delete()

        months_synced = len({metric.period_month for metric in metrics})
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.sync_cursor = {
            **(connection.sync_cursor or {}),
            "last_synced_at": synced_at.isoformat(),
            "luma_events_synced": len(events),
            "luma_catalog_events_synced": catalog_events,
            "luma_months_synced": months_synced,
            "luma_selected_metrics": metric_keys,
            "luma_selected_event_count": (len(event_ids) if event_ids is not None else None),
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
        "catalogEventsSynced": catalog_events,
        "catalog_events_synced": catalog_events,
        "monthsSynced": months_synced,
        "months_synced": months_synced,
        "metrics": metric_keys,
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
    }


def upsert_luma_event_catalog(
    *,
    connection: ExternalServiceConnection,
    events: list[dict[str, Any]],
    synced_at: datetime,
) -> int:
    """Refresh reconciliation's Luma event catalogue from the metric sync payload.

    ``selected`` is intentionally omitted from the defaults so a background sync
    never changes the founder's event-selection preferences.
    """
    synced = 0
    for item in events:
        event = item.get("event") or {}
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            continue
        start_at = item.get("start_at")
        LumaEventSelection.objects.update_or_create(
            connection=connection,
            event_id=event_id,
            defaults={
                "user": connection.user,
                "organization": connection.organization,
                "event_name": str(event.get("name") or event_id).strip()[:255],
                "event_url": str(event.get("url") or "").strip()[:512],
                "start_at": start_at,
                "registration_count": int(item.get("registration_count") or 0),
                "checked_in_count": int(item.get("checked_in_count") or 0),
                "raw_payload": event,
                "last_synced_at": synced_at,
            },
        )
        synced += 1
    return synced


def publish_luma_event_metrics(
    *,
    organization: Organization,
    events: list[dict[str, Any]],
    selected_metrics: Optional[list[str]] = None,
    observed_at: Optional[datetime] = None,
    timezone_name: str = MELBOURNE_TIMEZONE,
) -> list[StartupMetricObservation]:
    """Aggregate Luma events by month and upsert the selected metric observations.

    ``events`` items are ``{"event", "start_at", "registration_count",
    "checked_in_count"}`` (the shape returned by
    ``LumaAttendeeReportService.collect_ended_event_attendance``). ``selected_metrics``
    defaults to all Luma metric keys.
    """
    observed_at = observed_at or timezone.now()
    metric_keys = [key for key in (selected_metrics or LUMA_METRIC_KEYS) if key in LUMA_METRIC_KEYS]
    if not metric_keys:
        metric_keys = list(LUMA_METRIC_KEYS)
    tz = ZoneInfo(timezone_name)

    buckets: dict[date, dict[str, Any]] = defaultdict(
        lambda: {"events": 0, "registrations": 0, "checked_in": 0, "event_ids": [], "event_names": []}
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
        bucket["checked_in"] += int(item.get("checked_in_count") or 0)
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
        registrations = values["registrations"]
        checked_in = values["checked_in"]
        event_ids = list(dict.fromkeys(values["event_ids"]))

        if registrations > 0:
            rate = (Decimal(checked_in) / Decimal(registrations) * Decimal(100)).quantize(Decimal("0.1"))
        else:
            rate = Decimal("0.0")

        # (value_number, value_text, unit) per metric key.
        computed: dict[str, tuple[Decimal, str, str]] = {
            EVENTS_RUN_METRIC_KEY: (Decimal(event_count), str(event_count), ""),
            EVENT_REGISTRATIONS_METRIC_KEY: (Decimal(registrations), str(registrations), ""),
            EVENT_ATTENDEES_METRIC_KEY: (Decimal(checked_in), str(checked_in), ""),
            EVENT_CHECK_IN_RATE_METRIC_KEY: (rate, f"{rate}%", "%"),
        }

        for metric_key in metric_keys:
            value_number, value_text, unit = computed[metric_key]
            metric, _created = StartupMetricObservation.objects.update_or_create(
                organization=organization,
                run=None,
                metric_key=metric_key,
                period_month=month,
                source_provider=LUMA_METRIC_SOURCE,
                defaults={
                    "metric_name": startup_update_metric_label(metric_key),
                    "value_text": value_text,
                    "value_number": value_number,
                    "unit": unit,
                    "observed_at": observed_at,
                    "confidence": 1.0,
                    "source_record_ids": event_ids,
                    "source_metadata": {
                        "calculation_basis": "luma_events",
                        "event_count": event_count,
                        "event_names": values["event_names"],
                    },
                    "summary": _metric_summary(metric_key, value_text, event_count, month),
                },
            )
            metrics.append(metric)
    return metrics


def _metric_summary(metric_key: str, value_text: str, event_count: int, month: date) -> str:
    label = month.strftime("%B %Y")
    event_noun = "event" if event_count == 1 else "events"
    suffix = "(from connected Luma calendar)."
    if metric_key == EVENTS_RUN_METRIC_KEY:
        return f"{value_text} {event_noun} run in {label} {suffix}"
    if metric_key == EVENT_REGISTRATIONS_METRIC_KEY:
        return f"{value_text} registrations across {event_count} {event_noun} in {label} {suffix}"
    if metric_key == EVENT_ATTENDEES_METRIC_KEY:
        return f"{value_text} checked-in attendees across {event_count} {event_noun} in {label} {suffix}"
    if metric_key == EVENT_CHECK_IN_RATE_METRIC_KEY:
        return f"{value_text} check-in rate across {event_count} {event_noun} in {label} {suffix}"
    return f"{value_text} in {label} {suffix}"
