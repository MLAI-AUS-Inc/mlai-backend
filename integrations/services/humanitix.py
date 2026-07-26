"""Read-only Humanitix catalogue and financial aggregate sync.

The public Humanitix API exposes event, order, and ticket data.  Buyer and
attendee fields are intentionally discarded: reconciliation only needs stable
event IDs, gateway classification, and aggregate financial totals.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    HumanitixEvent,
    HumanitixEventFinancialSummary,
)


DEFAULT_HUMANITIX_API_BASE_URL = "https://api.humanitix.com/v1"
MONEY_QUANTUM = Decimal("0.01")
STRIPE_GATEWAYS = {"stripe", "stripe-payments"}
OFFLINE_GATEWAYS = {"manual", "invoice", "cash"}
HUMANITIX_NATIVE_GATEWAYS = {
    "afterpay",
    "bpoint",
    "credit",
    "discover-nsw",
    "gift-card",
    "paypal",
    "till",
    "tillterminal",
    "worldpay",
    "zipmoney",
}


class HumanitixConfigurationError(RuntimeError):
    """Raised when a Humanitix connection cannot be used."""


class HumanitixAPIError(RuntimeError):
    """Raised when the Humanitix API rejects or fails a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[int] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _event_id(payload: dict[str, Any]) -> str:
    return str(payload.get("_id") or payload.get("id") or "").strip()


def _event_dates(payload: dict[str, Any]) -> tuple[Any, Any]:
    start_at = parse_datetime(str(payload.get("startDate") or "")) or None
    end_at = parse_datetime(str(payload.get("endDate") or "")) or None
    if start_at or end_at:
        return start_at, end_at
    for date_range in payload.get("dates") or []:
        if not isinstance(date_range, dict) or date_range.get("deleted"):
            continue
        start_at = parse_datetime(str(date_range.get("startDate") or "")) or None
        end_at = parse_datetime(str(date_range.get("endDate") or "")) or None
        if start_at or end_at:
            return start_at, end_at
    return None, None


def sanitise_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return event catalogue fields only; descriptions and people are omitted."""
    start_at, end_at = _event_dates(payload)
    location = payload.get("eventLocation") if isinstance(payload.get("eventLocation"), dict) else {}
    if not location and isinstance(payload.get("location"), dict):
        location = payload.get("location")
    return {
        "event_id": _event_id(payload),
        "name": str(payload.get("name") or "").strip(),
        "url": str(payload.get("url") or "").strip(),
        "currency": str(payload.get("currency") or "").upper().strip(),
        "timezone": str(payload.get("timezone") or "").strip(),
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
        "published": bool(payload.get("published")),
        "archived": bool(payload.get("isArchived") or payload.get("isPermanentlyArchived")),
        "total_capacity": payload.get("totalCapacity"),
        "venue": str(
            location.get("name")
            or location.get("venueName")
            or location.get("address")
            or ""
        ).strip()[:500],
        "updated_at": str(payload.get("updatedAt") or "").strip(),
    }


def classify_gateway(order: dict[str, Any]) -> str:
    gateway = str(order.get("paymentGateway") or "").strip().lower()
    if order.get("manualOrder") or gateway in OFFLINE_GATEWAYS:
        return "offline"
    if gateway in STRIPE_GATEWAYS:
        return "stripe"
    if gateway in HUMANITIX_NATIVE_GATEWAYS:
        return "humanitix_native"
    return "unknown"


def aggregate_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "order_count": 0,
        "paid_order_count": 0,
        "free_order_count": 0,
        "gross_sales": Decimal("0.00"),
        "net_sales": Decimal("0.00"),
        "refunds": Decimal("0.00"),
        "discounts": Decimal("0.00"),
        "donations": Decimal("0.00"),
        "humanitix_fees": Decimal("0.00"),
        "taxes": Decimal("0.00"),
    }
    gateways: dict[str, dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        financial_status = str(order.get("financialStatus") or "").strip()
        order_totals = order.get("totals") if isinstance(order.get("totals"), dict) else {}
        totals["order_count"] += 1
        if financial_status == "paid":
            totals["paid_order_count"] += 1
        if financial_status == "free":
            totals["free_order_count"] += 1
        gross_sales = _money(order_totals.get("grossSales"))
        net_sales = _money(order_totals.get("netSales"))
        refunds = abs(_money(order_totals.get("refunds")))
        totals["gross_sales"] += gross_sales
        totals["net_sales"] += net_sales
        totals["refunds"] += refunds
        totals["discounts"] += abs(_money(order_totals.get("discounts")))
        totals["donations"] += _money(
            order_totals.get("netClientDonation")
            if order_totals.get("netClientDonation") is not None
            else order_totals.get("clientDonation", order.get("clientDonation"))
        )
        totals["humanitix_fees"] += abs(_money(order_totals.get("humanitixFee")))
        totals["taxes"] += _money(order_totals.get("totalTaxes"))

        gateway = str(order.get("paymentGateway") or "unknown").strip().lower() or "unknown"
        bucket = gateways.setdefault(
            gateway,
            {
                "classification": classify_gateway(order),
                "orders": 0,
                "gross_sales": Decimal("0.00"),
                "net_sales": Decimal("0.00"),
                "refunds": Decimal("0.00"),
            },
        )
        bucket["orders"] += 1
        bucket["gross_sales"] += gross_sales
        bucket["net_sales"] += net_sales
        bucket["refunds"] += refunds

    totals["gateway_breakdown"] = {
        gateway: {
            **values,
            "gross_sales": str(values["gross_sales"].quantize(MONEY_QUANTUM)),
            "net_sales": str(values["net_sales"].quantize(MONEY_QUANTUM)),
            "refunds": str(values["refunds"].quantize(MONEY_QUANTUM)),
        }
        for gateway, values in sorted(gateways.items())
    }
    return totals


def aggregate_tickets(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    ticket_count = 0
    absorbed_fees = Decimal("0.00")
    breakdown: dict[str, dict[str, Any]] = {}
    for ticket in tickets:
        if not isinstance(ticket, dict) or str(ticket.get("status") or "") == "cancelled":
            continue
        ticket_count += 1
        fee = abs(_money(ticket.get("absorbedFee")))
        absorbed_fees += fee
        ticket_type_id = str(ticket.get("ticketTypeId") or "").strip()
        ticket_type_name = str(ticket.get("ticketTypeName") or ticket_type_id or "Unknown").strip()
        key = ticket_type_id or ticket_type_name
        bucket = breakdown.setdefault(
            key,
            {
                "ticket_type_id": ticket_type_id,
                "ticket_type_name": ticket_type_name,
                "count": 0,
                "net_price": Decimal("0.00"),
                "taxes": Decimal("0.00"),
                "absorbed_fees": Decimal("0.00"),
                "total": Decimal("0.00"),
            },
        )
        bucket["count"] += 1
        bucket["net_price"] += _money(ticket.get("netPrice"))
        bucket["taxes"] += _money(ticket.get("taxes"))
        bucket["absorbed_fees"] += fee
        bucket["total"] += _money(ticket.get("total"))
    return {
        "ticket_count": ticket_count,
        "absorbed_fees": absorbed_fees,
        "ticket_type_breakdown": {
            key: {
                **values,
                "net_price": str(values["net_price"].quantize(MONEY_QUANTUM)),
                "taxes": str(values["taxes"].quantize(MONEY_QUANTUM)),
                "absorbed_fees": str(values["absorbed_fees"].quantize(MONEY_QUANTUM)),
                "total": str(values["total"].quantize(MONEY_QUANTUM)),
            }
            for key, values in sorted(breakdown.items())
        },
    }


class HumanitixClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: Any = (3, 30),
    ):
        raw_key = api_key if api_key is not None else getattr(settings, "HUMANITIX_API_KEY", "")
        self.api_key = str(raw_key or "").strip()
        self.base_url = str(
            base_url
            or getattr(settings, "HUMANITIX_API_BASE_URL", DEFAULT_HUMANITIX_API_BASE_URL)
        ).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def list_events_page(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        since: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": max(int(page or 1), 1),
            "pageSize": min(max(int(page_size or 100), 1), 100),
        }
        if since:
            params["since"] = str(since)
        return self._get("/events", params=params)

    def iter_event_pages(
        self,
        *,
        page_size: int = 100,
        since: Optional[str] = None,
    ) -> Iterator[list[dict[str, Any]]]:
        page = 1
        while True:
            payload = self.list_events_page(page=page, page_size=page_size, since=since)
            rows = [item for item in payload.get("events") or [] if isinstance(item, dict)]
            yield rows
            total = int(payload.get("total") or len(rows))
            returned_page = int(payload.get("page") or page)
            returned_size = max(int(payload.get("pageSize") or page_size), 1)
            if returned_page * returned_size >= total or not rows:
                break
            page = returned_page + 1

    def list_orders(self, event_id: str, *, since: Optional[str] = None) -> list[dict[str, Any]]:
        return self._paginate(f"/events/{quote(event_id, safe='')}/orders", "orders", since=since)

    def list_tickets(self, event_id: str, *, since: Optional[str] = None) -> list[dict[str, Any]]:
        return self._paginate(f"/events/{quote(event_id, safe='')}/tickets", "tickets", since=since)

    def _paginate(
        self,
        path: str,
        collection_key: str,
        *,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": 100}
            if since:
                params["since"] = str(since)
            payload = self._get(path, params=params)
            page_rows = [
                item for item in payload.get(collection_key) or [] if isinstance(item, dict)
            ]
            rows.extend(page_rows)
            total = int(payload.get("total") or len(rows))
            returned_page = int(payload.get("page") or page)
            returned_size = max(int(payload.get("pageSize") or 100), 1)
            if returned_page * returned_size >= total or not page_rows:
                break
            page = returned_page + 1
        return rows

    def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise HumanitixConfigurationError("Humanitix API key is not configured.")
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                headers={
                    "Accept": "application/json",
                    "x-api-key": self.api_key,
                },
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise HumanitixAPIError("Unable to reach Humanitix.") from exc

        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            retry_after = response.headers.get("Retry-After") if hasattr(response, "headers") else None
            try:
                retry_after_seconds = int(retry_after) if retry_after else 60
            except (TypeError, ValueError):
                retry_after_seconds = 60
            raise HumanitixAPIError(
                "Humanitix rate limit reached; retry the sync shortly.",
                status_code=429,
                retry_after_seconds=retry_after_seconds,
            )
        if status_code in (401, 403):
            raise HumanitixAPIError(
                "Humanitix rejected the API key.",
                status_code=status_code,
            )
        if status_code is not None and status_code >= 400:
            raise HumanitixAPIError(
                f"Humanitix returned HTTP {status_code}.",
                status_code=status_code,
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise HumanitixAPIError("Humanitix returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise HumanitixAPIError("Humanitix returned an unexpected response.")
        return payload


def _safe_capacity(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def sync_humanitix_connection(
    connection: ExternalServiceConnection,
    *,
    full_backfill: bool = True,
    include_tickets: bool = True,
    max_events: Optional[int] = None,
    client: Optional[HumanitixClient] = None,
) -> dict[str, Any]:
    """Sync Humanitix events and PII-free financial aggregates.

    The API is read-only.  Each event is committed independently, so a rate
    limit or transient error does not discard already completed backfill work.
    """
    if connection.provider != ExternalServiceProvider.HUMANITIX:
        raise HumanitixConfigurationError("Connection is not a Humanitix connection.")
    if not connection.access_token:
        raise HumanitixConfigurationError("Humanitix connection is missing an API key.")
    if connection.organization_id is None:
        raise HumanitixConfigurationError("Humanitix connection is not linked to an organisation.")

    connection.status = ExternalServiceConnectionStatus.SYNCING
    connection.last_error = ""
    connection.save(update_fields=["status", "last_error", "updated_at"])

    client = client or HumanitixClient(api_key=connection.access_token)
    since = None
    if not full_backfill and connection.last_synced_at:
        since = connection.last_synced_at.isoformat()
    synced_at = timezone.now()
    events_synced = 0
    orders_synced = 0
    tickets_synced = 0

    try:
        stop = False
        for event_page in client.iter_event_pages(since=since):
            for event_payload in event_page:
                if max_events is not None and events_synced >= max(int(max_events), 0):
                    stop = True
                    break
                external_event_id = _event_id(event_payload)
                if not external_event_id:
                    continue
                # Even for an incremental event catalogue fetch, rebuild the
                # selected event's aggregate from its complete order/ticket
                # history. Applying only a delta would overwrite the stored
                # lifetime totals with the latest slice.
                orders = client.list_orders(external_event_id)
                tickets = client.list_tickets(external_event_id) if include_tickets else []
                order_summary = aggregate_orders(orders)
                ticket_summary = aggregate_tickets(tickets)
                safe_event = sanitise_event_payload(event_payload)
                summary_payload = {
                    "orders": {
                        key: (
                            str(value.quantize(MONEY_QUANTUM))
                            if isinstance(value, Decimal)
                            else value
                        )
                        for key, value in order_summary.items()
                    },
                    "tickets": {
                        key: (
                            str(value.quantize(MONEY_QUANTUM))
                            if isinstance(value, Decimal)
                            else value
                        )
                        for key, value in ticket_summary.items()
                    },
                }
                start_at, end_at = _event_dates(event_payload)
                with transaction.atomic():
                    event, _created = HumanitixEvent.objects.update_or_create(
                        organization=connection.organization,
                        external_event_id=external_event_id,
                        defaults={
                            "connection": connection,
                            "event_name": safe_event["name"] or external_event_id,
                            "event_url": safe_event["url"][:1000],
                            "currency": safe_event["currency"][:12],
                            "timezone_name": safe_event["timezone"][:100],
                            "start_at": start_at,
                            "end_at": end_at,
                            "total_capacity": _safe_capacity(safe_event["total_capacity"]),
                            "published": safe_event["published"],
                            "archived": safe_event["archived"],
                            "source_hash": _stable_hash(safe_event),
                            "source_payload": safe_event,
                            "last_synced_at": synced_at,
                        },
                    )
                    HumanitixEventFinancialSummary.objects.update_or_create(
                        event=event,
                        defaults={
                            "order_count": order_summary["order_count"],
                            "paid_order_count": order_summary["paid_order_count"],
                            "free_order_count": order_summary["free_order_count"],
                            "ticket_count": ticket_summary["ticket_count"],
                            "gross_sales": order_summary["gross_sales"],
                            "net_sales": order_summary["net_sales"],
                            "refunds": order_summary["refunds"],
                            "discounts": order_summary["discounts"],
                            "donations": order_summary["donations"],
                            "humanitix_fees": order_summary["humanitix_fees"],
                            "absorbed_fees": ticket_summary["absorbed_fees"],
                            "taxes": order_summary["taxes"],
                            "gateway_breakdown": order_summary["gateway_breakdown"],
                            "ticket_type_breakdown": ticket_summary["ticket_type_breakdown"],
                            "source_hash": _stable_hash(summary_payload),
                            "last_synced_at": synced_at,
                        },
                    )
                events_synced += 1
                orders_synced += len(orders)
                tickets_synced += len(tickets)
                connection.sync_cursor = {
                    **dict(connection.sync_cursor or {}),
                    "humanitix_last_event_id": external_event_id,
                    "humanitix_events_synced": events_synced,
                    "humanitix_orders_synced": orders_synced,
                    "humanitix_tickets_synced": tickets_synced,
                    "humanitix_full_backfill": full_backfill,
                }
                connection.save(update_fields=["sync_cursor", "updated_at"])
            if stop:
                break
    except Exception as exc:
        connection.status = ExternalServiceConnectionStatus.ERROR
        connection.last_error = str(exc)[:2000]
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise

    connection.status = ExternalServiceConnectionStatus.CONNECTED
    connection.last_error = ""
    connection.last_synced_at = synced_at
    connection.sync_cursor = {
        **dict(connection.sync_cursor or {}),
        "last_synced_at": synced_at.isoformat(),
        "humanitix_events_synced": events_synced,
        "humanitix_orders_synced": orders_synced,
        "humanitix_tickets_synced": tickets_synced,
        "humanitix_full_backfill": full_backfill,
        "humanitix_complete": not stop,
    }
    connection.save(
        update_fields=["status", "last_error", "last_synced_at", "sync_cursor", "updated_at"]
    )
    return {
        "connectionId": connection.id,
        "connection_id": connection.id,
        "provider": connection.provider,
        "status": "synced",
        "eventsSynced": events_synced,
        "events_synced": events_synced,
        "ordersSynced": orders_synced,
        "orders_synced": orders_synced,
        "ticketsSynced": tickets_synced,
        "tickets_synced": tickets_synced,
        "fullBackfill": full_backfill,
        "full_backfill": full_backfill,
        "complete": not stop,
        "lastSyncedAt": synced_at.isoformat(),
        "last_synced_at": synced_at.isoformat(),
    }
