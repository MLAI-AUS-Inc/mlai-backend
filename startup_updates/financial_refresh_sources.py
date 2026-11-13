"""Bounded, read-only financial collection for a single Pulse chart candidate.

These requests never publish observations or alter another draft's evidence.
Xero supplies both bases: Profit and Loss (accrual) and Bank Summary (cash).
Stripe supplies monthly balance totals; whether they are added to Xero or shown
as the Stripe share of it is decided when the chart is read, not here.
"""
import calendar
import copy
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from integrations import http_client as requests
from integrations.services.external_connectors import (
    ConnectorOAuthError, ConnectorRateLimitError, ConnectorTemporarilyUnavailable,
    fetch_xero_accounting_report, fetch_xero_base_currency,
)
from integrations.services.xero_scopes import normalize_xero_scopes, xero_has_cash_report_scope
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


CASH_STAGE = "cash:"
STRIPE_STAGE = "balance"
# Stripe reporting categories, mapped to the four monthly figures Pulse keeps.
# Older API versions without reporting_category fall back to the row type.
STRIPE_SALES = {"charge", "payment"}
STRIPE_REFUNDS = {"refund", "payment_refund", "dispute", "partial_capture_reversal", "refund_failure", "dispute_reversal"}
STRIPE_PAYOUTS = {"payout", "payout_reversal"}
STRIPE_FEES = {"fee", "stripe_fee"}


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
            month = next((month for month in progress["months"] if month not in completed), None)
            if month:
                return provider, month
            if not progress["xero"].get("cashUnavailable"):
                cash = progress["xero"].get("cash", {})
                month = next((month for month in progress["months"] if month not in cash), None)
                if month:
                    return provider, CASH_STAGE + month
            return provider, "done"
        return provider, STRIPE_STAGE
    return None


def _xero_window(stage, request):
    month = date.fromisoformat(stage)
    cutoff = min(date.fromisoformat(request["coverageEnd"]),
        month.replace(day=calendar.monthrange(month.year, month.month)[1]))
    return month, cutoff


def fetch_stage(provider, stage, connection, request, progress):
    """Read one report or Stripe page, using the run's immutable time window."""
    if not connection.access_token:
        raise ConnectorOAuthError("Reconnect the selected source.")
    if provider == "xero":
        scopes = normalize_xero_scopes(connection.scopes)
        if stage.startswith(CASH_STAGE):
            month, cutoff = _xero_window(stage[len(CASH_STAGE):], request)
            if not xero_has_cash_report_scope(scopes):
                # Accrual still works; the founder is asked to reconnect for cash.
                return {"month": month.isoformat(), "unavailable": "scope"}
            try:
                report = fetch_xero_accounting_report(connection, "BankSummary", params={
                    "fromDate": month.isoformat(), "toDate": cutoff.isoformat()})
            except ConnectorOAuthError:
                return {"month": month.isoformat(), "unavailable": "scope"}
            except requests.HTTPError as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403}:
                    return {"month": month.isoformat(), "unavailable": "scope"}
                raise
            reports = report.get("Reports") if isinstance(report, dict) else None
            if not isinstance(reports, list) or not any(item.get("ReportID") == "BankSummary" for item in reports if isinstance(item, dict)):
                raise ValueError("Xero did not return a Bank Summary report.")
            return {"month": month.isoformat(), "end": cutoff.isoformat(), "report": report}
        if not scopes.intersection({"accounting.reports", "accounting.reports.read", "accounting.reports.profitandloss.read"}):
            raise ConnectorOAuthError("Reconnect Xero to allow Profit and Loss reports.")
        if stage == "currency":
            currency = fetch_xero_base_currency(connection)
            if currency != request["currency"]:
                raise ValueError("The Xero currency does not match the startup reporting currency.")
            return {"currency": currency}
        month, cutoff = _xero_window(stage, request)
        report = fetch_xero_accounting_report(connection, "ProfitAndLoss", params={
            "fromDate": stage, "toDate": cutoff.isoformat(), "paymentsOnly": "false", "standardLayout": "true"})
        reports = report.get("Reports") if isinstance(report, dict) else None
        if not isinstance(reports, list) or not any(item.get("ReportID") == "ProfitAndLoss" for item in reports if isinstance(item, dict)):
            raise ValueError("Xero did not return a Profit and Loss report.")
        return {"month": stage, "end": cutoff.isoformat(), "report": report}
    return _fetch_stripe_balance_page(connection, request, progress)


def _fetch_stripe_balance_page(connection, request, progress):
    """One page of Stripe balance transactions, reduced to monthly totals.

    Balance transactions cover every way money moves through Stripe: invoices,
    Checkout, Payment Links and subscriptions alike. Only totals per reporting
    month are kept; no customer, card or description data leaves this function.
    """
    from integrations.services.finance import STRIPE_API_BASE_URL, _minor_units
    zone = ZoneInfo(request["timezone"])
    start = datetime.combine(date.fromisoformat(progress["months"][0]), datetime.min.time(), tzinfo=zone)
    params = {"limit": 100, "created[gte]": int(start.timestamp()),
        "created[lt]": int(datetime.fromisoformat(request["period"]["cutoff"]).timestamp())}
    cursor = progress.get("stripe", {}).get("cursor")
    if cursor:
        params["starting_after"] = cursor
    response = requests.get(f"{STRIPE_API_BASE_URL}/v1/balance_transactions", headers={
        "Authorization": f"Bearer {connection.access_token}",
        "Stripe-Version": str(getattr(settings, "STRIPE_API_VERSION", "2026-07-29.dahlia")),
    }, params=params, timeout=(3, 20))
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Stripe returned an incomplete balance page.")
    next_cursor = rows[-1].get("id") if rows else None
    more = bool(payload.get("has_more"))
    if more and (not next_cursor or next_cursor == cursor or progress.get("stripe", {}).get("pages", 0) >= 99):
        raise ValueError("Stripe balance collection could not complete within the refresh limit.")
    months = set(progress["months"])
    totals, other_currency = {}, 0
    for row in rows:
        currency = str(row.get("currency") or "").upper()
        created = row.get("created")
        if type(created) is not int:
            continue
        month = datetime.fromtimestamp(created, tz=dt_timezone.utc).astimezone(zone).date().replace(day=1).isoformat()
        if month not in months:
            continue
        if currency != request["currency"]:
            other_currency += 1
            continue
        category = str(row.get("reporting_category") or row.get("type") or "")
        amount = _minor_units(row.get("amount"), currency)
        fee = _minor_units(row.get("fee"), currency)
        bucket = totals.setdefault(month, {"sales": Decimal("0"), "refunds": Decimal("0"), "fees": Decimal("0"), "payouts": Decimal("0")})
        if category in STRIPE_SALES:
            bucket["sales"] += amount
            bucket["fees"] += fee
        elif category in STRIPE_REFUNDS:
            bucket["refunds"] -= amount
            bucket["fees"] += fee
        elif category in STRIPE_PAYOUTS:
            bucket["payouts"] -= amount
        elif category in STRIPE_FEES:
            bucket["fees"] -= amount
    return {"count": len(rows), "hash": content_hash(payload), "cursor": next_cursor, "more": more,
        "months": {month: {key: str(value) for key, value in bucket.items()} for month, bucket in totals.items()},
        "otherCurrency": other_currency}


def apply_stage(progress, provider, stage, receipt, *, now=None):
    """Merge a provider receipt without retaining Stripe customer information."""
    result = copy.deepcopy(progress)
    captured = (now or timezone.now()).isoformat()
    if provider == "xero":
        target = result.setdefault("xero", {})
        if stage == "currency":
            target.update(receipt)
        elif stage.startswith(CASH_STAGE):
            if receipt.get("unavailable"):
                target["cashUnavailable"] = receipt["unavailable"]
            else:
                target.setdefault("cash", {})[stage[len(CASH_STAGE):]] = receipt
        else:
            target.setdefault("reports", {})[stage] = receipt
        months = len(result.get("months") or []) or 6
        complete = len(target.get("reports", {})) == months and (
            bool(target.get("cashUnavailable")) or len(target.get("cash", {})) == months)
    else:
        target = result.setdefault("stripe", {"count": 0, "pages": 0, "pageHashes": []})
        target.update(count=target["count"] + receipt["count"], pages=target["pages"] + 1, cursor=receipt["cursor"])
        target["pageHashes"].append(receipt["hash"])
        merged = target.setdefault("months", {})
        for month, values in (receipt.get("months") or {}).items():
            bucket = merged.setdefault(month, {"sales": "0", "refunds": "0", "fees": "0", "payouts": "0"})
            for key, value in values.items():
                bucket[key] = str(Decimal(bucket.get(key, "0")) + Decimal(value))
        target["otherCurrency"] = target.get("otherCurrency", 0) + receipt.get("otherCurrency", 0)
        complete = not receipt["more"]
    result["sources"][provider] = {"provider": provider, "status": "ready" if complete else "refreshing"}
    if complete:
        result["sources"][provider]["lastRefreshedAt"] = captured
        if provider == "xero" and result["xero"].get("cashUnavailable") == "scope":
            result["sources"][provider]["detail"] = "Reconnect Xero to show cash in and out. Accrual figures refreshed."
        if provider == "stripe" and target.get("otherCurrency"):
            result["sources"][provider]["detail"] = (f"{target['otherCurrency']} Stripe transactions in another currency "
                "were left out of the chart.")
    result.pop("retryAt", None)
    result.pop("attempts", None)
    return result


def build_candidate_evidence(request, progress):
    """Use only fetched reports, with verified totals for each basis and gaps."""
    from startup_updates.services import _parse_xero_bank_summary_report, _parse_xero_profit_and_loss_report
    reports, rows, cash_rows = {}, [], []
    xero = progress.get("xero") or {}
    xero_pin = (request.get("connections") or {}).get("xero")
    for month in progress["months"] if xero.get("reports") else []:
        receipt = xero["reports"][month]
        report = receipt["report"]
        digest = content_hash(report)
        reports[digest] = copy.deepcopy(report)
        totals = _parse_xero_profit_and_loss_report(report)
        metadata = {"unit": request["currency"], "source_provider": "xero",
            "accounting_basis": "accrual", "connection_id": xero_pin["id"],
            "timezone": request["timezone"], "report_hash": digest,
            "report_start_date": month, "report_end_date": receipt["end"]}
        row = {"month": month, "metric_evidence": {}}
        for key, field, source in (("revenue", "income", "xero_profit_and_loss_revenue"),
                ("monthly_costs", "expenses", "xero_profit_and_loss_monthly_costs")):
            total = totals.get(key)
            row[field] = str(total["amount"]) if total else None
            row["metric_evidence"][field] = {**metadata, "source_metric": source}
        rows.append(row)
    for month in progress["months"] if xero.get("cash") else []:
        receipt = xero["cash"].get(month)
        if not receipt:
            continue
        report = receipt["report"]
        digest = content_hash(report)
        reports[digest] = copy.deepcopy(report)
        cash = _parse_xero_bank_summary_report(report)
        metadata = {"unit": request["currency"], "source_provider": "xero",
            "accounting_basis": "cash", "connection_id": xero_pin["id"],
            "timezone": request["timezone"], "report_hash": digest,
            "report_start_date": month, "report_end_date": receipt["end"]}
        cash_rows.append({"month": month,
            "cash_in": str(cash["cash_in"]) if cash["cash_in"] is not None else None,
            "cash_out": str(cash["cash_out"]) if cash["cash_out"] is not None else None,
            "metric_evidence": {"cash_in": {**metadata, "source_metric": "xero_bank_summary_cash_in"},
                "cash_out": {**metadata, "source_metric": "xero_bank_summary_cash_out"}}})
    stripe = None
    stripe_pin = (request.get("connections") or {}).get("stripe")
    if (progress["sources"].get("stripe") or {}).get("status") == "ready" and stripe_pin:
        stripe = {"connection_id": stripe_pin["id"], "unit": request["currency"], "timezone": request["timezone"],
            "coverage_end": request["coverageEnd"], "basis": "stripe_balance_transactions",
            "months": {month: copy.deepcopy((progress.get("stripe") or {}).get("months", {}).get(month)
                or {"sales": "0", "refunds": "0", "fees": "0", "payouts": "0"}) for month in progress["months"]},
            "page_hashes": list((progress.get("stripe") or {}).get("pageHashes", []))}
    return {"schema_version": 1, "organization_id": request["organizationId"],
        "period": copy.deepcopy(request["period"]), "metrics": [],
        "charts": {"schema_version": 2, "target_month": request["targetMonth"],
            "currency": request["currency"], "performance": rows, "cash": cash_rows,
            "cash_unavailable": xero.get("cashUnavailable"), "stripe": stripe, "source_reports": reports},
        "financial_refresh": {"requestId": request["requestId"],
            "sources": copy.deepcopy(progress["sources"]),
            "stripe": {key: value for key, value in (progress.get("stripe") or {}).items() if key != "months"} or None,
            "stripeBasis": "Stripe balance transactions per reporting month: sales, refunds, fees and payouts."}}


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
