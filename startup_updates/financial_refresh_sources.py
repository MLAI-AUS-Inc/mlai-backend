"""Bounded, read-only financial collection for a single Pulse chart candidate.

These requests never publish observations or alter another draft's evidence.
Stripe receipts are separate payment-source evidence, never accounting revenue.
"""
import calendar
import copy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from integrations import http_client as requests
from integrations.services.external_connectors import (
    ConnectorOAuthError, ConnectorRateLimitError, ConnectorTemporarilyUnavailable,
    fetch_xero_accounting_report, fetch_xero_base_currency,
)
from integrations.services.xero_scopes import normalize_xero_scopes
from startup_updates.evidence_contract import content_hash


def month_slots(month):
    """Return the six calendar month starts ending at the selected month."""
    index = month.year * 12 + month.month - 1
    return [date((index + offset) // 12, (index + offset) % 12 + 1, 1)
        for offset in range(-5, 1)]


def connection_identity(connection):
    """Pin source authority without persisting secrets or rotating token values."""
    return {"id": connection.pk, "account": connection.external_account_id,
        "scopes": sorted(normalize_xero_scopes(connection.scopes))}


def next_stage(progress, sources):
    """Choose one bounded provider read, resuming at the last durable receipt."""
    for provider in sorted(sources, key=lambda value: value != "xero"):
        state = progress["sources"][provider]["status"]
        if state in {"ready", "failed", "reconnect_required"}:
            continue
        if provider == "xero":
            if not progress.get("xero", {}).get("currency"):
                return provider, "currency"
            completed = progress["xero"].get("reports", {})
            return provider, next((month for month in progress["months"] if month not in completed), "done")
        return provider, "invoices"
    return None


def fetch_stage(provider, stage, connection, request, progress):
    """Read one report or invoice page, using the run's immutable time window."""
    if not connection.access_token:
        raise ConnectorOAuthError("Reconnect the selected source.")
    if provider == "xero":
        scopes = normalize_xero_scopes(connection.scopes)
        if not scopes.intersection({"accounting.reports", "accounting.reports.read", "accounting.reports.profitandloss.read"}):
            raise ConnectorOAuthError("Reconnect Xero to allow Profit and Loss reports.")
        if stage == "currency":
            currency = fetch_xero_base_currency(connection)
            if currency != request["currency"]:
                raise ValueError("The Xero currency does not match the startup reporting currency.")
            return {"currency": currency}
        month = date.fromisoformat(stage)
        cutoff = min(date.fromisoformat(request["coverageEnd"]),
            month.replace(day=calendar.monthrange(month.year, month.month)[1]))
        report = fetch_xero_accounting_report(connection, "ProfitAndLoss", params={
            "fromDate": stage, "toDate": cutoff.isoformat(), "paymentsOnly": "false", "standardLayout": "true"})
        reports = report.get("Reports") if isinstance(report, dict) else None
        if not isinstance(reports, list) or not any(item.get("ReportID") == "ProfitAndLoss" for item in reports if isinstance(item, dict)):
            raise ValueError("Xero did not return a Profit and Loss report.")
        return {"month": stage, "end": cutoff.isoformat(), "report": report}
    # Payment evidence remains separately labelled. Created-in-window invoices
    # do not establish company revenue or complete cash receipts for that window.
    from integrations.services.finance import STRIPE_API_BASE_URL
    start = datetime.combine(date.fromisoformat(progress["months"][0]), datetime.min.time(),
        tzinfo=ZoneInfo(request["timezone"]))
    params = {"limit": 100, "created[gte]": int(start.timestamp()),
        "created[lt]": int(datetime.fromisoformat(request["period"]["cutoff"]).timestamp())}
    cursor = progress.get("stripe", {}).get("cursor")
    if cursor:
        params["starting_after"] = cursor
    response = requests.get(f"{STRIPE_API_BASE_URL}/v1/invoices", headers={
        "Authorization": f"Bearer {connection.access_token}",
        "Stripe-Version": str(getattr(settings, "STRIPE_API_VERSION", "2026-07-29.dahlia")),
    }, params=params, timeout=(3, 20))
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Stripe returned an incomplete invoice page.")
    next_cursor = rows[-1].get("id") if rows else None
    more = bool(payload.get("has_more"))
    if more and (not next_cursor or next_cursor == cursor or progress.get("stripe", {}).get("pages", 0) >= 49):
        raise ValueError("Stripe invoice collection could not complete within the refresh limit.")
    return {"count": len(rows), "hash": content_hash(payload), "cursor": next_cursor, "more": more}


def apply_stage(progress, provider, stage, receipt, *, now=None):
    """Merge a provider receipt without retaining Stripe customer information."""
    result = copy.deepcopy(progress)
    captured = (now or timezone.now()).isoformat()
    if provider == "xero":
        target = result.setdefault("xero", {})
        if stage == "currency":
            target.update(receipt)
        else:
            target.setdefault("reports", {})[stage] = receipt
        complete = len(target.get("reports", {})) == 6
    else:
        target = result.setdefault("stripe", {"count": 0, "pages": 0, "pageHashes": []})
        target.update(count=target["count"] + receipt["count"], pages=target["pages"] + 1, cursor=receipt["cursor"])
        target["pageHashes"].append(receipt["hash"])
        complete = not receipt["more"]
    result["sources"][provider] = {"provider": provider, "status": "ready" if complete else "refreshing"}
    if complete:
        result["sources"][provider]["lastRefreshedAt"] = captured
    result.pop("retryAt", None)
    result.pop("attempts", None)
    return result


def build_candidate_evidence(request, progress):
    """Use only fetched Xero reports, with verified accounting totals and gaps."""
    from startup_updates.services import _parse_xero_profit_and_loss_report
    reports, rows = {}, []
    for month in progress["months"]:
        receipt = progress["xero"]["reports"][month]
        report = receipt["report"]
        digest = content_hash(report)
        reports[digest] = copy.deepcopy(report)
        totals = _parse_xero_profit_and_loss_report(report)
        metadata = {"unit": request["currency"], "source_provider": "xero",
            "accounting_basis": "accrual", "connection_id": request["connections"]["xero"]["id"],
            "timezone": request["timezone"], "report_hash": digest,
            "report_start_date": month, "report_end_date": receipt["end"]}
        row = {"month": month, "metric_evidence": {}}
        for key, field, source in (("revenue", "income", "xero_profit_and_loss_revenue"),
                ("monthly_costs", "expenses", "xero_profit_and_loss_monthly_costs")):
            total = totals.get(key)
            row[field] = str(total["amount"]) if total else None
            row["metric_evidence"][field] = {**metadata, "source_metric": source}
        rows.append(row)
    return {"schema_version": 1, "organization_id": request["organizationId"],
        "period": copy.deepcopy(request["period"]), "metrics": [],
        "charts": {"schema_version": 2, "target_month": request["targetMonth"],
            "currency": request["currency"], "performance": rows, "source_reports": reports},
        "financial_refresh": {"requestId": request["requestId"],
            "sources": copy.deepcopy(progress["sources"]),
            "stripe": copy.deepcopy(progress.get("stripe")),
            "stripeBasis": "Invoices created in this window; payment evidence only, never added to accounting revenue."}}


# Waits longer than this are a provider quota (Xero's daily limit), not a
# blip. Parking a refresh for an hour looks stuck, so fail it and say when.
LONG_WAIT_SECONDS = 300


def local_time(moment, tz_name):
    """Format a deadline in the founder's reporting timezone, e.g. 12:00 am."""
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except (KeyError, ValueError):
        zone = ZoneInfo("UTC")
    local = moment.astimezone(zone)
    text = f"{local:%I:%M %p}".lstrip("0").lower()
    if local.date() != timezone.now().astimezone(zone).date():
        text += f" on {local:%a} {local.day} {local:%b}"
    return text


def failure_details(exc, provider, *, tz_name=None, now=None):
    """Expose safe retry/reconnect information without upstream bodies or tokens."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    temporary = isinstance(exc, (ConnectorTemporarilyUnavailable, ConnectorRateLimitError))
    reconnect = not temporary and (isinstance(exc, ConnectorOAuthError) or status in {401, 403})
    retryable = not reconnect and (temporary or isinstance(exc, (requests.RequestException, TimeoutError)) or status == 429)
    delay = min(3600, max(15, getattr(exc, "retry_after_seconds", 15)))
    wait = delay
    if status == 429:
        wait = requests._retry_after_seconds(getattr(response, "headers", {}).get("Retry-After"))
        delay = min(3600, max(15, wait))
    elif isinstance(exc, ConnectorRateLimitError):
        wait = exc.retry_after_seconds
    name = provider.title()
    if retryable and wait > LONG_WAIT_SECONDS:
        retry_at = (now or timezone.now()) + timedelta(seconds=wait)
        return {"provider": provider, "status": "failed", "reason": "rate_limited", "retryAt": retry_at.isoformat(),
            "detail": f"{name} has reached its {'daily ' if provider == 'xero' else ''}API limit. "
                f"Try again after {local_time(retry_at, tz_name)}. "
                "Your saved chart and writing are unchanged."}, False, wait
    return {"provider": provider, "status": "reconnect_required" if reconnect else "failed",
        "detail": f"Reconnect {name} to refresh this source." if reconnect else
            f"{name} could not finish refreshing. Your saved chart is unchanged."}, retryable, delay
