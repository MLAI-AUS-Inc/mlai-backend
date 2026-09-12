"""Founder edits may not replace the financial evidence in a saved snapshot."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

FINANCIAL_KEYS = frozenset({
    "revenue", "monthlyCosts", "netProfitLoss", "operatingExpenses", "costOfSales",
    "mrr", "arr", "burnRate", "runway", "invoiceRevenue", "cashCollected",
    "cashBalance", "grossProfit", "grossMargin",
})
FINANCIAL_PROVIDERS = frozenset({"xero", "stripe", "financial", "bank_feed"})


class FinancialMetricEditError(ValueError):
    pass


def _same_value(value, metric):
    text = "" if value is None else str(value).strip()
    display = metric.get("display_value")
    if text == ("" if display is None else str(display).strip()):
        return True
    # Existing clients send either the display or an unformatted number.
    # Do not accept another currency or strip arbitrary text to make it match.
    unit = str(metric.get("unit") or "")
    if unit and text.startswith(unit + " "):
        text = text[len(unit):].strip()
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return False
    try:
        candidate = Decimal(text.replace(",", ""))
        expected = Decimal(str(metric.get("value")))
        return candidate.is_finite() and expected.is_finite() and candidate == expected
    except (InvalidOperation, ValueError):
        return False


def founder_metric_changes(incoming, snapshot_metrics, previous=None):
    """Return only manual changes, using server-owned evidence for locked fields.

    Missing keys preserve their frozen values. Unknown financial values cannot
    be promoted to connector evidence by a browser, including on a first save.
    """
    by_key = {item["key"]: item for item in snapshot_metrics}
    changes = {}
    for key, value in incoming.items():
        metric = by_key.get(key, {})
        locked = key in FINANCIAL_KEYS or metric.get("source_provider") in FINANCIAL_PROVIDERS
        if locked:
            if _same_value(value, metric):
                continue
            provider = metric.get("source_provider")
            source = "Stripe" if provider == "financial" else (str(provider).title() if provider else "your financial source")
            if provider == "founder":
                source = "a financial connection"
            label = metric.get("label") or key
            raise FinancialMetricEditError(
                f"{label} is read-only. Update {source} and refresh the draft to change financial figures."
            )
        if not _same_value(value, metric) and (previous or {}).get(key) != value:
            changes[key] = value
    return changes


def snapshot_for_founder_edit(*, organization, month, draft, incoming, previous, capture):
    """Keep the exact evidence snapshot for writing/cover edits; fork only manual metrics."""
    base = draft.current_revision.snapshot if draft.current_revision_id else capture(organization, month)
    changes = founder_metric_changes(incoming, base.payload.get("metrics", []), previous)
    return (
        capture(organization, month, manual_metrics=changes, base_snapshot=base) if changes else base,
        changes,
    )
