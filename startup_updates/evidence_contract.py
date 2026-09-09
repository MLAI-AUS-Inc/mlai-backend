"""Pure, versioned reporting contracts shared by snapshots and their renderers."""
from __future__ import annotations

import calendar
import copy
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
REVENUE_DEFINITION = {
    "key": "revenue", "label": "Revenue", "version": 2, "unit": "money",
    "definition": "Accounting revenue from Xero; otherwise paid Stripe sales excluding tax. Sources are never added together.",
}


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def reporting_period(month: date, timezone_name: str = "UTC", *, as_of: datetime | None = None) -> dict:
    zone = ZoneInfo(timezone_name)
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    start = datetime(month.year, month.month, 1, tzinfo=zone)
    end_month = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
    end = datetime(end_month.year, end_month.month, 1, tzinfo=zone)
    if start > now:
        raise ValueError("Cannot report a future month")
    return {
        "month": start.date().isoformat(), "timezone": timezone_name,
        "start": start.isoformat(), "end_exclusive": end.isoformat(),
        "as_of": now.isoformat(), "cutoff": min(now, end).isoformat(),
        "is_partial": now < end,
    }


def decimal_value(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def customer_receipt(record) -> bool:
    """Xero supplier payments share a record type with customer receipts."""
    payload = record.raw_payload or {}
    invoice = payload.get("Invoice") or {}
    payment_type = str(payload.get("PaymentType") or "").upper()
    return (
        str(record.status or "").upper() not in {"DELETED", "VOIDED", "FAILED"}
        and str(record.direction or "").lower() == "credit"
        and str(invoice.get("Type") or "").upper() != "ACCPAY"
        and payment_type not in {"ACCPAYPAYMENT", "ARCREDITPAYMENT"}
        and str(getattr(record, "category", "") or "").lower() != "bill_payment"
    )


def financial_snapshot_from_metrics(period: dict, metrics: list[dict], definitions: list[dict] | None = None) -> dict:
    """Freeze complete values and provenance, not references to mutable rows alone."""
    payload = {
        "schema_version": SCHEMA_VERSION, "period": copy.deepcopy(period),
        "definitions": copy.deepcopy(definitions or [REVENUE_DEFINITION]),
        "metrics": copy.deepcopy(metrics),
    }
    payload["hash"] = content_hash(payload)
    return payload


def render_metric_claims(memo: dict, metrics: list[dict]) -> dict:
    """Resolve model metric tokens. Models do not format financial amounts."""
    values = {str(item["key"]): str(item["display_value"]) for item in metrics if item.get("display_value") is not None}
    token = re.compile(r"\{\{metric:([A-Za-z0-9_.-]+)\}\}")
    def visit(value):
        if isinstance(value, str):
            def replace(match):
                if match[1] not in values:
                    raise ValueError(f"Unsupported metric reference: {match[1]}")
                return values[match[1]]
            return token.sub(replace, value)
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        return value
    return visit(copy.deepcopy(memo))


def health_assessment(snapshot: dict) -> dict:
    metrics = snapshot.get("metrics", [])
    available = [item for item in metrics if item.get("display_value") is not None]
    attention = [
        {"metric_key": item["key"], "reason": item.get("reason") or ("; ".join((item.get("metadata") or {}).get("limitations", []))) or "Confirm this metric's source and period."}
        for item in metrics if item.get("display_value") is None or item.get("quality") in {"disputed", "stale", "founder_asserted", "partial"}
    ]
    by_key = {item["key"]: item for item in metrics}
    revenue, costs = by_key.get("revenue", {}), by_key.get("monthlyCosts", {})
    revenue_value, costs_value = decimal_value(revenue.get("value")), decimal_value(costs.get("value"))
    if (revenue_value is not None and costs_value is not None
            and revenue.get("source_provider") == costs.get("source_provider") == "xero"
            and revenue.get("unit") == costs.get("unit") and costs_value > revenue_value):
        attention.append({"metric_key": "financial_result", "reason": "Recorded costs exceed revenue for this period."})
    return {
        "snapshot_hash": snapshot.get("hash"), "period": snapshot.get("period"),
        "summary": "Review the changes below against your current priorities." if available else "There is not enough recorded evidence to assess business health.",
        "metrics": metrics, "attention": attention,
    }


def stripe_paid_invoice_sales_minor(payload: dict):
    """Paid invoice sales excluding tax, in minor units; ambiguous adjustments stay unknown.

    This is a processor-only sales measure, not reconciled accounting revenue.
    Credit notes require line-level tax allocation; never prorate them by guesswork.
    """
    if str(payload.get("status") or "").lower() != "paid":
        return None
    paid, total = decimal_value(payload.get("amount_paid")), decimal_value(payload.get("total"))
    excluding_tax = decimal_value(payload.get("total_excluding_tax"))
    if paid is None or total is None or paid < 0 or total < 0:
        return None
    if decimal_value(payload.get("post_payment_credit_notes_amount") or 0) != 0:
        return None
    if decimal_value(payload.get("pre_payment_credit_notes_amount") or 0) != 0:
        return None
    if paid == 0:
        return Decimal("0")
    if excluding_tax is None or total <= 0 or excluding_tax < 0 or excluding_tax > total:
        return None
    # Partial/out-of-band and overpayment allocation cannot be reconstructed from totals alone.
    if paid != total:
        return None
    return excluding_tax


def validate_generated_metric_claims(memo: dict):
    """Numeric financial claims must be tokens, never free-form LLM amounts."""
    token = re.compile(r"\{\{metric:[A-Za-z0-9_.-]+\}\}")
    financial_number = re.compile(r"(?:[$€£¥]\s*\d|\b(?:USD|AUD|EUR|GBP|revenue|profit|costs?|MRR|ARR)\s*[:=]?\s*[-(]?\d)", re.I)
    def visit(value):
        if isinstance(value, str) and financial_number.search(token.sub("METRIC", value)):
            raise ValueError("Financial claims must reference snapshot metric tokens.")
        if isinstance(value, list):
            for item in value:
                visit(item)
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in {"kpi_snapshot", "source_notes", "metric_suggestions"}:
                    visit(item)
    visit(memo)
