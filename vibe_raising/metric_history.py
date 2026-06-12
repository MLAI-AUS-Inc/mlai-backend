"""Numeric metric history derived from monthly update memos.

The series produced here must always agree with the metric strings each
month's update displays, so extraction mirrors ``_extract_metrics`` in
``vibe_raising.views`` (same key normalization, same value precedence,
last writer wins within a month).
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Tuple

from startup_updates.metric_catalog import (
    startup_update_metric_key,
    startup_update_metric_label,
)


DEFAULT_METRIC_HISTORY_MONTHS = 24

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_MAGNITUDE_SUFFIXES = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "b": 1_000_000_000.0,
}


def parse_metric_number(value_number: Any, value_text: Any) -> Optional[float]:
    """Best-effort numeric value for a kpi_snapshot item.

    Prefers the structured ``value_number`` (often a stringified Decimal from
    Xero-backed observations); otherwise parses the display string. Returns
    None when no number can be extracted.
    """
    if value_number is not None:
        try:
            return float(Decimal(str(value_number).strip()))
        except (InvalidOperation, ValueError, TypeError):
            pass

    text = str(value_text or "").strip()
    if not text:
        return None

    match = _NUMBER_RE.search(text)
    if not match:
        return None

    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None

    # Magnitude suffix only when it sits immediately after the number and is
    # not the start of a longer word ("$50k" yes, "18 months" no).
    suffix_index = match.end()
    if suffix_index < len(text):
        suffix = text[suffix_index].lower()
        next_index = suffix_index + 1
        is_word_end = next_index >= len(text) or not text[next_index].isalpha()
        if suffix in _MAGNITUDE_SUFFIXES and is_word_end:
            value *= _MAGNITUDE_SUFFIXES[suffix]

    if value > 0 and _has_leading_negation(text, match):
        value = -value

    return value


def _has_leading_negation(text: str, match: "re.Match") -> bool:
    """Detect negatives the number regex can't capture directly: a minus sign
    or accounting-style paren separated from the digits only by currency
    symbols/whitespace ("-$4,200", "AUD -1,200", "(5,000)")."""
    prefix = text[: match.start()]
    minus_index = prefix.rfind("-")
    if minus_index >= 0 and not any(ch.isalnum() for ch in prefix[minus_index + 1:]):
        return True
    open_paren = prefix.rfind("(")
    if (
        open_paren >= 0
        and not any(ch.isalnum() for ch in prefix[open_paren + 1:])
        and text.find(")", match.end()) != -1
    ):
        return True
    return False


def _metric_key_for_item(item: dict) -> Optional[str]:
    metric_key = startup_update_metric_key(item.get("metric_key"))
    if metric_key:
        return metric_key
    return startup_update_metric_key(
        item.get("label") or item.get("name") or item.get("metric_name")
    )


def build_metric_history(
    month_memo_pairs: Iterable[Tuple[date, Optional[dict]]],
    max_months: int = DEFAULT_METRIC_HISTORY_MONTHS,
) -> dict:
    """Build per-metric time series from (month, merged structured_memo) pairs.

    Returns ``{metricKey: {metricKey, label, unit, points: [{month, value,
    valueText}]}}`` with points ascending by month, capped to the most recent
    ``max_months`` months. Values that don't parse to a number are skipped;
    metrics with no numeric points are omitted entirely.
    """
    pairs = sorted(
        (
            (month, memo)
            for month, memo in month_memo_pairs
            if isinstance(month, date)
        ),
        key=lambda pair: pair[0],
    )
    if max_months and len(pairs) > max_months:
        pairs = pairs[-max_months:]

    history: dict = {}
    for month, memo in pairs:
        snapshot = (memo or {}).get("kpi_snapshot") or []
        month_points: dict = {}
        for item in snapshot:
            if not isinstance(item, dict):
                continue

            metric_key = _metric_key_for_item(item)
            if not metric_key:
                continue

            raw_value = (
                item.get("value")
                or item.get("value_text")
                or item.get("value_number")
            )
            value_text = str(raw_value).strip() if raw_value is not None else ""
            if not value_text:
                continue

            value = parse_metric_number(item.get("value_number"), value_text)
            if value is None:
                continue

            month_points[metric_key] = {
                "value": value,
                "valueText": value_text,
                "unit": str(item.get("unit") or "").strip(),
            }

        for metric_key, point in month_points.items():
            series = history.setdefault(
                metric_key,
                {
                    "metricKey": metric_key,
                    "label": startup_update_metric_label(metric_key),
                    "unit": "",
                    "points": [],
                },
            )
            if not series["unit"] and point["unit"]:
                series["unit"] = point["unit"]
            series["points"].append(
                {
                    "month": month.isoformat(),
                    "value": point["value"],
                    "valueText": point["valueText"],
                }
            )

    return history
