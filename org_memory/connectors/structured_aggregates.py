from __future__ import annotations

import base64
import json
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.external_connectors import sync_xero_connection
from integrations.services.finance import sync_stripe_connection
from integrations.services.luma import LumaAttendeeReportService
from org_memory.models import (
    MemoryClassification,
    MemoryScopeStatus,
    MemorySource,
    StructuredAggregateArtifact,
    StructuredAggregateState,
)
from org_memory.wake_control import suppress_artifact_wakes
from startup_updates.models import LumaEventSelection

from .artifact_utils import (
    bounded_text,
    canonical_hash,
    content_hash,
    estimate_tokens,
    source_acl,
    version_key,
)
from .base import (
    ConnectorHealth,
    DryRunResult,
    ScopeDescriptor,
    ScopePage,
    SourcePreview,
    SourceVersionPayload,
    SyncPage,
    TombstoneResult,
)


STRIPE_AGGREGATES = {
    "invoice_revenue": "Invoice revenue",
    "cash_collected": "Cash collected",
    "invoice_count": "Paid and open invoice count",
    "mrr": "Monthly recurring revenue",
    "active_subscriptions": "Active subscriptions",
}
XERO_AGGREGATES = {
    "invoice_revenue": "Sales invoice revenue",
    "cash_collected": "Cash collected",
    "invoice_count": "Sales invoice count",
    "mrr": "Repeating-invoice MRR",
    "recurring_invoice_count": "Active repeating invoice count",
}
LUMA_AGGREGATES = {
    "events_run": "Events run",
    "event_registrations": "Event registrations",
    "event_attendees": "Checked-in attendees",
    "event_check_in_rate": "Event check-in rate",
}

FINANCE_SOURCE_TYPES = {
    ExternalServiceProvider.STRIPE: "stripe_metric",
    ExternalServiceProvider.XERO: "xero_metric",
}


class StructuredAggregateProviderError(RuntimeError):
    pass


def _page_size() -> int:
    return max(
        min(int(getattr(settings, "ORG_MEMORY_STRUCTURED_PAGE_SIZE", 100)), 500),
        1,
    )


def _stale_seconds() -> int:
    return max(
        int(getattr(settings, "ORG_MEMORY_STRUCTURED_STALE_SECONDS", 90000)),
        60,
    )


def _cutoff_date(configuration) -> date:
    if configuration.historical_cutoff:
        return configuration.historical_cutoff.date()
    days = max(
        min(int(getattr(settings, "ORG_MEMORY_STRUCTURED_BACKFILL_DAYS", 730)), 3650),
        1,
    )
    return (timezone.now() - timedelta(days=days)).date()


def _encode_state(value: Mapping) -> str:
    raw = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_state(value: Optional[str]) -> dict:
    if not value:
        return {"version": 1, "mode": "idle"}
    try:
        padding = "=" * (-len(value) % 4)
        result = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode(
                "utf-8"
            )
        )
    except Exception as exc:
        raise ValueError("Structured aggregate cursor is invalid.") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise ValueError("Structured aggregate cursor is invalid.")
    return result


def _aggregate_definitions(provider: str) -> dict[str, str]:
    if provider == ExternalServiceProvider.STRIPE:
        return STRIPE_AGGREGATES
    if provider == ExternalServiceProvider.XERO:
        return XERO_AGGREGATES
    if provider == ExternalServiceProvider.LUMA:
        return LUMA_AGGREGATES
    raise ValueError("Structured aggregate provider is unsupported.")


def aggregate_scope_ids(provider: str):
    return frozenset(_aggregate_definitions(str(provider)))


def _selected_scopes(configuration, selected_scopes=None):
    provider = str(configuration.provider)
    scopes = list(
        selected_scopes
        if selected_scopes is not None
        else configuration.source_scopes.filter(selected=True, status="selected")
    )
    definitions = _aggregate_definitions(provider)
    if provider in {ExternalServiceProvider.STRIPE, ExternalServiceProvider.XERO}:
        aggregates = {}
        for scope in scopes:
            external_id = str(scope.external_id or "").strip()
            if scope.scope_type != "aggregate" or external_id not in definitions:
                raise ValueError(
                    f"{provider.title()} memory requires explicit supported aggregate scopes."
                )
            aggregates[external_id] = scope
        if not aggregates:
            raise ValueError(f"{provider.title()} memory requires an approved aggregate.")
        return {"aggregates": aggregates, "events": {}}

    aggregates = {}
    events = {}
    for scope in scopes:
        external_id = str(scope.external_id or "").strip()
        if scope.scope_type == "aggregate" and external_id in definitions:
            aggregates[external_id] = scope
        elif scope.scope_type == "event" and external_id:
            events[external_id] = scope
        else:
            raise ValueError("Luma memory supports explicit event and aggregate scopes only.")
    if not events:
        raise ValueError("Luma memory requires at least one approved event scope.")
    return {"aggregates": aggregates, "events": events}


def _connection_ready(configuration) -> bool:
    connection = configuration.connection
    status = str(connection.status or "connected")
    if status == ExternalServiceConnectionStatus.DISCONNECTED:
        return False
    if configuration.provider in {
        ExternalServiceProvider.STRIPE,
        ExternalServiceProvider.LUMA,
    }:
        return bool(str(connection.access_token or "").strip())
    return bool(
        str(connection.external_account_id or "").strip()
        and (
            str(connection.access_token or "").strip()
            or str(connection.refresh_token or "").strip()
        )
    )


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _month_end(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1) - timedelta(days=1)
    return date(value.year, value.month + 1, 1) - timedelta(days=1)


def _as_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _format_value(value: Decimal, unit: str) -> str:
    if unit == "%":
        return f"{value.quantize(Decimal('0.1'))}%"
    if unit == "count":
        return str(int(value))
    amount = value.quantize(Decimal("0.01"))
    return f"{unit} {amount}".strip()


def _decimal_text(value) -> Optional[str]:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _monthly_normalized_amount(record: ExternalFinancialRecord) -> Decimal:
    amount = _as_decimal(record.amount)
    category = str(record.category or "")
    if category == "monthly_normalized":
        return amount
    if not category.startswith("recurrence:"):
        return amount
    _prefix, unit, raw_period = (category.split(":", 2) + ["1", "1"])[:3]
    try:
        period = Decimal(raw_period)
    except (InvalidOperation, ValueError):
        period = Decimal("1")
    if period <= 0:
        period = Decimal("1")
    unit = unit.upper()
    if unit in {"YEAR", "YEARLY"}:
        return amount / (Decimal("12") * period)
    if unit in {"WEEK", "WEEKLY"}:
        return amount * Decimal("52") / Decimal("12") / period
    if unit in {"DAY", "DAILY"}:
        return amount * Decimal("365") / Decimal("12") / period
    return amount / period


def _record_hash(record: ExternalFinancialRecord) -> str:
    return canonical_hash(
        {
            "provider": record.provider,
            "record_type": record.record_type,
            "external_record_id": record.external_record_id,
        }
    )


def _upsert_artifact(
    configuration,
    scope,
    *,
    scan_id,
    source_type,
    external_id,
    metric_key="",
    name,
    period_start=None,
    period_end=None,
    value_number=None,
    value_text="",
    unit="",
    dimensions=None,
    occurred_at=None,
    source_updated_at=None,
    volatile_until=None,
):
    dimensions = dict(dimensions or {})
    revision = canonical_hash(
        {
            "provider": configuration.provider,
            "source_type": source_type,
            "external_id": external_id,
            "metric_key": metric_key,
            "period_start": period_start,
            "period_end": period_end,
            "value_number": value_number,
            "value_text": value_text,
            "unit": unit,
            "dimensions": dimensions,
        }
    )
    artifact, _created = StructuredAggregateArtifact.objects.update_or_create(
        configuration=configuration,
        source_type=source_type,
        external_id=external_id,
        defaults={
            "organization": configuration.organization,
            "source_scope": scope,
            "provider": configuration.provider,
            "metric_key": metric_key,
            "name": bounded_text(name, 255),
            "period_start": period_start,
            "period_end": period_end,
            "value_number": value_number,
            "value_text": bounded_text(value_text, 255),
            "unit": bounded_text(unit, 32),
            "dimensions": dimensions,
            "source_revision": revision,
            "occurred_at": occurred_at,
            "source_updated_at": source_updated_at,
            "volatile_until": volatile_until,
            "stale_after": timezone.now() + timedelta(seconds=_stale_seconds()),
            "lifecycle_state": StructuredAggregateState.ACTIVE,
            "scan_generation": scan_id,
            "last_seen_at": timezone.now(),
            "removed_at": None,
        },
    )
    return artifact


def _finance_artifacts(configuration, scopes, scan_id):
    connection = configuration.connection
    provider = configuration.provider
    definitions = _aggregate_definitions(provider)
    selected = scopes["aggregates"]
    records = list(
        ExternalFinancialRecord.objects.filter(
            organization=configuration.organization,
            connection=connection,
            provider=provider,
        )
        .filter(
            Q(transaction_date__gte=_cutoff_date(configuration))
            | Q(
                record_type__in=(
                    "stripe_subscription",
                    ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
                )
            )
        )
        .order_by("transaction_date", "pk")
    )
    now = timezone.now()
    buckets = defaultdict(list)
    recurring = []
    for record in records:
        if provider == ExternalServiceProvider.STRIPE:
            if record.record_type == "stripe_subscription":
                recurring.append(record)
                continue
            if record.record_type != "stripe_invoice" or not record.transaction_date:
                continue
        else:
            if record.record_type == ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE:
                recurring.append(record)
                continue
            if record.record_type not in {
                ExternalFinancialRecord.RECORD_XERO_INVOICE,
                ExternalFinancialRecord.RECORD_XERO_PAYMENT,
            } or not record.transaction_date:
                continue
        buckets[(_month_start(record.transaction_date), record.currency.upper() or "AUD")].append(record)

    artifacts = []
    source_type = FINANCE_SOURCE_TYPES[provider]
    for (month, currency), month_records in sorted(buckets.items()):
        invoices = [
            row
            for row in month_records
            if row.record_type in {"stripe_invoice", ExternalFinancialRecord.RECORD_XERO_INVOICE}
            and str(row.status or "").upper()
            not in {"DRAFT", "DELETED", "VOID", "VOIDED", "UNCOLLECTIBLE"}
        ]
        payments = [
            row
            for row in month_records
            if (
                row.record_type == ExternalFinancialRecord.RECORD_XERO_PAYMENT
                or (
                    row.record_type == "stripe_invoice"
                    and str(row.status or "").lower() == "paid"
                )
            )
        ]
        metrics = {
            "invoice_revenue": (sum((_as_decimal(row.amount) for row in invoices), Decimal("0")), invoices, currency),
            "cash_collected": (sum((abs(_as_decimal(row.amount)) for row in payments), Decimal("0")), payments, currency),
            "invoice_count": (Decimal(len(invoices)), invoices, "count"),
        }
        for metric_key, (value, evidence, unit) in metrics.items():
            scope = selected.get(metric_key)
            if scope is None or not evidence:
                continue
            source_hashes = sorted(_record_hash(row) for row in evidence)
            latest = max((row.updated_at for row in evidence), default=now)
            artifacts.append(
                _upsert_artifact(
                    configuration,
                    scope,
                    scan_id=scan_id,
                    source_type=source_type,
                    external_id=f"{metric_key}:{month.isoformat()}:{currency}",
                    metric_key=metric_key,
                    name=definitions[metric_key],
                    period_start=month,
                    period_end=_month_end(month),
                    value_number=value,
                    value_text=_format_value(value, unit),
                    unit=unit,
                    dimensions={
                        "calculation_basis": "sanitized_external_financial_records",
                        "record_count": len(evidence),
                        "currency": currency,
                        "source_record_hashes": source_hashes,
                        "current_period": month == _month_start(now.date()),
                    },
                    occurred_at=datetime.combine(
                        _month_end(month),
                        time.max,
                        tzinfo=datetime_timezone.utc,
                    ),
                    source_updated_at=latest,
                    volatile_until=(
                        datetime.combine(
                            _month_end(month),
                            time.max,
                            tzinfo=datetime_timezone.utc,
                        )
                        if month == _month_start(now.date())
                        else None
                    ),
                )
            )

    current_month = _month_start(now.date())
    if provider == ExternalServiceProvider.STRIPE:
        active_statuses = {"ACTIVE", "TRIALING"}
    else:
        active_statuses = {"AUTHORISED", "ACTIVE"}
    active_recurring = [
        row
        for row in recurring
        if str(row.status or "").upper() in active_statuses
    ]
    by_currency = defaultdict(list)
    for row in active_recurring:
        by_currency[row.currency.upper() or "AUD"].append(row)

    # MRR is currency-specific. Do not invent a currency when the account has
    # never supplied one; retain previously observed currencies long enough to
    # emit an authoritative zero after the last recurring record disappears.
    prior_currencies = (
        StructuredAggregateArtifact.objects.filter(
            configuration=configuration,
            metric_key="mrr",
        )
        .exclude(unit="")
        .values_list("unit", flat=True)
        .distinct()
    )
    for currency in prior_currencies:
        by_currency.setdefault(str(currency).upper(), [])
    for currency, currency_records in sorted(by_currency.items()):
        scope = selected.get("mrr")
        if scope is None:
            continue
        value = sum(
            (_monthly_normalized_amount(row) for row in currency_records),
            Decimal("0"),
        )
        artifacts.append(
            _upsert_artifact(
                configuration,
                scope,
                scan_id=scan_id,
                source_type=source_type,
                external_id=f"mrr:{current_month.isoformat()}:{currency}",
                metric_key="mrr",
                name=definitions["mrr"],
                period_start=current_month,
                period_end=_month_end(current_month),
                value_number=value,
                value_text=_format_value(value, currency),
                unit=currency,
                dimensions={
                    "calculation_basis": "active_sanitized_recurring_records",
                    "record_count": len(currency_records),
                    "currency": currency,
                    "source_record_hashes": sorted(
                        _record_hash(row) for row in currency_records
                    ),
                    "current_period": True,
                },
                occurred_at=now,
                source_updated_at=max(
                    (row.updated_at for row in currency_records),
                    default=connection.last_synced_at or now,
                ),
                volatile_until=datetime.combine(
                    _month_end(current_month),
                    time.max,
                    tzinfo=datetime_timezone.utc,
                ),
            )
        )

    count_key = (
        "active_subscriptions"
        if provider == ExternalServiceProvider.STRIPE
        else "recurring_invoice_count"
    )
    count_scope = selected.get(count_key)
    if count_scope is not None:
        value = Decimal(len(active_recurring))
        artifacts.append(
            _upsert_artifact(
                configuration,
                count_scope,
                scan_id=scan_id,
                source_type=source_type,
                external_id=f"{count_key}:{current_month.isoformat()}:count",
                metric_key=count_key,
                name=definitions[count_key],
                period_start=current_month,
                period_end=_month_end(current_month),
                value_number=value,
                value_text=_format_value(value, "count"),
                unit="count",
                dimensions={
                    "calculation_basis": "active_sanitized_recurring_records",
                    "record_count": len(active_recurring),
                    "source_record_hashes": sorted(
                        _record_hash(row) for row in active_recurring
                    ),
                    "current_period": True,
                },
                occurred_at=now,
                source_updated_at=max(
                    (row.updated_at for row in active_recurring),
                    default=connection.last_synced_at or now,
                ),
                volatile_until=datetime.combine(
                    _month_end(current_month),
                    time.max,
                    tzinfo=datetime_timezone.utc,
                ),
            )
        )
    return artifacts


def _safe_event_values(selection, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    event_id = str(payload.get("id") or selection.event_id)
    name = bounded_text(payload.get("name") or selection.event_name or event_id, 255)
    public_url = bounded_text(payload.get("url") or selection.event_url, 512)
    start_at = parse_datetime(str(payload.get("start_at") or "")) or selection.start_at
    end_at = parse_datetime(str(payload.get("end_at") or ""))
    venue = ""
    for candidate in (
        payload.get("geo_address_json"),
        payload.get("geo_address_info"),
        payload.get("location"),
    ):
        if isinstance(candidate, dict):
            venue = str(
                candidate.get("full_address")
                or candidate.get("address")
                or candidate.get("description")
                or ""
            )
        elif isinstance(candidate, str):
            venue = candidate
        if venue:
            break
    return {
        "event_id": event_id,
        "name": name,
        "public_url": public_url,
        "start_at": start_at,
        "end_at": end_at,
        "venue": bounded_text(venue, 500),
    }


def _luma_artifacts(configuration, scopes, scan_id):
    connection = configuration.connection
    event_ids = set(scopes["events"])
    service = LumaAttendeeReportService(api_key=connection.access_token)
    returned = service.collect_ended_event_attendance(event_ids=event_ids)
    selections = {
        row.event_id: row
        for row in LumaEventSelection.objects.filter(
            connection=connection,
            event_id__in=event_ids,
        )
    }
    now = timezone.now()
    artifacts = []
    returned_ids = set()
    monthly = defaultdict(
        lambda: {
            "events": 0,
            "registrations": 0,
            "attendees": 0,
            "event_hashes": [],
            "source_updated_at": None,
        }
    )
    tz = ZoneInfo(str(getattr(settings, "ORG_MEMORY_LUMA_TIMEZONE", "Australia/Melbourne")))
    for item in returned:
        payload = item.get("event") if isinstance(item.get("event"), dict) else {}
        event_id = str(payload.get("id") or "").strip()
        if event_id not in event_ids:
            continue
        returned_ids.add(event_id)
        selection = selections.get(event_id)
        if selection is None:
            provider_updated_at = parse_datetime(str(payload.get("updated_at") or ""))
            provider_start_at = parse_datetime(str(payload.get("start_at") or ""))
            selection = SimpleNamespace(
                event_id=event_id,
                event_name=bounded_text(payload.get("name") or event_id, 255),
                event_url=bounded_text(payload.get("url"), 512),
                start_at=provider_start_at,
                raw_payload={},
                last_synced_at=None,
                updated_at=provider_updated_at or provider_start_at or connection.created_at,
            )
            selections[event_id] = selection
        safe = _safe_event_values(selection, payload)
        start_at = safe["start_at"] or item.get("start_at")
        source_updated_at = (
            parse_datetime(str(payload.get("updated_at") or ""))
            or getattr(selection, "last_synced_at", None)
            or selection.updated_at
            or start_at
        )
        registrations = max(int(item.get("registration_count") or 0), 0)
        attendees = max(int(item.get("checked_in_count") or 0), 0)
        rate = (
            (Decimal(attendees) / Decimal(registrations) * Decimal("100"))
            if registrations
            else Decimal("0")
        )
        dims = {
            **safe,
            "start_at": start_at.isoformat() if start_at else None,
            "end_at": safe["end_at"].isoformat() if safe["end_at"] else None,
            "registration_count": registrations,
            "attendance_count": attendees,
            "check_in_rate": str(rate.quantize(Decimal("0.1"))),
            "attendee_pii_included": False,
        }
        artifacts.append(
            _upsert_artifact(
                configuration,
                scopes["events"][event_id],
                scan_id=scan_id,
                source_type="luma_event",
                external_id=f"event:{event_id}",
                name=safe["name"],
                dimensions=dims,
                occurred_at=start_at,
                source_updated_at=source_updated_at,
                volatile_until=(safe["end_at"] if safe["end_at"] and safe["end_at"] > now else None),
            )
        )
        if start_at:
            month = date(start_at.astimezone(tz).year, start_at.astimezone(tz).month, 1)
            bucket = monthly[month]
            bucket["events"] += 1
            bucket["registrations"] += registrations
            bucket["attendees"] += attendees
            bucket["event_hashes"].append(canonical_hash(event_id))
            if source_updated_at and (
                bucket["source_updated_at"] is None
                or source_updated_at > bucket["source_updated_at"]
            ):
                bucket["source_updated_at"] = source_updated_at

    # Upcoming selected events are not returned by the ended-event attendance
    # endpoint. Preserve only their whitelisted identity/date metadata.
    for event_id, selection in selections.items():
        if event_id in returned_ids or not selection.start_at or selection.start_at < now:
            continue
        safe = _safe_event_values(selection, selection.raw_payload)
        dims = {
            **safe,
            "start_at": selection.start_at.isoformat(),
            "end_at": safe["end_at"].isoformat() if safe["end_at"] else None,
            "registration_count": None,
            "attendance_count": None,
            "check_in_rate": None,
            "attendee_pii_included": False,
        }
        artifacts.append(
            _upsert_artifact(
                configuration,
                scopes["events"][event_id],
                scan_id=scan_id,
                source_type="luma_event",
                external_id=f"event:{event_id}",
                name=safe["name"],
                dimensions=dims,
                occurred_at=selection.start_at,
                source_updated_at=selection.updated_at,
                volatile_until=safe["end_at"] or selection.start_at,
            )
        )

    for month, values in sorted(monthly.items()):
        registrations = values["registrations"]
        computed = {
            "events_run": (Decimal(values["events"]), "count"),
            "event_registrations": (Decimal(registrations), "count"),
            "event_attendees": (Decimal(values["attendees"]), "count"),
            "event_check_in_rate": (
                Decimal(values["attendees"]) / Decimal(registrations) * Decimal("100")
                if registrations
                else Decimal("0"),
                "%",
            ),
        }
        for metric_key, (value, unit) in computed.items():
            scope = scopes["aggregates"].get(metric_key)
            if scope is None:
                continue
            artifacts.append(
                _upsert_artifact(
                    configuration,
                    scope,
                    scan_id=scan_id,
                    source_type="luma_metric",
                    external_id=f"{metric_key}:{month.isoformat()}",
                    metric_key=metric_key,
                    name=LUMA_AGGREGATES[metric_key],
                    period_start=month,
                    period_end=_month_end(month),
                    value_number=value,
                    value_text=_format_value(value, unit),
                    unit=unit,
                    dimensions={
                        "calculation_basis": "selected_luma_events",
                        "event_count": values["events"],
                        "source_event_hashes": sorted(values["event_hashes"]),
                        "attendee_pii_included": False,
                    },
                    occurred_at=datetime.combine(
                        _month_end(month),
                        time.max,
                        tzinfo=datetime_timezone.utc,
                    ),
                    source_updated_at=values["source_updated_at"],
                    volatile_until=(
                        datetime.combine(
                            _month_end(month),
                            time.max,
                            tzinfo=tz,
                        )
                        if month == _month_start(now.astimezone(tz).date())
                        else None
                    ),
                )
            )
    return artifacts


def _refresh_artifacts(configuration, scopes):
    if not _connection_ready(configuration):
        raise StructuredAggregateProviderError("Provider connection is not accessible.")
    # Upstream syncs save financial rows. Those saves normally wake memory for
    # provider-webhook/manual changes, but this reconciliation must not enqueue
    # itself again through its own post-save signals.
    with suppress_artifact_wakes():
        if configuration.provider == ExternalServiceProvider.STRIPE:
            sync_stripe_connection(configuration.connection)
        elif configuration.provider == ExternalServiceProvider.XERO:
            sync_xero_connection(configuration.connection)
    scan_id = uuid.uuid4()
    with transaction.atomic():
        if configuration.provider in {
            ExternalServiceProvider.STRIPE,
            ExternalServiceProvider.XERO,
        }:
            artifacts = _finance_artifacts(configuration, scopes, scan_id)
        else:
            artifacts = _luma_artifacts(configuration, scopes, scan_id)
        now = timezone.now()
        StructuredAggregateArtifact.objects.filter(
            configuration=configuration,
            lifecycle_state=StructuredAggregateState.ACTIVE,
        ).exclude(scan_generation=scan_id).update(
            lifecycle_state=StructuredAggregateState.REMOVED,
            removed_at=now,
            last_seen_at=now,
            updated_at=now,
        )
    return scan_id, artifacts


def _active_artifacts(configuration):
    return StructuredAggregateArtifact.objects.filter(
        configuration=configuration,
        lifecycle_state=StructuredAggregateState.ACTIVE,
        source_scope__selected=True,
        source_scope__status=MemoryScopeStatus.SELECTED,
    ).select_related("source_scope")


def _classification(configuration, scope):
    if configuration.provider in {
        ExternalServiceProvider.STRIPE,
        ExternalServiceProvider.XERO,
    }:
        return MemoryClassification.FINANCE
    return scope.default_classification


def _artifact_text(artifact):
    if artifact.source_type == "luma_event":
        values = artifact.dimensions or {}
        rows = [f"Event: {artifact.name}"]
        for label, key in (
            ("Start", "start_at"),
            ("End", "end_at"),
            ("Venue", "venue"),
            ("Public URL", "public_url"),
            ("Registrations", "registration_count"),
            ("Checked-in attendees", "attendance_count"),
            ("Check-in rate", "check_in_rate"),
        ):
            value = values.get(key)
            if value not in {None, ""}:
                rows.append(f"{label}: {value}")
        return "\n".join(rows)
    rows = [
        f"Metric: {artifact.name}",
        f"Provider: {artifact.provider}",
        f"Period: {artifact.period_start.isoformat() if artifact.period_start else 'current'}",
        f"Value: {artifact.value_text}",
    ]
    return "\n".join(rows)


def _artifact_record(configuration, artifact):
    scope = artifact.source_scope
    if scope is None:
        return None
    text = _artifact_text(artifact)
    acl = source_acl(
        configuration,
        scope,
        revision_payload={
            "artifact_revision": artifact.source_revision,
            "lifecycle_state": artifact.lifecycle_state,
        },
    )
    acl["metadata"] = {
        **dict(acl.get("metadata") or {}),
        "aggregate_only": True,
        "attendee_pii_included": False,
    }
    payload = {
        "content_hash": content_hash(text),
        "source_revision": artifact.source_revision,
        "acl": acl,
        "adapter": "structured-aggregate-v1",
    }
    dimensions = dict(artifact.dimensions or {})
    dimensions.pop("source_record_hashes", None)
    dimensions.pop("source_event_hashes", None)
    canonical_url = (
        bounded_text(dimensions.get("public_url"), 2048)
        if artifact.source_type == "luma_event"
        else ""
    )
    return {
        "source_scope_id": scope.pk,
        "source_type": artifact.source_type,
        "external_id": artifact.external_id,
        "version_key": version_key(payload),
        "content_hash": payload["content_hash"],
        "classification": _classification(configuration, scope),
        "acl": acl,
        "chunks": (
            {
                "ordinal": 0,
                "chunk_kind": "structured_aggregate",
                "text": text,
                "token_count": estimate_tokens(text),
                "source_locator": {
                    "aggregate_id": str(artifact.pk),
                    "source_type": artifact.source_type,
                    "metric_key": artifact.metric_key,
                    "period_start": artifact.period_start.isoformat()
                    if artifact.period_start
                    else None,
                    "unit": artifact.unit,
                },
                "occurred_at": artifact.occurred_at,
            },
        ),
        "canonical_url": canonical_url,
        "title": bounded_text(artifact.name, 512),
        "author_external_id": artifact.provider,
        "source_created_at": artifact.occurred_at or artifact.created_at,
        "source_updated_at": artifact.source_updated_at or artifact.updated_at,
        "occurred_at": artifact.occurred_at,
        "bounded_excerpt": text[:4096],
        "metadata": {
            "record_type": artifact.source_type,
            "aggregate_only": True,
            "metric_key": artifact.metric_key,
            "period_start": artifact.period_start.isoformat()
            if artifact.period_start
            else None,
            "period_end": artifact.period_end.isoformat()
            if artifact.period_end
            else None,
            "value_number": _decimal_text(artifact.value_number),
            "value_text": artifact.value_text,
            "unit": artifact.unit,
            "dimensions": dimensions,
            "volatile_until": artifact.volatile_until.isoformat()
            if artifact.volatile_until
            else None,
            "stale_after": artifact.stale_after.isoformat()
            if artifact.stale_after
            else None,
            "attendee_pii_included": False,
            "authority_fields": [artifact.metric_key]
            if artifact.metric_key
            else ["event_identity", "event_date"],
            "structured_fact": {
                "kind": "metric" if artifact.metric_key else "event",
                "predicate": artifact.metric_key or "event_details",
                "value": _decimal_text(artifact.value_number)
                if artifact.value_number is not None
                else artifact.name,
                "value_text": artifact.value_text,
                "unit": artifact.unit,
                "statement": text[:4000],
            },
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _expected_sources(configuration):
    return set(
        _active_artifacts(configuration).values_list("source_type", "external_id")
    )


def _removals(configuration, *, revoke_access=False):
    expected = _expected_sources(configuration)
    removals = []
    for source_type, external_id in (
        MemorySource.objects.filter(configuration=configuration)
        .exclude(lifecycle_state="tombstoned")
        .values_list("source_type", "external_id")
    ):
        if (str(source_type), str(external_id)) in expected:
            continue
        removals.append(
            {
                "source_type": str(source_type),
                "external_id": str(external_id),
                "reason": (
                    "provider_access_lost"
                    if revoke_access
                    else "aggregate_removed_or_outside_approved_scope"
                ),
                "revoke_access": bool(revoke_access),
            }
        )
    return tuple(removals)


def _access_lost_page(configuration, *, next_cursor=None):
    now = timezone.now()
    StructuredAggregateArtifact.objects.filter(
        configuration=configuration,
        lifecycle_state=StructuredAggregateState.ACTIVE,
    ).update(
        lifecycle_state=StructuredAggregateState.ACCESS_LOST,
        removed_at=now,
        last_seen_at=now,
        updated_at=now,
    )
    return SyncPage(
        records=(),
        removals=_removals(configuration, revoke_access=True),
        next_cursor=next_cursor,
        checkpoint={"mode": "access_lost", "reconciled_at": now.isoformat()},
        has_more=False,
    )


class StructuredAggregateMemoryConnector:
    def __init__(self, provider):
        self.provider = str(provider)

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        if cursor:
            raise ValueError("Structured aggregate discovery is not paginated.")
        connection = configuration.connection
        account = str(connection.external_account_id or connection.account_label or "")
        descriptors = [
            ScopeDescriptor(
                scope_type="aggregate",
                external_id=key,
                name=label,
                metadata={
                    "aggregate_only": True,
                    "account": account[:255],
                    "attendee_pii_allowed": False,
                },
            )
            for key, label in _aggregate_definitions(self.provider).items()
        ]
        if self.provider == ExternalServiceProvider.LUMA:
            descriptors.extend(
                ScopeDescriptor(
                    scope_type="event",
                    external_id=row.event_id,
                    name=row.event_name or row.event_id,
                    canonical_url=row.event_url,
                    metadata={
                        "event_metadata_only": True,
                        "start_at": row.start_at.isoformat() if row.start_at else None,
                        "attendee_pii_allowed": False,
                    },
                )
                for row in LumaEventSelection.objects.filter(
                    connection=connection
                ).order_by("-start_at", "event_id")[:500]
            )
        return ScopePage(
            scopes=tuple(descriptors),
            warnings=(
                "Only sanitized aggregates are available; raw records and attendee identities are excluded.",
            ),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scopes(configuration, selected_scopes)
        return SourcePreview(
            summary={
                "aggregate_scope_count": len(scopes["aggregates"]),
                "event_scope_count": len(scopes["events"]),
                "durable_aggregate_count": _active_artifacts(configuration).count(),
                "content_activated": False,
                "raw_financial_records_included": False,
                "attendee_pii_included": False,
            },
            warnings=(
                "Authoritative counts are established by the approved backfill/poll.",
            ),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        _selected_scopes(configuration, selected_scopes)
        samples = list(
            _active_artifacts(configuration).values(
                "source_type", "external_id", "metric_key", "period_start"
            )[:10]
        )
        return DryRunResult(
            summary={
                "sample_artifacts": len(samples),
                "samples": samples,
                "active_memory_created": False,
            },
            warnings=(
                "Dry-run reads sanitized aggregate metadata and creates no memory sources.",
            ),
        )

    def _emit(self, configuration, state, *, persist_cursor):
        offset = max(int(state.get("offset") or 0), 0)
        rows = list(
            _active_artifacts(configuration).order_by(
                "source_type", "period_start", "external_id"
            )[offset : offset + _page_size() + 1]
        )
        has_more = len(rows) > _page_size()
        if has_more:
            rows = rows[: _page_size()]
        records = tuple(
            record
            for record in (_artifact_record(configuration, row) for row in rows)
            if record
        )
        next_state = {
            "version": 1,
            "mode": "structured_emit",
            "scan_id": str(state.get("scan_id") or ""),
            "offset": offset + len(rows),
        }
        if not has_more:
            next_state = {
                "version": 1,
                "mode": "idle",
                "last_sync_at": timezone.now().isoformat(),
            }
        return SyncPage(
            records=records,
            removals=() if has_more else _removals(configuration),
            next_cursor=_encode_state(next_state) if persist_cursor else None,
            checkpoint=next_state,
            has_more=has_more,
        )

    def _start_refresh(self, configuration, scopes):
        scan_id, _artifacts = _refresh_artifacts(configuration, scopes)
        return {
            "version": 1,
            "mode": "structured_emit",
            "scan_id": str(scan_id),
            "offset": 0,
        }

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        scopes = _selected_scopes(configuration, selected_scopes)
        if not _connection_ready(configuration):
            return _access_lost_page(configuration)
        state = (
            dict(checkpoint)
            if (checkpoint or {}).get("mode") == "structured_emit"
            else self._start_refresh(configuration, scopes)
        )
        # Persist every continuation and the final idle cursor. Without this,
        # multi-page backfills can leave the configuration pinned to the first
        # emission page and skip the next scheduled reconciliation.
        return self._emit(configuration, state, persist_cursor=True)

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        scopes = _selected_scopes(configuration)
        if not _connection_ready(configuration):
            return _access_lost_page(configuration, next_cursor=cursor)
        state = _decode_state(cursor)
        if state.get("mode") != "structured_emit":
            state = self._start_refresh(configuration, scopes)
        return self._emit(configuration, state, persist_cursor=True)

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage:
        scopes = _selected_scopes(configuration)
        if not _connection_ready(configuration):
            return _access_lost_page(
                configuration,
                next_cursor=configuration.sync_cursor or None,
            )
        state = (
            dict(checkpoint)
            if (checkpoint or {}).get("mode") == "structured_emit"
            else self._start_refresh(configuration, scopes)
        )
        return replace(self._emit(configuration, state, persist_cursor=False), next_cursor=None)

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload:
        artifact = _active_artifacts(configuration).filter(
            external_id=str(external_id or "")
        ).first()
        if artifact is None:
            raise ValueError("Structured aggregate is outside the active approved inventory.")
        record = _artifact_record(configuration, artifact)
        if record is None:
            raise ValueError("Structured aggregate has no active approved scope.")
        return SourceVersionPayload(
            external_id=record["external_id"],
            canonical_url=record["canonical_url"],
            version_key=record["version_key"],
            source_times={
                "created_at": record["source_created_at"],
                "modified_at": record["source_updated_at"],
            },
            metadata=record["metadata"],
            acl=record["acl"],
            content=record["bounded_excerpt"],
        )

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        removals = _removals(configuration)
        return TombstoneResult(
            tombstoned_external_ids=tuple(row["external_id"] for row in removals)
        )

    def health(self, configuration) -> ConnectorHealth:
        last_sync = configuration.last_successful_sync_at
        now = timezone.now()
        stale_count = _active_artifacts(configuration).filter(
            stale_after__lte=now
        ).count()
        status = str(configuration.connection.status or "connected")
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=("connected" if _connection_ready(configuration) else status),
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=(
                max(int((now - last_sync).total_seconds()), 0) if last_sync else None
            ),
            details={
                "active_aggregates": _active_artifacts(configuration).count(),
                "stale_aggregates": stale_count,
                "aggregate_only": True,
                "attendee_pii_included": False,
                "daily_fallback_seconds": int(
                    getattr(settings, "ORG_MEMORY_SYNC_INTERVAL_SECONDS", 86400)
                ),
            },
        )
