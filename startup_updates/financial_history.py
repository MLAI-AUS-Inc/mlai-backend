"""Opt-in, immutable six-month financial disclosures for a Pulse revision.

Only the save path materializes values, from its server-owned evidence snapshot.
Readers project that saved disclosure and never query observations or providers.
"""
from __future__ import annotations

import calendar
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from startup_updates.disclosure import CURRENCIES
from startup_updates.evidence_contract import content_hash


class FinancialHistoryError(ValueError):
    """A disclosure cannot safely use the selected financial evidence."""


def validate_selection(value):
    """Require an explicit inclusion and separate dollar-amount decision."""
    if not isinstance(value, dict) or set(value) - {"enabled", "shareAmounts", "refresh"}:
        raise FinancialHistoryError("Choose whether to include financial history and share amounts.")
    if any(type(value.get(key)) is not bool for key in ("enabled", "shareAmounts")):
        raise FinancialHistoryError("Financial history choices must be true or false.")
    if "refresh" in value and type(value["refresh"]) is not bool:
        raise FinancialHistoryError("The source refresh choice must be true or false.")
    if not value["enabled"] and (value["shareAmounts"] or value.get("refresh")):
        raise FinancialHistoryError("Include financial history before sharing amounts or refreshing it.")
    return {"enabled": value["enabled"], "shareAmounts": value["shareAmounts"],
        "refresh": value.get("refresh", False)}


def _month_shift(month, offset):
    index = month.year * 12 + month.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _month_end(month):
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        converted = float(number)
        return converted if number.is_finite() and abs(converted) != float("inf") else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _coverage(period, month):
    if not isinstance(period, dict) or period.get("month") != month.isoformat():
        raise FinancialHistoryError("Refresh this update's financial evidence for its reporting month.")
    try:
        cutoff = datetime.fromisoformat(period["cutoff"].replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("A cutoff needs a timezone")
        # Evidence cutoffs are exclusive; a month-end cutoff is next month's midnight.
        final = (cutoff.astimezone(ZoneInfo(period["timezone"])) - timedelta(microseconds=1)).date()
    except (KeyError, TypeError, ValueError) as exc:
        raise FinancialHistoryError("Refresh this update's financial evidence to confirm its coverage.") from exc
    if final < month:
        raise FinancialHistoryError("There is no financial coverage in this reporting month yet.")
    return min(final, _month_end(month))


def _entry(value, evidence, *, field, month, currency, coverage, reports, timezone_name):
    """Use canonical verified accounting totals with the same explicit scope."""
    number = _number(value)
    if number is None or not isinstance(evidence, dict) or evidence.get("unit") != currency:
        return None
    if evidence.get("source_provider") != "xero" or evidence.get("accounting_basis") != "accrual":
        return None
    account = evidence.get("connection_id")
    if not isinstance(account, (str, int)) or isinstance(account, bool) or not str(account):
        return None
    report_hash = evidence.get("report_hash")
    report = reports.get(report_hash) if isinstance(report_hash, str) else None
    if not isinstance(report, dict) or content_hash(report) != report_hash:
        return None
    expected_source = {"revenue": "xero_profit_and_loss_revenue", "costs": "xero_profit_and_loss_monthly_costs"}[field]
    if evidence.get("source_metric") != expected_source:
        return None
    # A hash proves integrity, not that a displayed number is the correct total.
    # Reuse the connector's canonical report parser, including direct costs.
    from startup_updates.services import _parse_xero_profit_and_loss_report
    parsed = _parse_xero_profit_and_loss_report(report)
    aggregate = parsed.get("revenue" if field == "revenue" else "monthly_costs")
    if not aggregate or Decimal(str(value)) != aggregate.get("amount"):
        return None
    try:
        start = date.fromisoformat(evidence.get("report_start_date", ""))
        end = date.fromisoformat(evidence.get("report_end_date", ""))
    except (TypeError, ValueError):
        return None
    if start != month or not month <= end <= min(_month_end(month), coverage):
        return None
    scope = ("xero", currency, "accrual", str(account), evidence.get("timezone") or timezone_name)
    return {"value": number, "scope": scope, "end": end}


def build_snapshot(payload, month, *, captured_at):
    """Freeze exactly six calendar slots from one immutable evidence payload."""
    coverage = _coverage(payload.get("period"), month)
    charts = payload.get("charts") or {}
    currency = charts.get("currency")
    if charts.get("schema_version") != 2 or currency not in CURRENCIES or charts.get("target_month") != month.isoformat():
        raise FinancialHistoryError("Refresh verified accounting evidence before including financial history.")
    rows = charts.get("performance") or []
    by_month = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("month") not in by_month:
            by_month[row.get("month")] = copy.deepcopy(row)
    # Capture can override its final chart point with private metrics. Those
    # values must retain the same source quality and provenance as the cards.
    current_metrics = {item.get("key"): item for item in payload.get("metrics", []) if isinstance(item, dict)}
    target = by_month.setdefault(month.isoformat(), {"month": month.isoformat()})
    reports = dict(charts.get("source_reports") or {})
    for key, field in (("revenue", "income"), ("monthlyCosts", "expenses")):
        metric = current_metrics.get(key)
        if metric is not None:
            metadata = metric.get("metadata") or {}
            target[field] = metric.get("value") if metric.get("quality") in {"source_reported", "verified"} else None
            target.setdefault("metric_evidence", {})[field] = {
                **metadata, "unit": metric.get("unit"), "source_provider": metric.get("source_provider"),
            }
            if metadata.get("report_hash") and isinstance(metadata.get("report_payload"), dict):
                reports[metadata["report_hash"]] = metadata["report_payload"]
    candidates = []
    for offset in range(-5, 1):
        point_month = _month_shift(month, offset)
        row = by_month.get(point_month.isoformat(), {})
        evidence = row.get("metric_evidence") or {}
        entries = {name: _entry(row.get(field), evidence.get(field), month=point_month,
            field=name, currency=currency, coverage=coverage, reports=reports, timezone_name=payload["period"]["timezone"])
            for name, field in (("revenue", "income"), ("costs", "expenses"))}
        available = [entry for entry in entries.values() if entry is not None]
        # A same-month pair with different account/basis/coverage is ambiguous.
        if len(available) == 2 and (available[0]["scope"] != available[1]["scope"] or available[0]["end"] != available[1]["end"]):
            entries = {"revenue": None, "costs": None}
        candidates.append((point_month, entries))
    scope = next((entry["scope"] for _, entries in reversed(candidates)
        for entry in entries.values() if entry is not None), None)
    points = []
    for point_month, entries in candidates:
        matching = {key: entry if entry and entry["scope"] == scope else None for key, entry in entries.items()}
        ends = [entry["end"] for entry in matching.values() if entry is not None]
        end = min(ends) if ends else None
        points.append({"month": point_month.isoformat(),
            **{key: entry["value"] if entry else None for key, entry in matching.items()},
            "partial": bool(end and end < _month_end(point_month)),
            "periodEnd": end.isoformat() if end else None})
    if not any(point[key] is not None for point in points for key in ("revenue", "costs")):
        raise FinancialHistoryError("No compatible verified financial figures are available. Refresh the update's accounting evidence, then review it again.")
    # A cached source may end earlier than a newly captured reporting window.
    # Label the actual final-month evidence, not today's refresh timestamp.
    if points[-1]["periodEnd"]:
        coverage = min(coverage, date.fromisoformat(points[-1]["periodEnd"]))
    return {"schemaVersion": 1, "reportingMonth": month.isoformat(),
        "rangeStart": _month_shift(month, -5).isoformat(), "rangeEnd": _month_end(month).isoformat(),
        "cutoff": coverage.isoformat(), "capturedAt": captured_at, "currency": currency, "points": points}


def history_for_revision(draft, memo, current, snapshot, *, captured_at):
    """Ignore numerical memo payloads; retain snapshots until an explicit refresh."""
    prior = (current.structured_memo if current else {}).get("financial_history")
    requested = memo.pop("_financial_history_selection", None)
    memo.pop("financial_history", None)
    if requested is None:
        return copy.deepcopy(prior)
    selection = validate_selection(requested)
    frozen = copy.deepcopy(prior.get("snapshot")) if isinstance(prior, dict) else None
    if selection["enabled"] and (selection["refresh"] or not frozen or frozen.get("reportingMonth") != draft.month.isoformat()):
        frozen = build_snapshot(snapshot.payload, draft.month, captured_at=captured_at)
    return {"enabled": selection["enabled"], "shareAmounts": selection["shareAmounts"], "snapshot": frozen}


def generated_history_selection(memo, run_request):
    """Only the founder's pinned run choice may opt generated prose into sharing."""
    result = copy.deepcopy(memo)
    for field in ("financial_history", "_financial_history_selection", "financialHistory", "financialHistorySelection"):
        result.pop(field, None)
    if "financial_history_selection" in (run_request or {}):
        result["_financial_history_selection"] = validate_selection(run_request["financial_history_selection"])
    return result


def history_export_memo(memo):
    """Apply shared privacy to new history exports without rewriting old posts."""
    if not isinstance(memo.get("financial_history"), dict) or memo.get("_audience_visibility") not in (["community"], ["public"]):
        return memo
    from startup_updates.disclosure import financial_metric_keys, has_financial_amount, shared_narrative
    result = copy.deepcopy(memo)
    metrics = result.get("kpi_snapshot") or []
    keys = financial_metric_keys(metrics)
    def redact(item):
        if isinstance(item, str):
            return shared_narrative(item, metric_keys=keys)
        if isinstance(item, list):
            return [safe for value in item if (safe := redact(value))]
        if isinstance(item, dict):
            # Labels supply the meaning of otherwise ambiguous numeric values.
            text = " ".join(str(item.get(key) or "") for key in ("label", "name", "value", "text", "summary"))
            return shared_narrative(text, metric_keys=keys)
        return None
    result["kpi_snapshot"] = [item for item in metrics if isinstance(item, dict)
        and item.get("metric_key") not in keys
        and not has_financial_amount(" ".join(str(item.get(key) or "") for key in ("label", "value", "unit")), metric_keys=keys)]
    for key in ("title", "startupOverview", "topline", "summary", "financial_performance", "highlights", "operations", "lowlights", "learnings", "next_30_days", "asks"):
        if key in result:
            result[key] = redact(result[key])
    # Generic progress charts are not in the community/public allowlist. Their
    # financial values must not bypass the dedicated, separately opted-in chart.
    result.pop("progress_charts", None)
    return result


def history_selection(history):
    """Return persisted founder choices without internal snapshot or refresh flags."""
    if not isinstance(history, dict) or type(history.get("enabled")) is not bool or type(history.get("shareAmounts")) is not bool:
        return None
    return {"enabled": history["enabled"], "shareAmounts": history["shareAmounts"] if history["enabled"] else False}


def history_payload(history):
    """Allowlist the approved disclosure; relative mode reveals no raw amounts."""
    selection = history_selection(history)
    if not selection or not selection["enabled"]:
        return None
    frozen = history.get("snapshot")
    if not isinstance(frozen, dict) or frozen.get("schemaVersion") != 1:
        return None
    try:
        month = date.fromisoformat(frozen["reportingMonth"])
        cutoff = date.fromisoformat(frozen["cutoff"])
        captured = datetime.fromisoformat(frozen["capturedAt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if (month.day != 1 or not month <= cutoff <= _month_end(month) or captured.tzinfo is None
            or frozen.get("currency") not in CURRENCIES or frozen.get("rangeStart") != _month_shift(month, -5).isoformat()
            or frozen.get("rangeEnd") != _month_end(month).isoformat()):
        return None
    rows = frozen.get("points")
    if not isinstance(rows, list) or len(rows) != 6:
        return None
    points = []
    for index, row in enumerate(rows):
        point_month = _month_shift(month, index - 5)
        if not isinstance(row, dict) or row.get("month") != point_month.isoformat() or type(row.get("partial")) is not bool:
            return None
        values = {key: _number(row.get(key)) for key in ("revenue", "costs")}
        if any(row.get(key) is not None and values[key] is None for key in values):
            return None
        try:
            end = date.fromisoformat(row["periodEnd"]) if row.get("periodEnd") else None
        except (TypeError, ValueError):
            return None
        available = any(value is not None for value in values.values())
        if (available and (end is None or not point_month <= end <= min(_month_end(point_month), cutoff))) or (not available and end is not None):
            return None
        if row["partial"] != bool(end and end < _month_end(point_month)):
            return None
        points.append({"month": point_month.isoformat(), **values,
            "partial": row["partial"], "periodEnd": end.isoformat() if end else None})
    maximum = max((abs(point[key]) for point in points for key in ("revenue", "costs") if point[key] is not None), default=0)
    if not any(point[key] is not None for point in points for key in ("revenue", "costs")):
        return None
    if not selection["shareAmounts"]:
        for point in points:
            for key in ("revenue", "costs"):
                if point[key] is not None:
                    point[key] = point[key] / maximum * 100 if maximum else 0.0
    return {"schemaVersion": 1, "reportingMonth": month.isoformat(),
        "rangeStart": frozen["rangeStart"], "rangeEnd": frozen["rangeEnd"], "cutoff": cutoff.isoformat(),
        "capturedAt": frozen["capturedAt"], "shareAmounts": selection["shareAmounts"],
        "currency": frozen["currency"] if selection["shareAmounts"] else None, "points": points}


def history_chart_context(chart):
    """Build an escaped, script-free public chart/table from a disclosed DTO."""
    if not chart:
        return None
    month = date.fromisoformat(chart["reportingMonth"])
    values = [point[key] for point in chart["points"] for key in ("revenue", "costs") if point[key] is not None]
    low, high = min([0, *values]), max([0, *values])
    if low == high:
        high = low + 1
    top, bottom = 28, 210
    def position(value):
        return round(bottom - (value - low) / (high - low) * (bottom - top), 2)
    def label(value):
        return f"{value:,.2f}" if chart["shareAmounts"] else f"{value:,.1f}"
    def axis_label(value):
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:,.1f}M"
        if abs(value) >= 1_000:
            return f"{value / 1_000:,.1f}K"
        return f"{value:,.0f}"
    points = []
    for index, point in enumerate(chart["points"]):
        point_month = date.fromisoformat(point["month"])
        points.append({**point, "x": 75 + index * 100, "label": point_month.strftime("%b"),
            "monthLabel": point_month.strftime("%B %Y"), "highlighted": index == 5,
            "revenueY": position(point["revenue"]) if point["revenue"] is not None else None,
            "costsY": position(point["costs"]) if point["costs"] is not None else None,
            "revenueLabel": label(point["revenue"]) if point["revenue"] is not None else "Unavailable",
            "costsLabel": label(point["costs"]) if point["costs"] is not None else "Unavailable"})
    segments = []
    for field in ("revenue", "costs"):
        for index in range(1, len(points)):
            previous, point = points[index - 1], points[index]
            if previous[field] is not None and point[field] is not None:
                segments.append({"field": field,
                    "coordinates": f"{previous['x']},{previous[field + 'Y']} {point['x']},{point[field + 'Y']}",
                    "partial": previous["partial"] or point["partial"]})
    return {"reportingMonth": month.strftime("%B %Y"), "points": points, "segments": segments,
        "unit": chart["currency"] if chart["shareAmounts"] else "Relative scale · peak = 100",
        "cutoff": chart["cutoff"], "partial": chart["cutoff"] < chart["rangeEnd"],
        "ticks": [{"y": position(low + (high - low) * index / 4),
            "top": round(position(low + (high - low) * index / 4) / 220 * 100, 2),
            "label": axis_label(low + (high - low) * index / 4)} for index in range(5)]}
