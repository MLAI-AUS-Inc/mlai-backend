from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    Organization,
)
from integrations import http_client as requests
from integrations.models import (
    ExternalFinancialRecord,
    FinancialAccount,
    FinancialConnection,
    FinancialConnectionStatus,
    FinancialProvider,
    FinancialRecordType,
    MonthlyRevenueSnapshot,
    StartupMetricObservation,
)
from integrations.services.valley_harness import notify_valley_run_created

logger = logging.getLogger(__name__)

FINANCIAL_MONTHLY_METRICS_WORKFLOW = "financial_monthly_metrics"
FINANCIAL_STEP_ORDER = [
    "profile_resolution",
    "financial_backfill",
    "financial_incremental_sync",
    "revenue_calculation",
    "timeline_publish",
]

STRIPE_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
STRIPE_TOKEN_URL = "https://connect.stripe.com/oauth/token"
STRIPE_API_BASE_URL = "https://api.stripe.com"
XERO_AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_ACCOUNTING_BASE_URL = "https://api.xero.com/api.xro/2.0"

STRIPE_PHASES = [
    (FinancialRecordType.SUBSCRIPTION, "/v1/subscriptions", "data"),
    (FinancialRecordType.INVOICE, "/v1/invoices", "data"),
]
XERO_PHASES = [
    (FinancialRecordType.REPEATING_INVOICE, "/RepeatingInvoices", "RepeatingInvoices"),
    (FinancialRecordType.INVOICE, "/Invoices", "Invoices"),
    (FinancialRecordType.PAYMENT, "/Payments", "Payments"),
    (FinancialRecordType.BANK_TRANSACTION, "/BankTransactions", "BankTransactions"),
]
OPEN_RUN_STATUSES = {
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
    ContentFactoryRunStatus.BLOCKED,
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.AWAITING_APPROVAL,
    ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
    ContentFactoryRunStatus.APPROVAL_REQUIRED,
}


def configured_stripe_api_version() -> str:
    return str(getattr(settings, "STRIPE_API_VERSION", "2026-02-25.clover") or "2026-02-25.clover")


def configured_xero_scopes() -> list[str]:
    raw = getattr(settings, "XERO_OAUTH_SCOPES", None)
    if raw:
        if isinstance(raw, str):
            return [item for item in raw.split() if item]
        return [str(item) for item in raw if str(item).strip()]
    return [
        "openid",
        "profile",
        "email",
        "offline_access",
        "accounting.transactions.read",
        "accounting.settings.read",
    ]


def build_stripe_oauth_url(*, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": getattr(settings, "STRIPE_CONNECT_CLIENT_ID", ""),
        "scope": "read_only",
        "redirect_uri": getattr(settings, "STRIPE_OAUTH_REDIRECT_URI", ""),
        "state": state,
    }
    return f"{STRIPE_AUTHORIZE_URL}?{urlencode(params)}"


def build_xero_oauth_url(*, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": getattr(settings, "XERO_CLIENT_ID", ""),
        "redirect_uri": getattr(settings, "XERO_OAUTH_REDIRECT_URI", ""),
        "scope": " ".join(configured_xero_scopes()),
        "state": state,
    }
    return f"{XERO_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_stripe_oauth_code(code: str) -> dict:
    response = requests.post(
        STRIPE_TOKEN_URL,
        data={
            "client_secret": getattr(settings, "STRIPE_SECRET_KEY", ""),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    return response.json()


def _xero_basic_auth_header() -> str:
    raw = f"{getattr(settings, 'XERO_CLIENT_ID', '')}:{getattr(settings, 'XERO_CLIENT_SECRET', '')}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def exchange_xero_oauth_code(code: str) -> dict:
    response = requests.post(
        XERO_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": getattr(settings, "XERO_OAUTH_REDIRECT_URI", ""),
        },
        headers={
            "Authorization": _xero_basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=(3, 20),
    )
    response.raise_for_status()
    return response.json()


def refresh_xero_connection_token(connection: FinancialConnection) -> FinancialConnection:
    if connection.provider != FinancialProvider.XERO:
        return connection
    if connection.expires_at and connection.expires_at > timezone.now() + timedelta(minutes=5):
        return connection
    if not connection.refresh_token:
        connection.status = FinancialConnectionStatus.ACTION_REQUIRED
        connection.last_error = "Missing Xero refresh token."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        return connection

    try:
        response = requests.post(
            XERO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
            },
            headers={
                "Authorization": _xero_basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=(3, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        logger.exception("Failed to refresh Xero token for financial connection %s", connection.id)
        connection.status = FinancialConnectionStatus.ACTION_REQUIRED
        connection.last_error = "Failed to refresh Xero token."
        connection.save(update_fields=["status", "last_error", "updated_at"])
        return connection

    _apply_token_payload(connection, payload)
    connection.status = FinancialConnectionStatus.CONNECTED
    connection.last_error = ""
    connection.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "token_type",
            "expires_at",
            "scopes",
            "status",
            "last_error",
            "updated_at",
        ]
    )
    return connection


def fetch_xero_connections(access_token: str) -> list[dict]:
    response = requests.get(
        XERO_CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=(3, 20),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def upsert_stripe_connection(*, organization: Organization, user, token_payload: dict) -> FinancialConnection:
    stripe_account_id = str(token_payload.get("stripe_user_id") or "").strip()
    if not stripe_account_id:
        raise ValueError("Stripe OAuth response did not include stripe_user_id.")

    scope = str(token_payload.get("scope") or "read_only")
    connection, _ = FinancialConnection.objects.update_or_create(
        organization=organization,
        provider=FinancialProvider.STRIPE,
        external_account_id=stripe_account_id,
        defaults={
            "user": user,
            "display_name": str(token_payload.get("stripe_user_id") or stripe_account_id),
            "access_token": token_payload.get("access_token") or "",
            "refresh_token": token_payload.get("refresh_token") or "",
            "token_type": token_payload.get("token_type") or "bearer",
            "scopes": [scope] if scope else [],
            "status": FinancialConnectionStatus.CONNECTED,
            "last_error": "",
            "metadata": {
                "livemode": bool(token_payload.get("livemode")),
                "stripe_publishable_key": token_payload.get("stripe_publishable_key") or "",
            },
        },
    )
    FinancialAccount.objects.update_or_create(
        organization=organization,
        connection=connection,
        external_account_id=stripe_account_id,
        defaults={
            "provider": FinancialProvider.STRIPE,
            "display_name": connection.display_name or stripe_account_id,
            "account_type": "stripe_account",
            "selected_for_revenue": True,
            "metadata": connection.metadata or {},
        },
    )
    return connection


def upsert_xero_connections(*, organization: Organization, user, token_payload: dict, tenants: list[dict]) -> list[FinancialConnection]:
    connections: list[FinancialConnection] = []
    for tenant in tenants:
        tenant_id = str(tenant.get("tenantId") or "").strip()
        if not tenant_id:
            continue

        connection, _ = FinancialConnection.objects.update_or_create(
            organization=organization,
            provider=FinancialProvider.XERO,
            external_account_id=tenant_id,
            defaults={
                "user": user,
                "display_name": str(tenant.get("tenantName") or tenant_id),
                "status": FinancialConnectionStatus.CONNECTED,
                "last_error": "",
                "metadata": {
                    "connection_id": tenant.get("id"),
                    "tenant_type": tenant.get("tenantType"),
                    "created_date_utc": tenant.get("createdDateUtc"),
                    "updated_date_utc": tenant.get("updatedDateUtc"),
                },
            },
        )
        _apply_token_payload(connection, token_payload)
        connection.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "token_type",
                "expires_at",
                "scopes",
                "updated_at",
            ]
        )
        FinancialAccount.objects.update_or_create(
            organization=organization,
            connection=connection,
            external_account_id=tenant_id,
            defaults={
                "provider": FinancialProvider.XERO,
                "display_name": connection.display_name or tenant_id,
                "account_type": "xero_tenant",
                "selected_for_revenue": True,
                "metadata": connection.metadata or {},
            },
        )
        connections.append(connection)
    return connections


def _apply_token_payload(connection: FinancialConnection, payload: dict) -> None:
    if payload.get("access_token"):
        connection.access_token = payload["access_token"]
    if payload.get("refresh_token"):
        connection.refresh_token = payload["refresh_token"]
    if payload.get("token_type"):
        connection.token_type = payload["token_type"]
    if payload.get("scope"):
        connection.scopes = [item for item in str(payload["scope"]).split() if item]
    expires_in = payload.get("expires_in")
    if expires_in not in (None, ""):
        try:
            connection.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            connection.expires_at = None


def serialize_financial_connection(connection: FinancialConnection) -> dict:
    return {
        "id": connection.id,
        "provider": connection.provider,
        "external_account_id": connection.external_account_id,
        "display_name": connection.display_name,
        "status": connection.status,
        "scopes": connection.scopes or [],
        "last_synced_at": connection.last_synced_at.isoformat() if connection.last_synced_at else None,
        "last_error": connection.last_error or "",
        "accounts": [
            {
                "id": account.id,
                "external_account_id": account.external_account_id,
                "display_name": account.display_name,
                "currency": account.currency,
                "account_type": account.account_type,
                "selected_for_revenue": account.selected_for_revenue,
            }
            for account in connection.accounts.order_by("provider", "display_name", "external_account_id")
        ],
    }


def serialize_revenue_snapshot(snapshot: MonthlyRevenueSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "month": snapshot.month.isoformat(),
        "currency": snapshot.currency,
        "mrr_amount": str(snapshot.mrr_amount),
        "mrr_growth_rate": str(snapshot.mrr_growth_rate) if snapshot.mrr_growth_rate is not None else None,
        "mrr_delta": str(snapshot.mrr_delta),
        "cash_collected_amount": str(snapshot.cash_collected_amount),
        "recognized_revenue_amount": (
            str(snapshot.recognized_revenue_amount) if snapshot.recognized_revenue_amount is not None else None
        ),
        "confidence": snapshot.confidence,
        "source_mix": snapshot.source_mix or {},
        "warnings": snapshot.warnings or [],
        "calculated_at": snapshot.calculated_at.isoformat() if snapshot.calculated_at else None,
    }


def get_financial_status(*, organization: Organization) -> dict:
    connections = list(
        organization.financial_connections.prefetch_related("accounts").order_by("provider", "-updated_at")
    )
    snapshots = list(organization.monthly_revenue_snapshots.order_by("-month", "currency")[:12])
    return {
        "domain": organization.domain,
        "connections": [serialize_financial_connection(connection) for connection in connections],
        "snapshots": [serialize_revenue_snapshot(snapshot) for snapshot in snapshots],
        "basiq_planned": True,
        "headline_metric": "mrr",
    }


def create_financial_sync_run(
    *,
    organization: Organization,
    user,
    connection_ids: Optional[Iterable[int]] = None,
    trigger: str = "manual",
) -> tuple[ContentFactoryRun, bool]:
    selected_connections = FinancialConnection.objects.filter(
        organization=organization,
        status=FinancialConnectionStatus.CONNECTED,
        provider__in=[FinancialProvider.STRIPE, FinancialProvider.XERO],
    )
    if connection_ids:
        selected_connections = selected_connections.filter(id__in=list(connection_ids))
    connection_id_list = list(selected_connections.order_by("provider", "id").values_list("id", flat=True))
    if not connection_id_list:
        raise ValueError("No connected Stripe or Xero financial connections are available.")

    existing = (
        ContentFactoryRun.objects.filter(
            workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
            domain=organization.domain,
            status__in=list(OPEN_RUN_STATUSES),
        )
        .order_by("-updated_at")
        .first()
    )
    if existing is not None:
        run_request = dict(existing.run_request or {})
        merged_connection_ids = sorted(set(list(run_request.get("connection_ids") or []) + connection_id_list))
        if merged_connection_ids != list(run_request.get("connection_ids") or []):
            run_request["connection_ids"] = merged_connection_ids
            existing.run_request = run_request
            existing.save(update_fields=["run_request", "updated_at"])
        for connection in selected_connections:
            cursor = dict(connection.sync_cursor or {})
            if cursor.get("active_run_id") != existing.run_id or _cursor_complete(connection):
                prepare_connection_for_financial_run(connection, run=existing)
        return existing, False

    run = ContentFactoryRun.objects.create(
        run_id=f"financial-metrics-{uuid.uuid4()}",
        workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
        domain=organization.domain,
        slack_user_id=str(getattr(user, "id", "") or ""),
        status=ContentFactoryRunStatus.QUEUED,
        current_step=FINANCIAL_STEP_ORDER[0],
        approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
        step_order=FINANCIAL_STEP_ORDER,
        run_request={
            "organization_id": organization.id,
            "user_id": getattr(user, "id", None),
            "connection_ids": connection_id_list,
            "trigger": trigger,
            "headline_metric": "mrr",
            "bank_feed_adapter": "basiq_planned",
        },
        result={},
        acceptance_summary={},
        verification_summary={},
    )
    for connection in selected_connections:
        prepare_connection_for_financial_run(connection, run=run)
    return run, True


def enqueue_financial_sync_run(*, organization: Organization, user, trigger: str = "oauth") -> Optional[ContentFactoryRun]:
    try:
        run, created = create_financial_sync_run(organization=organization, user=user, trigger=trigger)
    except ValueError:
        return None
    if created:
        transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
    return run


def prepare_connection_for_financial_run(connection: FinancialConnection, *, run: ContentFactoryRun) -> None:
    cursor = dict(connection.sync_cursor or {})
    cursor["active_run_id"] = run.run_id
    cursor["provider"] = connection.provider
    cursor["phase_index"] = 0
    cursor["phases"] = {
        phase.value if hasattr(phase, "value") else str(phase): {"done": False}
        for phase, _path, _payload_key in _phases_for_provider(connection.provider)
    }
    cursor["started_at"] = timezone.now().isoformat()
    cursor.pop("completed_at", None)
    connection.sync_cursor = cursor
    connection.last_error = ""
    connection.save(update_fields=["sync_cursor", "last_error", "updated_at"])


def get_financial_run(run_id: str) -> ContentFactoryRun:
    return ContentFactoryRun.objects.get(
        run_id=run_id,
        workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
    )


def sync_next_financial_page(*, run: ContentFactoryRun) -> dict:
    organization = Organization.objects.get(id=(run.run_request or {}).get("organization_id"))
    connection_ids = list((run.run_request or {}).get("connection_ids") or [])
    connections = list(
        FinancialConnection.objects.filter(
            organization=organization,
            id__in=connection_ids,
            status=FinancialConnectionStatus.CONNECTED,
        ).order_by("provider", "id")
    )
    if not connections:
        return {"run_id": run.run_id, "synced_count": 0, "has_more": False, "detail": "No active connections."}

    for connection in connections:
        cursor = dict(connection.sync_cursor or {})
        if cursor.get("active_run_id") != run.run_id:
            prepare_connection_for_financial_run(connection, run=run)
            cursor = dict(connection.sync_cursor or {})
        if _cursor_complete(connection):
            continue
        result = _sync_connection_phase_page(connection)
        result["has_more"] = _any_sync_remaining(connections)
        return result

    return {"run_id": run.run_id, "synced_count": 0, "has_more": False, "detail": "Financial sync complete."}


def calculate_and_publish_monthly_revenue(*, run: ContentFactoryRun) -> dict:
    organization = Organization.objects.get(id=(run.run_request or {}).get("organization_id"))
    snapshots = calculate_monthly_revenue_snapshots(organization=organization, run=run)
    published_count = publish_revenue_snapshots_to_startup_metrics(organization=organization, run=run, snapshots=snapshots)
    return {
        "run_id": run.run_id,
        "snapshot_count": len(snapshots),
        "published_metric_count": published_count,
        "snapshots": [serialize_revenue_snapshot(snapshot) for snapshot in snapshots],
    }


def _phases_for_provider(provider: str):
    if provider == FinancialProvider.STRIPE:
        return STRIPE_PHASES
    if provider == FinancialProvider.XERO:
        return XERO_PHASES
    return []


def _cursor_complete(connection: FinancialConnection) -> bool:
    cursor = dict(connection.sync_cursor or {})
    phases = cursor.get("phases") or {}
    provider_phases = _phases_for_provider(connection.provider)
    if not provider_phases:
        return True
    return all((phases.get(phase.value) or {}).get("done") for phase, _path, _key in provider_phases)


def _any_sync_remaining(connections: Iterable[FinancialConnection]) -> bool:
    for connection in connections:
        connection.refresh_from_db(fields=["sync_cursor"])
        if not _cursor_complete(connection):
            return True
    return False


def _sync_connection_phase_page(connection: FinancialConnection) -> dict:
    if connection.provider == FinancialProvider.STRIPE:
        return _sync_stripe_phase_page(connection)
    if connection.provider == FinancialProvider.XERO:
        return _sync_xero_phase_page(connection)
    return {
        "connection_id": connection.id,
        "provider": connection.provider,
        "synced_count": 0,
        "has_more": False,
        "detail": "Provider is not implemented for v1 sync.",
    }


def _current_phase(connection: FinancialConnection):
    cursor = dict(connection.sync_cursor or {})
    phases = _phases_for_provider(connection.provider)
    phase_index = int(cursor.get("phase_index") or 0)
    if phase_index >= len(phases):
        return None
    return phases[phase_index]


def _advance_phase(connection: FinancialConnection, phase: FinancialRecordType) -> None:
    cursor = dict(connection.sync_cursor or {})
    phases_state = dict(cursor.get("phases") or {})
    phase_state = dict(phases_state.get(phase.value) or {})
    phase_state["done"] = True
    phase_state["completed_at"] = timezone.now().isoformat()
    phases_state[phase.value] = phase_state
    cursor["phases"] = phases_state
    cursor["phase_index"] = int(cursor.get("phase_index") or 0) + 1
    if cursor["phase_index"] >= len(_phases_for_provider(connection.provider)):
        cursor["completed_at"] = timezone.now().isoformat()
        connection.last_synced_at = timezone.now()
        update_fields = ["sync_cursor", "last_synced_at", "updated_at"]
    else:
        update_fields = ["sync_cursor", "updated_at"]
    connection.sync_cursor = cursor
    connection.save(update_fields=update_fields)


def _update_phase_state(connection: FinancialConnection, phase: FinancialRecordType, state_patch: dict) -> None:
    cursor = dict(connection.sync_cursor or {})
    phases_state = dict(cursor.get("phases") or {})
    phase_state = dict(phases_state.get(phase.value) or {})
    phase_state.update(state_patch)
    phases_state[phase.value] = phase_state
    cursor["phases"] = phases_state
    connection.sync_cursor = cursor
    connection.save(update_fields=["sync_cursor", "updated_at"])


def _sync_stripe_phase_page(connection: FinancialConnection) -> dict:
    phase_tuple = _current_phase(connection)
    if phase_tuple is None:
        return {"connection_id": connection.id, "provider": connection.provider, "synced_count": 0, "has_more": False}
    phase, path, payload_key = phase_tuple
    phase_state = ((connection.sync_cursor or {}).get("phases") or {}).get(phase.value) or {}

    headers = {
        "Authorization": f"Bearer {_stripe_bearer_token(connection)}",
        "Stripe-Version": configured_stripe_api_version(),
    }
    if not connection.access_token and getattr(settings, "STRIPE_SECRET_KEY", ""):
        headers["Stripe-Account"] = connection.external_account_id

    params = {"limit": 100}
    if phase == FinancialRecordType.SUBSCRIPTION:
        params["status"] = "all"
    if phase == FinancialRecordType.INVOICE:
        params["status"] = "paid"
    if phase_state.get("starting_after"):
        params["starting_after"] = phase_state["starting_after"]

    response = requests.get(
        f"{STRIPE_API_BASE_URL}{path}",
        headers=headers,
        params=params,
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    items = list(payload.get(payload_key) or [])
    saved = [_upsert_stripe_record(connection=connection, object_type=phase, raw=item) for item in items]

    if payload.get("has_more") and items:
        _update_phase_state(connection, phase, {"starting_after": items[-1].get("id"), "last_count": len(items)})
        has_more_for_connection = True
    else:
        _advance_phase(connection, phase)
        has_more_for_connection = not _cursor_complete(connection)

    return {
        "connection_id": connection.id,
        "provider": connection.provider,
        "object_type": phase.value,
        "synced_count": len(saved),
        "record_ids": [record.id for record in saved],
        "has_more": has_more_for_connection,
    }


def _stripe_bearer_token(connection: FinancialConnection) -> str:
    token = str(connection.access_token or "").strip()
    if token:
        return token
    return str(getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()


def _sync_xero_phase_page(connection: FinancialConnection) -> dict:
    refresh_xero_connection_token(connection)
    connection.refresh_from_db()
    if connection.status != FinancialConnectionStatus.CONNECTED:
        return {
            "connection_id": connection.id,
            "provider": connection.provider,
            "synced_count": 0,
            "has_more": False,
            "error": connection.last_error or "Xero connection requires reauthorization.",
        }

    phase_tuple = _current_phase(connection)
    if phase_tuple is None:
        return {"connection_id": connection.id, "provider": connection.provider, "synced_count": 0, "has_more": False}
    phase, path, payload_key = phase_tuple
    phase_state = ((connection.sync_cursor or {}).get("phases") or {}).get(phase.value) or {}
    page = int(phase_state.get("page") or 1)
    page_size = 100
    headers = {
        "Authorization": f"Bearer {connection.access_token}",
        "Accept": "application/json",
        "Xero-Tenant-Id": connection.external_account_id,
    }
    if connection.last_synced_at:
        headers["If-Modified-Since"] = connection.last_synced_at.strftime("%a, %d %b %Y %H:%M:%S GMT")

    response = requests.get(
        f"{XERO_ACCOUNTING_BASE_URL}{path}",
        headers=headers,
        params={"page": page, "pageSize": page_size},
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    items = list(payload.get(payload_key) or [])
    saved = [_upsert_xero_record(connection=connection, object_type=phase, raw=item) for item in items]

    if len(items) >= page_size:
        _update_phase_state(connection, phase, {"page": page + 1, "last_count": len(items)})
        has_more_for_connection = True
    else:
        _advance_phase(connection, phase)
        has_more_for_connection = not _cursor_complete(connection)

    return {
        "connection_id": connection.id,
        "provider": connection.provider,
        "object_type": phase.value,
        "synced_count": len(saved),
        "record_ids": [record.id for record in saved],
        "has_more": has_more_for_connection,
    }


def _upsert_stripe_record(
    *,
    connection: FinancialConnection,
    object_type: FinancialRecordType,
    raw: dict,
) -> ExternalFinancialRecord:
    external_id = str(raw.get("id") or "").strip()
    if not external_id:
        raise ValueError("Stripe record is missing id.")
    defaults = _stripe_record_defaults(object_type=object_type, raw=raw)
    return _upsert_external_record(
        connection=connection,
        object_type=object_type,
        external_id=external_id,
        raw=raw,
        defaults=defaults,
    )


def _stripe_record_defaults(*, object_type: FinancialRecordType, raw: dict) -> dict:
    if object_type == FinancialRecordType.SUBSCRIPTION:
        mrr_amount, currency = _stripe_subscription_mrr(raw)
        return {
            "source_status": str(raw.get("status") or ""),
            "customer_ref": str(raw.get("customer") or ""),
            "period_start": _date_from_unix(raw.get("current_period_start")),
            "period_end": _date_from_unix(raw.get("current_period_end")),
            "amount": mrr_amount,
            "currency": currency,
            "occurred_at": _datetime_from_unix(raw.get("created")),
            "updated_at_source": _datetime_from_unix(raw.get("current_period_start")),
        }
    if object_type == FinancialRecordType.INVOICE:
        paid_at = (raw.get("status_transitions") or {}).get("paid_at")
        return {
            "source_status": str(raw.get("status") or ""),
            "customer_ref": str(raw.get("customer") or ""),
            "period_start": _date_from_unix(raw.get("created")),
            "period_end": _date_from_unix(raw.get("created")),
            "amount": _minor_units_to_decimal(raw.get("amount_paid")),
            "currency": str(raw.get("currency") or "").upper(),
            "occurred_at": _datetime_from_unix(paid_at or raw.get("created")),
            "updated_at_source": _datetime_from_unix(raw.get("status_transitions", {}).get("finalized_at") or raw.get("created")),
        }
    return {}


def _upsert_xero_record(
    *,
    connection: FinancialConnection,
    object_type: FinancialRecordType,
    raw: dict,
) -> ExternalFinancialRecord:
    external_id = (
        raw.get("RepeatingInvoiceID")
        or raw.get("InvoiceID")
        or raw.get("PaymentID")
        or raw.get("BankTransactionID")
        or raw.get("AccountID")
        or raw.get("ID")
    )
    external_id = str(external_id or "").strip()
    if not external_id:
        raise ValueError("Xero record is missing an external id.")
    defaults = _xero_record_defaults(object_type=object_type, raw=raw)
    return _upsert_external_record(
        connection=connection,
        object_type=object_type,
        external_id=external_id,
        raw=raw,
        defaults=defaults,
    )


def _xero_record_defaults(*, object_type: FinancialRecordType, raw: dict) -> dict:
    if object_type == FinancialRecordType.REPEATING_INVOICE:
        return {
            "source_status": str(raw.get("Status") or ""),
            "customer_ref": str((raw.get("Contact") or {}).get("ContactID") or (raw.get("Contact") or {}).get("Name") or ""),
            "period_start": _parse_date_value(raw.get("StartDate") or raw.get("Date")),
            "period_end": _parse_date_value(raw.get("EndDate")),
            "amount": _normalize_xero_repeating_invoice_mrr(raw),
            "currency": str(raw.get("CurrencyCode") or "").upper(),
            "occurred_at": _parse_datetime_value(raw.get("UpdatedDateUTC") or raw.get("Date")),
            "updated_at_source": _parse_datetime_value(raw.get("UpdatedDateUTC")),
        }
    if object_type == FinancialRecordType.INVOICE:
        return {
            "source_status": str(raw.get("Status") or ""),
            "customer_ref": str((raw.get("Contact") or {}).get("ContactID") or (raw.get("Contact") or {}).get("Name") or ""),
            "period_start": _parse_date_value(raw.get("Date")),
            "period_end": _parse_date_value(raw.get("FullyPaidOnDate") or raw.get("DueDate") or raw.get("Date")),
            "amount": _to_decimal(raw.get("Total")),
            "currency": str(raw.get("CurrencyCode") or "").upper(),
            "occurred_at": _parse_datetime_value(raw.get("FullyPaidOnDate") or raw.get("Date")),
            "updated_at_source": _parse_datetime_value(raw.get("UpdatedDateUTC")),
        }
    if object_type == FinancialRecordType.PAYMENT:
        return {
            "source_status": str(raw.get("Status") or ""),
            "customer_ref": str((raw.get("Invoice") or {}).get("Contact", {}).get("ContactID") or ""),
            "period_start": _parse_date_value(raw.get("Date")),
            "period_end": _parse_date_value(raw.get("Date")),
            "amount": _to_decimal(raw.get("Amount")),
            "currency": str(raw.get("CurrencyCode") or ""),
            "occurred_at": _parse_datetime_value(raw.get("Date")),
            "updated_at_source": _parse_datetime_value(raw.get("UpdatedDateUTC")),
        }
    if object_type == FinancialRecordType.BANK_TRANSACTION:
        return {
            "source_status": str(raw.get("Status") or ""),
            "customer_ref": str((raw.get("Contact") or {}).get("ContactID") or (raw.get("Contact") or {}).get("Name") or ""),
            "period_start": _parse_date_value(raw.get("Date")),
            "period_end": _parse_date_value(raw.get("Date")),
            "amount": _to_decimal(raw.get("Total")),
            "currency": str(raw.get("CurrencyCode") or "").upper(),
            "occurred_at": _parse_datetime_value(raw.get("Date")),
            "updated_at_source": _parse_datetime_value(raw.get("UpdatedDateUTC")),
        }
    return {}


def _upsert_external_record(
    *,
    connection: FinancialConnection,
    object_type: FinancialRecordType,
    external_id: str,
    raw: dict,
    defaults: dict,
) -> ExternalFinancialRecord:
    defaults = dict(defaults or {})
    defaults.setdefault("provider", connection.provider)
    defaults["organization"] = connection.organization
    defaults["raw_payload"] = raw or {}
    defaults["raw_hash"] = _hash_payload(raw or {})
    record, _ = ExternalFinancialRecord.objects.update_or_create(
        connection=connection,
        object_type=object_type,
        external_id=external_id,
        defaults=defaults,
    )
    return record


def calculate_monthly_revenue_snapshots(*, organization: Organization, run: Optional[ContentFactoryRun] = None) -> list[MonthlyRevenueSnapshot]:
    records = list(
        organization.external_financial_records.select_related("connection").order_by("period_start", "occurred_at", "id")
    )
    if not records:
        return []

    month_keys = set(_recent_month_starts(6))
    for record in records:
        record_month = _record_month(record)
        if record_month:
            month_keys.add(record_month)

    buckets: dict[tuple[date, str], dict] = defaultdict(
        lambda: {
            "mrr": Decimal("0"),
            "cash": Decimal("0"),
            "recognized": Decimal("0"),
            "confidence": 0.0,
            "warnings": [],
            "source_mix": defaultdict(int),
            "source_record_ids": [],
        }
    )
    xero_invoice_candidates: list[ExternalFinancialRecord] = []

    for record in records:
        month = _record_month(record)
        currency = str(record.currency or "").upper()
        if not month or not currency:
            continue
        month_keys.add(month)
        bucket = buckets[(month, currency)]

        if record.provider == FinancialProvider.STRIPE and record.object_type == FinancialRecordType.SUBSCRIPTION:
            if str(record.source_status or "").lower() in {"active", "past_due"} and (record.amount or 0) > 0:
                bucket["mrr"] += record.amount or Decimal("0")
                bucket["confidence"] = max(bucket["confidence"], 0.95)
                bucket["source_mix"]["stripe_subscriptions"] += 1
                bucket["source_record_ids"].append(record.id)
            continue

        if record.provider == FinancialProvider.STRIPE and record.object_type == FinancialRecordType.INVOICE:
            if str(record.source_status or "").lower() == "paid":
                bucket["cash"] += record.amount or Decimal("0")
                bucket["source_mix"]["stripe_paid_invoices"] += 1
                bucket["source_record_ids"].append(record.id)
            continue

        if record.provider == FinancialProvider.XERO and record.object_type == FinancialRecordType.REPEATING_INVOICE:
            if _is_revenue_xero_record(record) and not _record_looks_stripe_origin(record):
                bucket["mrr"] += record.amount or Decimal("0")
                bucket["confidence"] = max(bucket["confidence"], 0.85)
                bucket["source_mix"]["xero_repeating_invoices"] += 1
                bucket["source_record_ids"].append(record.id)
            continue

        if record.provider == FinancialProvider.XERO and record.object_type == FinancialRecordType.INVOICE:
            if _is_revenue_xero_record(record) and not _record_looks_stripe_origin(record):
                xero_invoice_candidates.append(record)
            continue

        if record.provider == FinancialProvider.XERO and record.object_type in {
            FinancialRecordType.PAYMENT,
            FinancialRecordType.BANK_TRANSACTION,
        }:
            if _is_cash_inflow_record(record):
                bucket["cash"] += record.amount or Decimal("0")
                bucket["source_mix"][f"xero_{record.object_type}s"] += 1
                bucket["source_record_ids"].append(record.id)

    for record in xero_invoice_candidates:
        month = _record_month(record)
        currency = str(record.currency or "").upper()
        if not month or not currency:
            continue
        bucket = buckets[(month, currency)]
        if bucket["mrr"] > 0:
            bucket["warnings"].append("xero_sales_invoice_excluded_because_stronger_recurring_source_exists")
            continue
        bucket["mrr"] += record.amount or Decimal("0")
        bucket["confidence"] = max(bucket["confidence"], 0.55)
        bucket["warnings"].append("mrr_inferred_from_xero_sales_invoices")
        bucket["source_mix"]["xero_sales_invoices_inferred"] += 1
        bucket["source_record_ids"].append(record.id)

    existing_snapshots = {
        (snapshot.month, snapshot.currency): snapshot
        for snapshot in organization.monthly_revenue_snapshots.all()
    }
    currencies = sorted(
        {
            currency
            for _month, currency in buckets.keys()
            if currency
        }
        | {
            currency
            for _month, currency in existing_snapshots.keys()
            if currency
        }
    )
    if len(currencies) > 1:
        for bucket in buckets.values():
            bucket["warnings"].append("multiple_currencies_kept_separate")

    snapshots: list[MonthlyRevenueSnapshot] = []
    for currency in currencies:
        for month in sorted(month for month, bucket_currency in buckets.keys() if bucket_currency == currency):
            bucket = buckets[(month, currency)]
            current_mrr = _money(bucket["mrr"])
            previous_snapshot = existing_snapshots.get((_previous_month_start(month), currency))
            previous_mrr = previous_snapshot.mrr_amount if previous_snapshot else None
            if previous_mrr is None:
                mrr_delta = Decimal("0")
                growth_rate = None
            else:
                mrr_delta = _money(current_mrr - previous_mrr)
                growth_rate = None if previous_mrr == 0 else _rate((current_mrr - previous_mrr) / previous_mrr)

            snapshot, _ = MonthlyRevenueSnapshot.objects.update_or_create(
                organization=organization,
                month=month,
                currency=currency,
                defaults={
                    "run": run,
                    "mrr_amount": current_mrr,
                    "mrr_growth_rate": growth_rate,
                    "mrr_delta": mrr_delta,
                    "cash_collected_amount": _money(bucket["cash"]),
                    "recognized_revenue_amount": _money(bucket["recognized"]),
                    "confidence": float(bucket["confidence"] or 0.0),
                    "source_mix": dict(bucket["source_mix"]),
                    "warnings": sorted(set(bucket["warnings"])),
                    "source_record_ids": sorted(set(bucket["source_record_ids"])),
                    "calculated_at": timezone.now(),
                },
            )
            snapshots.append(snapshot)

    return snapshots


def publish_revenue_snapshots_to_startup_metrics(
    *,
    organization: Organization,
    run: Optional[ContentFactoryRun],
    snapshots: Iterable[MonthlyRevenueSnapshot],
) -> int:
    count = 0
    for snapshot in snapshots:
        count += _upsert_finance_metric(
            organization=organization,
            run=run,
            metric_key="mrr",
            metric_name="Monthly Recurring Revenue",
            value_number=snapshot.mrr_amount,
            value_text=_format_money(snapshot.currency, snapshot.mrr_amount),
            unit=snapshot.currency,
            period_month=snapshot.month,
            confidence=snapshot.confidence,
            summary="Deterministically calculated from connected Stripe and Xero finance records.",
        )
        if snapshot.mrr_growth_rate is not None:
            count += _upsert_finance_metric(
                organization=organization,
                run=run,
                metric_key="revenue_growth_rate",
                metric_name="Monthly MRR Growth Rate",
                value_number=snapshot.mrr_growth_rate,
                value_text=_format_percent(snapshot.mrr_growth_rate),
                unit="ratio",
                period_month=snapshot.month,
                confidence=snapshot.confidence,
                summary="Calculated as current month MRR minus previous month MRR divided by previous month MRR.",
            )
        count += _upsert_finance_metric(
            organization=organization,
            run=run,
            metric_key="cash_collected",
            metric_name="Cash Collected",
            value_number=snapshot.cash_collected_amount,
            value_text=_format_money(snapshot.currency, snapshot.cash_collected_amount),
            unit=snapshot.currency,
            period_month=snapshot.month,
            confidence=max(0.5, snapshot.confidence),
            summary="Cash validation from paid invoices, payments, and bank transactions in connected finance systems.",
        )
    return count


def _upsert_finance_metric(
    *,
    organization: Organization,
    run: Optional[ContentFactoryRun],
    metric_key: str,
    metric_name: str,
    value_number: Decimal,
    value_text: str,
    unit: str,
    period_month: date,
    confidence: float,
    summary: str,
) -> int:
    metric = (
        StartupMetricObservation.objects.filter(
            organization=organization,
            source_thread=None,
            metric_key=metric_key,
            period_month=period_month,
            unit=unit,
        )
        .order_by("-updated_at")
        .first()
    )
    defaults = {
        "run": run,
        "metric_name": metric_name,
        "value_text": value_text,
        "value_number": value_number,
        "observed_at": timezone.now(),
        "confidence": confidence,
        "evidence_message_ids": [],
        "evidence_attachment_ids": [],
        "summary": summary,
    }
    if metric is None:
        StartupMetricObservation.objects.create(
            organization=organization,
            source_thread=None,
            metric_key=metric_key,
            period_month=period_month,
            unit=unit,
            **defaults,
        )
        return 1

    for field_name, field_value in defaults.items():
        setattr(metric, field_name, field_value)
    metric.save(
        update_fields=[
            "run",
            "metric_name",
            "value_text",
            "value_number",
            "observed_at",
            "confidence",
            "evidence_message_ids",
            "evidence_attachment_ids",
            "summary",
            "updated_at",
        ]
    )
    StartupMetricObservation.objects.filter(
        organization=organization,
        source_thread=None,
        metric_key=metric_key,
        period_month=period_month,
        unit=unit,
    ).exclude(id=metric.id).delete()
    return 1


def _record_month(record: ExternalFinancialRecord) -> Optional[date]:
    candidate = record.period_start or (record.occurred_at.date() if record.occurred_at else None)
    if candidate is None:
        return None
    return date(candidate.year, candidate.month, 1)


def _is_revenue_xero_record(record: ExternalFinancialRecord) -> bool:
    raw = record.raw_payload or {}
    raw_type = str(raw.get("Type") or "").upper()
    if raw_type and raw_type != "ACCREC":
        return False
    status = str(record.source_status or "").upper()
    if record.object_type == FinancialRecordType.REPEATING_INVOICE:
        return status in {"AUTHORISED", "ACTIVE"}
    return status in {"AUTHORISED", "PAID"}


def _is_cash_inflow_record(record: ExternalFinancialRecord) -> bool:
    if (record.amount or Decimal("0")) <= 0:
        return False
    raw = record.raw_payload or {}
    raw_type = str(raw.get("Type") or raw.get("PaymentType") or "").upper()
    if record.object_type == FinancialRecordType.BANK_TRANSACTION:
        return raw_type.startswith("RECEIVE")
    return True


def _record_looks_stripe_origin(record: ExternalFinancialRecord) -> bool:
    raw = record.raw_payload or {}
    haystack = " ".join(
        str(raw.get(key) or "")
        for key in ["Reference", "Url", "InvoiceNumber", "LineAmountTypes", "SubTotal"]
    ).lower()
    return "stripe" in haystack


def _stripe_subscription_mrr(raw: dict) -> tuple[Decimal, str]:
    if str(raw.get("status") or "").lower() not in {"active", "past_due"}:
        return Decimal("0"), ""
    total = Decimal("0")
    currency = ""
    for item in ((raw.get("items") or {}).get("data") or []):
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        interval = str(recurring.get("interval") or "month").lower()
        interval_count = max(int(recurring.get("interval_count") or 1), 1)
        quantity = Decimal(str(item.get("quantity") or 1))
        unit_amount = _minor_units_to_decimal(price.get("unit_amount_decimal", price.get("unit_amount")))
        monthly_amount = _normalize_recurring_amount(unit_amount * quantity, interval=interval, interval_count=interval_count)
        total += monthly_amount
        currency = currency or str(price.get("currency") or "").upper()
    total = _apply_stripe_subscription_discount(total, raw)
    return _money(total), currency


def _apply_stripe_subscription_discount(amount: Decimal, raw: dict) -> Decimal:
    discount = raw.get("discount") or {}
    coupon = discount.get("coupon") or {}
    if coupon.get("percent_off") not in (None, ""):
        return max(Decimal("0"), amount * (Decimal("100") - _to_decimal(coupon["percent_off"])) / Decimal("100"))
    if coupon.get("amount_off") not in (None, ""):
        return max(Decimal("0"), amount - _minor_units_to_decimal(coupon.get("amount_off")))
    return amount


def _normalize_xero_repeating_invoice_mrr(raw: dict) -> Decimal:
    amount = _to_decimal(raw.get("Total"))
    schedule = raw.get("Schedule") or {}
    unit = str(schedule.get("Unit") or schedule.get("unit") or "MONTHLY").upper()
    period = max(int(schedule.get("Period") or schedule.get("period") or 1), 1)
    if "YEAR" in unit:
        return _money(amount / Decimal(12 * period))
    if "WEEK" in unit:
        return _money(amount * Decimal(52) / Decimal(12 * period))
    if "DAY" in unit:
        return _money(amount * Decimal(365) / Decimal(12 * period))
    return _money(amount / Decimal(period))


def _normalize_recurring_amount(amount: Decimal, *, interval: str, interval_count: int) -> Decimal:
    if interval == "year":
        return amount / Decimal(12 * interval_count)
    if interval == "week":
        return amount * Decimal(52) / Decimal(12 * interval_count)
    if interval == "day":
        return amount * Decimal(365) / Decimal(12 * interval_count)
    return amount / Decimal(interval_count)


def _minor_units_to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return _to_decimal(value) / Decimal("100")


def _to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return _to_decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    return _to_decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _date_from_unix(value) -> Optional[date]:
    dt = _datetime_from_unix(value)
    return dt.date() if dt else None


def _datetime_from_unix(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_date_value(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parsed = parse_date(str(value))
    if parsed:
        return parsed
    parsed_dt = _parse_datetime_value(value)
    return parsed_dt.date() if parsed_dt else None


def _parse_datetime_value(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        parsed = parse_datetime(text) or _parse_xero_json_datetime(text)
    if not parsed:
        parsed_date = parse_date(str(value))
        if not parsed_date:
            return None
        parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone=dt_timezone.utc)
    return parsed


def _parse_xero_json_datetime(value: str) -> Optional[datetime]:
    if not value.startswith("/Date(") or ")/" not in value:
        return None
    millis = value.removeprefix("/Date(").split(")", 1)[0]
    try:
        return datetime.fromtimestamp(int(millis) / 1000, tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _hash_payload(payload: dict) -> str:
    import json

    encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recent_month_starts(count: int) -> list[date]:
    today = timezone.now().date()
    current = date(today.year, today.month, 1)
    months = []
    for offset in range(count):
        month = current.month - offset
        year = current.year
        while month <= 0:
            month += 12
            year -= 1
        months.append(date(year, month, 1))
    return list(reversed(months))


def _previous_month_start(month: date) -> date:
    year = month.year
    previous_month = month.month - 1
    if previous_month <= 0:
        previous_month = 12
        year -= 1
    return date(year, previous_month, 1)


def _format_money(currency: str, amount: Decimal) -> str:
    return f"{str(currency or '').upper()} {_money(amount):,.2f}".strip()


def _format_percent(value: Decimal) -> str:
    return f"{(_to_decimal(value) * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
