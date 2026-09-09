from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import ContentFactoryRun, ContentFactoryRunStatus, Organization
from integrations import http_client as requests
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.external_connectors import (
    ConnectorConfigurationError,
    ConnectorOAuthError,
    serialize_source_status,
    sync_basiq_connection,
    sync_xero_connection,
)
from startup_updates.models import StartupMetricObservation

logger = logging.getLogger(__name__)

FINANCIAL_MONTHLY_METRICS_WORKFLOW = "financial_monthly_metrics"
FINANCIAL_STEP_ORDER = [
    "profile_resolution",
    "financial_backfill",
    "financial_incremental_sync",
    "revenue_calculation",
    "timeline_publish",
]

STRIPE_API_BASE_URL = "https://api.stripe.com"
STRIPE_RECORD_INVOICE = "stripe_invoice"
STRIPE_RECORD_SUBSCRIPTION = "stripe_subscription"
FINANCIAL_METRIC_SOURCE = "financial"
FINANCIAL_OPEN_RUN_STATUSES = {
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
    ContentFactoryRunStatus.BLOCKED,
}


def get_financial_status(*, organization: Optional[Organization] = None, user=None) -> dict[str, Any]:
    payload = serialize_source_status(user, financial_only=True) if user is not None else {"sources": [], "connections": []}
    if organization is not None:
        payload["domain"] = organization.domain
        payload["organization_id"] = organization.id
        payload["organizationId"] = organization.id
    return payload


def create_financial_sync_run(
    *,
    organization: Organization,
    user,
    connection_ids: Optional[Iterable[int]] = None,
    trigger: str = "manual",
) -> tuple[ContentFactoryRun, bool]:
    ids = [int(item) for item in (connection_ids or []) if str(item).strip()]
    existing = (
        ContentFactoryRun.objects.filter(
            workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
            domain=organization.domain,
            status__in=FINANCIAL_OPEN_RUN_STATUSES,
        )
        .order_by("-updated_at")
        .first()
    )
    if existing:
        request_payload = dict(existing.run_request or {})
        if ids:
            request_payload["connection_ids"] = ids
        request_payload["trigger"] = trigger or request_payload.get("trigger") or "manual"
        request_payload["organization_id"] = organization.id
        request_payload["user_id"] = getattr(user, "id", None)
        existing.run_request = request_payload
        existing.save(update_fields=["run_request", "updated_at"])
        return existing, False

    run = ContentFactoryRun.objects.create(
        run_id=f"financial-{uuid.uuid4().hex[:12]}",
        workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
        domain=organization.domain,
        slack_user_id=str(getattr(user, "slack_id", "") or ""),
        status=ContentFactoryRunStatus.QUEUED,
        current_step=FINANCIAL_STEP_ORDER[0],
        step_order=list(FINANCIAL_STEP_ORDER),
        run_request={
            "organization_id": organization.id,
            "user_id": getattr(user, "id", None),
            "connection_ids": ids,
            "trigger": trigger or "manual",
            "financial_only": True,
        },
    )
    return run, True


def enqueue_financial_sync_run(*, organization: Organization, user, trigger: str = "manual") -> Optional[ContentFactoryRun]:
    if user is None:
        return None
    run, _created = create_financial_sync_run(organization=organization, user=user, trigger=trigger)
    return run


def sync_next_financial_page(*, run: ContentFactoryRun) -> dict[str, Any]:
    organization = _run_organization(run)
    user_id = (run.run_request or {}).get("user_id")
    connection_ids = [int(item) for item in (run.run_request or {}).get("connection_ids") or []]
    connections = _financial_connections(organization=organization, user_id=user_id, connection_ids=connection_ids)
    sync_results = []
    errors = []

    for connection in connections:
        try:
            if connection.provider == ExternalServiceProvider.XERO:
                result = sync_xero_connection(connection)
            elif connection.provider == ExternalServiceProvider.BANK_FEED:
                result = sync_basiq_connection(connection)
            elif connection.provider == ExternalServiceProvider.STRIPE:
                result = sync_stripe_connection(connection)
            else:
                result = {
                    "connectionId": connection.id,
                    "connection_id": connection.id,
                    "provider": connection.provider,
                    "status": "skipped",
                }
        except (ConnectorConfigurationError, ConnectorOAuthError, requests.RequestException) as exc:
            logger.exception("Financial connection sync failed", extra={"connection_id": connection.id})
            connection.status = ExternalServiceConnectionStatus.ERROR
            connection.last_error = str(exc) or "Financial sync failed."
            connection.save(update_fields=["status", "last_error", "updated_at"])
            result = {
                "connectionId": connection.id,
                "connection_id": connection.id,
                "provider": connection.provider,
                "status": "error",
                "error": connection.last_error,
            }
            errors.append(connection.last_error)
        else:
            # Some connectors (e.g. Basiq failed jobs) report failure via the
            # result payload instead of raising.
            if isinstance(result, dict) and str(result.get("status") or "") == ExternalServiceConnectionStatus.ERROR:
                errors.append(str(result.get("error") or "") or "Financial sync failed.")
        sync_results.append(result)

    result_payload = {
        **(run.result or {}),
        "sync_results": sync_results,
        "syncResults": sync_results,
        "connection_count": len(connections),
        "connectionCount": len(connections),
        "errors": errors,
    }
    run.result = result_payload
    run.status = ContentFactoryRunStatus.RUNNING if not errors else ContentFactoryRunStatus.BLOCKED
    run.current_step = "revenue_calculation" if not errors else "financial_backfill"
    run.error = "; ".join(errors)
    run.save(update_fields=["result", "status", "current_step", "error", "updated_at"])

    return {
        "status": "error" if errors else "synced",
        "has_more": False,
        "hasMore": False,
        "sync_results": sync_results,
        "syncResults": sync_results,
        "errors": errors,
    }


def calculate_and_publish_monthly_revenue(*, run: ContentFactoryRun) -> dict[str, Any]:
    organization = _run_organization(run)
    metrics = publish_financial_metric_observations(organization=organization, run=run)
    run.result = {
        **(run.result or {}),
        "published_metric_count": len(metrics),
        "publishedMetricCount": len(metrics),
        "metric_ids": [metric.id for metric in metrics],
        "metricIds": [metric.id for metric in metrics],
    }
    run.status = ContentFactoryRunStatus.COMPLETED
    run.current_step = "timeline_publish"
    run.error = ""
    run.save(update_fields=["result", "status", "current_step", "error", "updated_at"])
    return {
        "status": "published",
        "published_metric_count": len(metrics),
        "publishedMetricCount": len(metrics),
        "metrics": [serialize_revenue_snapshot(metric) for metric in metrics],
    }


def publish_financial_metric_observations(*, organization: Organization, run: Optional[ContentFactoryRun] = None) -> list[StartupMetricObservation]:
    """One Revenue metric: Xero books win; never add invoices or bill payments."""
    if ExternalServiceConnection.objects.filter(organization=organization, provider=ExternalServiceProvider.XERO).exclude(status=ExternalServiceConnectionStatus.DISCONNECTED).exists():
        from startup_updates.services import publish_xero_metric_observations
        from startup_updates.models import StartupProfile
        from zoneinfo import ZoneInfo
        profile, _ = StartupProfile.objects.get_or_create(organization=organization)
        today = timezone.now().astimezone(ZoneInfo(profile.reporting_timezone)).date()
        publish_xero_metric_observations(organization=organization, run=run,
            start_date=date(today.year, today.month, 1), end_date=today)
        return list(StartupMetricObservation.objects.filter(
            organization=organization, source_provider=ExternalServiceProvider.XERO, metric_key="revenue",
            source_metadata__source_metric="xero_profit_and_loss_revenue",
        ).order_by("period_month", "unit", "-observed_at"))

    from zoneinfo import ZoneInfo
    from startup_updates.models import StartupProfile
    profile, _ = StartupProfile.objects.get_or_create(organization=organization)
    zone = ZoneInfo(profile.reporting_timezone)
    records = ExternalFinancialRecord.objects.filter(
        organization=organization, provider=ExternalServiceProvider.STRIPE,
        record_type=STRIPE_RECORD_INVOICE, status="paid",
    ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
    buckets = {}
    for record in records:
        paid_at = _timestamp((record.raw_payload or {}).get("status_transitions", {}).get("paid_at"))
        month = paid_at.astimezone(zone).date().replace(day=1) if paid_at else None
        currency = (record.currency or "").upper()
        if not month or not currency:
            continue
        payload = record.raw_payload or {}
        from startup_updates.evidence_contract import stripe_paid_invoice_sales_minor
        minor_amount = stripe_paid_invoice_sales_minor(payload)
        bucket = buckets.setdefault((month, currency), {"amount": Decimal("0"), "ids": [], "unknown": False})
        if minor_amount is None:
            bucket["unknown"] = True
        else:
            bucket["amount"] += _minor_units(minor_amount, currency)
        bucket["ids"].append(_record_source_id(record))
    metrics = []
    for (month, currency), values in sorted(buckets.items()):
        # The semantic key is independent of a workflow retry/run.
        metric = StartupMetricObservation.objects.filter(organization=organization, metric_key="revenue", period_month=month, source_provider=FINANCIAL_METRIC_SOURCE, unit=currency).order_by("-id").first()
        if metric is None:
            metric = StartupMetricObservation(organization=organization, metric_key="revenue", period_month=month, source_provider=FINANCIAL_METRIC_SOURCE, unit=currency)
        metric.run = run
        metric.metric_name = "Revenue"
        metric.value_number = None if values["unknown"] else values["amount"]
        metric.value_text = "" if values["unknown"] else _format_money(values["amount"], currency)
        metric.observed_at = timezone.now()
        metric.source_record_ids = values["ids"]
        metric.source_metadata = {"definition_version": 2, "basis": "paid_stripe_invoice_sales_excluding_tax", "currency": currency, "coverage": "paid_invoices_only", "needs_confirmation": values["unknown"], "limitations": ["Excludes non-invoice payments and refunds issued without credit notes; not whole-business accounting revenue."]}
        metric.summary = "Revenue from paid Stripe invoices excluding tax. Credit adjustments or incomplete totals require confirmation; processor coverage is not reconciled company accounts."
        metric.save()
        metrics.append(metric)
    return metrics


def serialize_revenue_snapshot(metric: StartupMetricObservation) -> dict[str, Any]:
    return {
        "id": metric.id,
        "metric_key": metric.metric_key,
        "metricKey": metric.metric_key,
        "metric_name": metric.metric_name,
        "metricName": metric.metric_name,
        "month": metric.period_month.isoformat(),
        "currency": metric.unit,
        "value_text": metric.value_text,
        "valueText": metric.value_text,
        "value_number": str(metric.value_number) if metric.value_number is not None else None,
        "valueNumber": str(metric.value_number) if metric.value_number is not None else None,
        "confidence": metric.confidence,
        "source_record_ids": metric.source_record_ids or [],
        "sourceRecordIds": metric.source_record_ids or [],
        "calculated_at": metric.updated_at.isoformat(),
        "calculatedAt": metric.updated_at.isoformat(),
    }


def sync_stripe_connection(connection: ExternalServiceConnection) -> dict[str, Any]:
    if connection.provider != ExternalServiceProvider.STRIPE:
        raise ConnectorConfigurationError("Connection is not a Stripe connection.")
    if not connection.access_token:
        raise ConnectorOAuthError("Stripe connection is missing an access token.")

    invoices = _stripe_collection(connection, "/v1/invoices", {"limit": 100})
    subscriptions = _stripe_collection(connection, "/v1/subscriptions", {"limit": 100, "status": "all"})
    synced_at = timezone.now()
    with transaction.atomic():
        invoices_synced = _upsert_stripe_invoices(connection, invoices)
        subscriptions_synced = _upsert_stripe_subscriptions(connection, subscriptions)
        connection.status = ExternalServiceConnectionStatus.CONNECTED
        connection.last_error = ""
        connection.last_synced_at = synced_at
        connection.sync_cursor = {
            **(connection.sync_cursor or {}),
            "last_synced_at": synced_at.isoformat(),
            "stripe_invoices_synced": invoices_synced,
            "stripe_subscriptions_synced": subscriptions_synced,
        }
        connection.save(update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"])

    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
        "invoicesSynced": invoices_synced,
        "invoices_synced": invoices_synced,
        "subscriptionsSynced": subscriptions_synced,
        "subscriptions_synced": subscriptions_synced,
    }


def _run_organization(run: ContentFactoryRun) -> Organization:
    organization_id = (run.run_request or {}).get("organization_id")
    if organization_id:
        return Organization.objects.get(id=organization_id)
    return Organization.objects.get(domain=run.domain)


def _financial_connections(
    *,
    organization: Organization,
    user_id: Optional[int],
    connection_ids: list[int],
) -> list[ExternalServiceConnection]:
    queryset = ExternalServiceConnection.objects.filter(
        organization=organization,
        provider__in=[
            ExternalServiceProvider.STRIPE,
            ExternalServiceProvider.XERO,
            ExternalServiceProvider.BANK_FEED,
        ],
    ).exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
    if user_id:
        queryset = queryset.filter(user_id=user_id)
    if connection_ids:
        queryset = queryset.filter(id__in=connection_ids)
    return list(queryset.order_by("provider", "-updated_at"))


def _stripe_collection(connection: ExternalServiceConnection, path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    request_params = dict(params or {})
    while True:
        response = requests.get(
            f"{STRIPE_API_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {connection.access_token}",
                "Stripe-Version": str(getattr(settings, "STRIPE_API_VERSION", "2026-07-29.dahlia")),
            },
            params=request_params,
            timeout=(3, 20),
        )
        response.raise_for_status()
        payload = response.json()
        page_items = payload.get("data") if isinstance(payload.get("data"), list) else []
        results.extend(item for item in page_items if isinstance(item, dict))
        if not payload.get("has_more"):
            break
        next_cursor = page_items[-1].get("id") if page_items else None
        if not next_cursor or next_cursor == request_params.get("starting_after"):
            raise ValueError("Stripe returned an incomplete page without an advancing cursor.")
        request_params["starting_after"] = next_cursor
    return results


def _upsert_stripe_invoices(connection: ExternalServiceConnection, invoices: list[dict[str, Any]]) -> int:
    count = 0
    for invoice in invoices:
        invoice_id = str(invoice.get("id") or "").strip()
        if not invoice_id:
            continue
        amount = _minor_units(invoice.get("amount_paid"), str(invoice.get("currency") or "USD"))
        occurred_at = _timestamp(invoice.get("status_transitions", {}).get("paid_at") or invoice.get("created"))
        transaction_date = occurred_at.date() if occurred_at else None
        ExternalFinancialRecord.objects.update_or_create(
            connection=connection,
            record_type=STRIPE_RECORD_INVOICE,
            external_record_id=invoice_id,
            defaults={
                "provider": ExternalServiceProvider.STRIPE,
                "user": connection.user,
                "organization": connection.organization,
                "external_account_id": connection.external_account_id,
                "currency": str(invoice.get("currency") or "").upper(),
                "amount": amount,
                "direction": "credit",
                "status": str(invoice.get("status") or ""),
                "posted_at": occurred_at,
                "transaction_date": transaction_date,
                "description": str(invoice.get("description") or invoice.get("number") or ""),
                "merchant_name": _customer_label(invoice),
                "raw_payload": invoice,
            },
        )
        count += 1
    return count


def _upsert_stripe_subscriptions(connection: ExternalServiceConnection, subscriptions: list[dict[str, Any]]) -> int:
    count = 0
    for subscription in subscriptions:
        subscription_id = str(subscription.get("id") or "").strip()
        if not subscription_id:
            continue
        occurred_at = _timestamp(subscription.get("created"))
        ExternalFinancialRecord.objects.update_or_create(
            connection=connection,
            record_type=STRIPE_RECORD_SUBSCRIPTION,
            external_record_id=subscription_id,
            defaults={
                "provider": ExternalServiceProvider.STRIPE,
                "user": connection.user,
                "organization": connection.organization,
                "external_account_id": connection.external_account_id,
                "currency": _subscription_currency(subscription),
                # Sanitized monthly-normalized value lets aggregate consumers
                # avoid the raw Stripe object, which may contain customer data.
                "amount": _subscription_monthly_amount(subscription),
                "direction": "credit",
                "status": str(subscription.get("status") or ""),
                "posted_at": occurred_at,
                "transaction_date": occurred_at.date() if occurred_at else None,
                "description": str(subscription.get("description") or ""),
                "merchant_name": str(subscription.get("customer") or ""),
                "category": "monthly_normalized",
                "class_name": "subscription_mrr",
                "raw_payload": subscription,
            },
        )
        count += 1
    return count


def _record_month(record: ExternalFinancialRecord) -> Optional[date]:
    value = record.transaction_date or (record.posted_at.date() if record.posted_at else None)
    if not value:
        return None
    return date(value.year, value.month, 1)


def _record_source_id(record: ExternalFinancialRecord) -> str:
    return f"{record.provider}:{record.record_type}:{record.external_record_id}"


def _format_money(value: Decimal, currency: str) -> str:
    return f"{currency} {value.quantize(Decimal('0.01'))}"


def _minor_units(value: Any, currency: str = "USD") -> Decimal:
    try:
        exponent = 0 if currency.upper() in {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"} else (3 if currency.upper() in {"BHD", "JOD", "KWD", "OMR", "TND"} else 2)
        return Decimal(str(value or 0)) / (Decimal(10) ** exponent)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _timestamp(value: Any) -> Optional[datetime]:
    if value in ("", None):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        parsed = parse_datetime(str(value))
        return parsed if parsed and parsed.tzinfo else (parsed.replace(tzinfo=dt_timezone.utc) if parsed else None)


def _customer_label(payload: dict[str, Any]) -> str:
    customer = payload.get("customer")
    if isinstance(customer, dict):
        return str(customer.get("name") or customer.get("email") or customer.get("id") or "")
    return str(customer or "")


def _subscription_items(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    items = subscription.get("items") if isinstance(subscription.get("items"), dict) else {}
    data = items.get("data") if isinstance(items.get("data"), list) else []
    return [item for item in data if isinstance(item, dict)]


def _subscription_monthly_amount(subscription: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for item in _subscription_items(subscription):
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        quantity = Decimal(str(item.get("quantity") or 1))
        amount = _minor_units(price.get("unit_amount") or 0) * quantity
        recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else {}
        interval = str(recurring.get("interval") or "month").lower()
        try:
            interval_count = Decimal(str(recurring.get("interval_count") or 1))
        except (InvalidOperation, TypeError, ValueError):
            interval_count = Decimal("1")
        if interval_count <= 0:
            interval_count = Decimal("1")
        if interval == "year":
            amount = amount / (Decimal("12") * interval_count)
        elif interval == "week":
            amount = amount * Decimal("52") / Decimal("12") / interval_count
        elif interval == "day":
            amount = amount * Decimal("365") / Decimal("12") / interval_count
        else:
            amount = amount / interval_count
        total += amount
    return total


def _subscription_currency(subscription: dict[str, Any]) -> str:
    for item in _subscription_items(subscription):
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        currency = str(price.get("currency") or "").upper()
        if currency:
            return currency
    return ""
