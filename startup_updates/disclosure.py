"""Financial information disclosed in updates is a relative aggregate chart."""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation

AGGREGATE_KEYS = {"revenue": "revenue", "monthlyCosts": "costs", "monthly_costs": "costs"}
ACCEPTED_QUALITIES = {"source_reported", "verified", "founder_asserted"}
CURRENCIES = {"AUD", "USD", "NZD", "CAD", "GBP", "EUR", "JPY", "CNY", "HKD", "SGD", "CHF", "INR", "KRW", "SEK", "NOK", "DKK", "ZAR", "BRL", "MXN"}
NARRATIVE_FIELDS = ("summary", "highlights", "challenges", "learnings", "next30Days", "asks")
SHARED_FIELDS = {
    "id", "updateTitle", "monthSequence", "coverImage", "coverImageUrl", "revisionId", "revisionHash", "isoMonth", "month", "monthName", "year", "date", "status", "visibility", "publishedAt",
    *NARRATIVE_FIELDS, "startup", "metrics", "metricEvidence", "displayConfig", "financialChart", "audienceVisibility", "evidenceStatus",
}
_METRIC_TOKEN = re.compile(r"\{\{metric:[A-Za-z0-9_.-]+\}\}")
_FINANCIAL_KEY = re.compile(r"revenue|income|turnover|profit|cost|expense|invoice|payment|budget|subtotal|fee|salary|wage|funding|raised|valuation|mrr|arr|cash|burn|runway|price|sales", re.I)
_CURRENCY_PATTERN = "|".join(sorted(CURRENCIES))
_EXPLICIT_MONEY = re.compile(
    rf"(?:[$€£¥₹]\s*[-(]?\s*\d|\b(?:{_CURRENCY_PATTERN})\s*[$€£¥₹]?\s*[-(]?\s*\d"
    rf"|\b\d[\d,.]*(?:\s*[kmb])?\s*(?:dollars?|euros?|pounds?|{_CURRENCY_PATTERN})\b)",
    re.I,
)
_FINANCIAL_TERM = re.compile(
    r"\b(?:revenues?|incomes?|turnovers?|profits?|costs?|expenses?|invoices?|payments?|budgets?|subtotals?|"
    r"fees?|salary|salaries|wages?|funding|raised|valuations?|cash|runway|burn|prices?|sales|MRR|ARR)\b", re.I,
)
_NUMBER = re.compile(r"\b\d[\d,.]*(?:\s*[kmb])?\b|\b(?:hundred|thousand|million|billion)\b", re.I)


def financial_metric_keys(metrics: list[dict] | None) -> set[str]:
    """Use frozen metric units to classify financially ambiguous placeholders."""
    return {str(item.get("key") or item.get("metric_key")) for item in metrics or []
        if isinstance(item, dict) and (re.fullmatch(r"[A-Z]{3}", str(item.get("unit") or "").upper())
            or item.get("unit") == "money" or (item.get("source_provider") in {"xero", "financial", "stripe"}
                and item.get("unit") not in {"count", "ratio", "months", "%"}))}


def has_financial_amount(text: str, *, metric_keys=()) -> bool:
    """Identify financial placeholders and numeric monetary claims in prose."""
    tokens = [match.group()[9:-2] for match in _METRIC_TOKEN.finditer(text)]
    text = re.sub(r"[*_`]", " ", text)
    return bool(any(key in metric_keys or _FINANCIAL_KEY.search(key) for key in tokens) or _EXPLICIT_MONEY.search(text)
        or (_FINANCIAL_TERM.search(text) and (_NUMBER.search(text) or tokens)))


def shared_narrative(value, *, metric_keys=()):
    """Remove amount-bearing sentences/bullets while preserving other story text."""
    if not isinstance(value, str):
        return value
    lines = []
    for line in value.splitlines():
        # Decimal points and abbreviations do not become sentence boundaries.
        sentences = re.split(r"(?<=[.!?])\s+(?!\d)", line)
        safe = [sentence for sentence in sentences if not has_financial_amount(sentence, metric_keys=metric_keys)]
        if safe:
            lines.append(" ".join(safe))
    return "\n".join(lines).strip()


def _amount(value, currency):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(?:(?P<currency>[A-Z]{3})\s*)?(?P<symbol>[$€£¥])?\s*(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)", text)
    if not match or (match['currency'] and match['currency'] != currency):
        return None
    symbol = match['symbol']
    symbol_currencies = {"$": {"AUD", "USD", "NZD", "CAD", "SGD", "HKD"}, "£": {"GBP"}, "€": {"EUR"}, "¥": {"JPY", "CNY"}}
    if symbol in symbol_currencies and currency not in symbol_currencies[symbol]:
        return None
    # No parsing of ranges, prose, per-invoice amounts or abbreviated values.
    try:
        number = Decimal(match['number'].replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def financial_chart(value: dict, *, respect_selection=False, metric_items=None):
    """Return relative revenue/cost bars only for compatible canonical totals."""
    validation = value.get("validation") or {}
    if value.get("evidenceStatus") == "legacy_unverified" or (isinstance(validation, dict) and validation.get("legacy_unverified")):
        return None
    selected = None
    if respect_selection and isinstance(value.get("displayConfig"), dict):
        full_keys = value["displayConfig"].get("fullMetricKeys") or []
        if not isinstance(full_keys, list):
            return None
        selected = {AGGREGATE_KEYS[key] for key in full_keys if isinstance(key, str) and key in AGGREGATE_KEYS}
    totals = {}
    currency = None
    providers = set()
    bases = {}
    metrics, evidence = value.get("metrics") or {}, value.get("metricEvidence") or {}
    if not isinstance(metrics, dict) or not isinstance(evidence, dict):
        return None
    candidates = [(key, amount, evidence.get(key) or {}) for key, amount in metrics.items()]
    if isinstance(metric_items, list):
        candidates = [(item.get("metric_key"), item.get("value"), item)
            for item in metric_items if isinstance(item, dict)]
    for key, amount, detail in candidates:
        field = AGGREGATE_KEYS.get(key)
        if not field or (selected is not None and field not in selected):
            continue
        if not isinstance(detail, dict) or detail.get("quality") not in ACCEPTED_QUALITIES:
            return None
        unit = str(detail.get("unit") or "").strip().upper()
        if unit not in CURRENCIES or (currency and currency != unit):
            return None
        currency = unit
        number = _amount(amount, unit)
        if number is None or (field in totals and totals[field] != number):
            return None
        totals[field] = number
        if detail.get("basis"):
            basis = str(detail["basis"])
            if field in bases and bases[field] != basis:
                return None
            bases[field] = basis
        if detail.get("source_provider"):
            providers.add(detail["source_provider"])
    if set(totals) != {"revenue", "costs"} or len(providers) > 1:
        return None
    if len(set(bases.values())) > 1 and bases != {
        "revenue": "xero_profit_and_loss_revenue", "costs": "xero_profit_and_loss_monthly_costs",
    }:
        return None
    maximum = max(totals.values())
    return {key: float(amount / maximum) if maximum else 0.0 for key, amount in totals.items()}


def shared_update(value: dict, *, metric_items=None) -> dict:
    """Project existing publications without modifying private frozen evidence."""
    result = {key: copy.deepcopy(item) for key, item in value.items() if key in SHARED_FIELDS}
    result["financialChart"] = financial_chart(value, respect_selection=True, metric_items=metric_items)
    result["metrics"] = {}
    result["metricEvidence"] = {}
    result["displayConfig"] = {"snippetMetricKeys": [], "fullMetricKeys": []}
    metric_keys = financial_metric_keys(metric_items)
    metric_keys.update(financial_metric_keys([dict(item, key=key) for key, item in (value.get("metricEvidence") or {}).items() if isinstance(item, dict)]))
    for key in NARRATIVE_FIELDS:
        result[key] = shared_narrative(result.get(key), metric_keys=metric_keys)
    return result
