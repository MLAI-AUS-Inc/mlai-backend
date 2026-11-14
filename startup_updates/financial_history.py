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


# Each chart field: (accounting basis, source metric, row evidence key).
_FIELDS = {
    "revenue": ("accrual", "xero_profit_and_loss_revenue"),
    "costs": ("accrual", "xero_profit_and_loss_monthly_costs"),
    "cashIn": ("cash", "xero_bank_summary_cash_in"),
    "cashOut": ("cash", "xero_bank_summary_cash_out"),
}


def _verified_amount(field, report):
    """Recompute a field from its source report with the canonical parsers."""
    from startup_updates.services import _parse_xero_bank_summary_report, _parse_xero_profit_and_loss_report
    if field in {"cashIn", "cashOut"}:
        return _parse_xero_bank_summary_report(report).get("cash_in" if field == "cashIn" else "cash_out")
    aggregate = _parse_xero_profit_and_loss_report(report).get("revenue" if field == "revenue" else "monthly_costs")
    return aggregate.get("amount") if aggregate else None


def _entry(value, evidence, *, field, month, currency, coverage, reports, timezone_name):
    """Use canonical verified accounting totals with the same explicit scope."""
    number = _number(value)
    if number is None or not isinstance(evidence, dict) or evidence.get("unit") != currency:
        return None
    basis, expected_source = _FIELDS[field]
    if evidence.get("source_provider") != "xero" or evidence.get("accounting_basis") != basis:
        return None
    account = evidence.get("connection_id")
    if not isinstance(account, (str, int)) or isinstance(account, bool) or not str(account):
        return None
    report_hash = evidence.get("report_hash")
    report = reports.get(report_hash) if isinstance(report_hash, str) else None
    if not isinstance(report, dict) or content_hash(report) != report_hash:
        return None
    if evidence.get("source_metric") != expected_source:
        return None
    # A hash proves integrity, not that a displayed number is the correct total.
    # Reuse the connector's canonical report parsers, including direct costs.
    amount = _verified_amount(field, report)
    if amount is None or Decimal(str(value)) != amount:
        return None
    try:
        start = date.fromisoformat(evidence.get("report_start_date", ""))
        end = date.fromisoformat(evidence.get("report_end_date", ""))
    except (TypeError, ValueError):
        return None
    if start != month or not month <= end <= min(_month_end(month), coverage):
        return None
    scope = ("xero", currency, basis, str(account), evidence.get("timezone") or timezone_name)
    return {"value": number, "scope": scope, "end": end}


def _series(rows, month, *, fields, coverage, currency, reports, timezone_name):
    """Six verified calendar slots for one pair of fields, or None when empty."""
    by_month = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("month") not in by_month:
            by_month[row.get("month")] = row
    candidates = []
    for offset in range(-5, 1):
        point_month = _month_shift(month, offset)
        row = by_month.get(point_month.isoformat(), {})
        evidence = row.get("metric_evidence") or {}
        entries = {name: _entry(row.get(column), evidence.get(column), month=point_month,
            field=name, currency=currency, coverage=coverage, reports=reports, timezone_name=timezone_name)
            for name, column in fields}
        available = [entry for entry in entries.values() if entry is not None]
        # A same-month pair with different account/basis/coverage is ambiguous.
        if len(available) == 2 and (available[0]["scope"] != available[1]["scope"] or available[0]["end"] != available[1]["end"]):
            entries = {name: None for name, _ in fields}
        candidates.append((point_month, entries))
    scope = next((entry["scope"] for _, entries in reversed(candidates)
        for entry in entries.values() if entry is not None), None)
    if scope is None:
        return None
    points = []
    for point_month, entries in candidates:
        matching = {key: entry if entry and entry["scope"] == scope else None for key, entry in entries.items()}
        ends = [entry["end"] for entry in matching.values() if entry is not None]
        end = min(ends) if ends else None
        points.append({"month": point_month.isoformat(),
            **{key: entry["value"] if entry else None for key, entry in matching.items()},
            "partial": bool(end and end < _month_end(point_month)),
            "periodEnd": end.isoformat() if end else None})
    return points


_STRIPE_KEYS = ("sales", "refunds", "fees", "payouts")


def _stripe_series(stripe, month, *, coverage, currency):
    """Monthly Stripe balance totals collected by the same refresh, or None."""
    if not isinstance(stripe, dict) or stripe.get("unit") != currency or not isinstance(stripe.get("months"), dict):
        return None
    try:
        stripe_end = min(date.fromisoformat(stripe["coverage_end"]), coverage)
    except (KeyError, TypeError, ValueError):
        return None
    points = []
    for offset in range(-5, 1):
        point_month = _month_shift(month, offset)
        values = stripe["months"].get(point_month.isoformat())
        numbers = {key: _number((values or {}).get(key)) for key in _STRIPE_KEYS} if isinstance(values, dict) else None
        if not numbers or any(value is None for value in numbers.values()) or stripe_end < point_month:
            points.append({"month": point_month.isoformat(), **{key: None for key in _STRIPE_KEYS},
                "partial": False, "periodEnd": None})
            continue
        end = min(_month_end(point_month), stripe_end)
        points.append({"month": point_month.isoformat(), **numbers,
            "partial": end < _month_end(point_month), "periodEnd": end.isoformat()})
    return points if any(point["periodEnd"] for point in points) else None


def build_snapshot(payload, month, *, captured_at):
    """Freeze six calendar slots for every basis from one immutable evidence payload.

    Accrual (Profit and Loss), cash (Bank Summary) and Stripe totals are stored
    side by side; which one a reader sees is chosen at read time from the
    startup's reporting settings, so changing them needs no provider call.
    """
    coverage = _coverage(payload.get("period"), month)
    charts = payload.get("charts") or {}
    currency = charts.get("currency")
    if charts.get("schema_version") != 2 or currency not in CURRENCIES or charts.get("target_month") != month.isoformat():
        raise FinancialHistoryError("Refresh verified accounting evidence before including financial history.")
    rows = copy.deepcopy(charts.get("performance") or [])
    rows = rows if isinstance(rows, list) else []
    by_month = {}
    for row in rows:
        if isinstance(row, dict) and row.get("month") not in by_month:
            by_month[row.get("month")] = row
    # Capture can override its final chart point with private metrics. Those
    # values must retain the same source quality and provenance as the cards.
    current_metrics = {item.get("key"): item for item in payload.get("metrics", []) if isinstance(item, dict)}
    target = by_month.get(month.isoformat())
    if target is None:
        target = {"month": month.isoformat()}
        rows.append(target)
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
    common = {"coverage": coverage, "currency": currency, "reports": reports, "timezone_name": payload["period"]["timezone"]}
    accrual = _series(rows, month, fields=(("revenue", "income"), ("costs", "expenses")), **common)
    cash = _series(charts.get("cash"), month, fields=(("cashIn", "cash_in"), ("cashOut", "cash_out")), **common)
    stripe = _stripe_series(charts.get("stripe"), month, coverage=coverage, currency=currency)
    if accrual is None and cash is None and stripe is None:
        raise FinancialHistoryError("No compatible verified financial figures are available. Refresh the update's accounting evidence, then review it again.")
    if accrual is None:
        accrual = [{"month": _month_shift(month, offset).isoformat(), "revenue": None, "costs": None,
            "partial": False, "periodEnd": None} for offset in range(-5, 1)]
    # A cached source may end earlier than a newly captured reporting window.
    # Label the actual final-month evidence, not today's refresh timestamp.
    ends = [date.fromisoformat(series[-1]["periodEnd"]) for series in (accrual, cash, stripe)
        if series and series[-1]["periodEnd"]]
    if ends:
        coverage = min(coverage, max(ends))
    unavailable = charts.get("cash_unavailable")
    return {"schemaVersion": 2, "reportingMonth": month.isoformat(),
        "rangeStart": _month_shift(month, -5).isoformat(), "rangeEnd": _month_end(month).isoformat(),
        "cutoff": coverage.isoformat(), "capturedAt": captured_at, "currency": currency, "points": accrual,
        "cash": {"points": cash} if cash else None,
        "cashUnavailable": unavailable if unavailable in {"scope"} else None,
        "stripe": {"points": stripe} if stripe else None}


CASH_SUPPLEMENT_KIND = "pulse_cash_supplement"
_LEGACY_CACHE: dict = {}
_LEGACY_CACHE_LIMIT = 512


def recompute_report_values(payload):
    """Recalculate stored Xero totals from their own reports with today's parsers.

    Charts saved before Other Income and every expense section were counted
    hold totals the current parser no longer produces. The reports themselves
    are unchanged and hash-verified, so their totals can be recomputed without
    any provider call.
    """
    result = copy.deepcopy(payload)
    charts = result.get("charts") if isinstance(result.get("charts"), dict) else {}
    reports = charts.get("source_reports") or {}
    for row in charts.get("performance") or []:
        if not isinstance(row, dict):
            continue
        for column, field in (("income", "revenue"), ("expenses", "costs")):
            evidence = (row.get("metric_evidence") or {}).get(column) or {}
            report = reports.get(evidence.get("report_hash"))
            if isinstance(report, dict) and content_hash(report) == evidence.get("report_hash"):
                amount = _verified_amount(field, report)
                row[column] = str(amount) if amount is not None else None
    for metric in result.get("metrics") or []:
        field = {"revenue": "revenue", "monthlyCosts": "costs"}.get((metric or {}).get("key"))
        metadata = (metric or {}).get("metadata") or {}
        report = metadata.get("report_payload")
        if field and isinstance(report, dict) and content_hash(report) == metadata.get("report_hash"):
            amount = _verified_amount(field, report)
            metric["value"] = str(amount) if amount is not None else None
    return result


def _cash_supplement(organization_id, month, source_hash):
    """Cash rows fetched later for a chart saved before cash was collected."""
    from startup_updates.models import MonthlyEvidenceSnapshot
    return MonthlyEvidenceSnapshot.objects.filter(organization_id=organization_id, month=month,
        payload__kind=CASH_SUPPLEMENT_KIND, payload__source_snapshot_hash=source_hash).order_by("-pk").first()


def upgrade_legacy_history(history, *, organization_id, month):
    """Read a pre-cash chart as today's chart, from its own saved evidence.

    Revisions stay immutable: the upgrade is recomputed at read time from the
    chart's source snapshot plus any backfilled Bank Summary supplement, and is
    never written back. Anything that cannot be verified keeps the saved chart.
    """
    if not isinstance(history, dict):
        return history
    frozen, source = history.get("snapshot"), history.get("_source")
    if (not isinstance(frozen, dict) or frozen.get("schemaVersion") != 1 or not isinstance(source, dict)
            or type(source.get("snapshotId")) is not int or not isinstance(source.get("snapshotHash"), str)):
        return history
    from startup_updates.models import MonthlyEvidenceSnapshot
    supplement = _cash_supplement(organization_id, month, source["snapshotHash"])
    key = (organization_id, source["snapshotId"], source["snapshotHash"], frozen.get("capturedAt"),
        supplement.content_hash if supplement else None)
    if key not in _LEGACY_CACHE:
        rebuilt = None
        snapshot = MonthlyEvidenceSnapshot.objects.filter(pk=source["snapshotId"], content_hash=source["snapshotHash"],
            organization_id=organization_id, month=month).first()
        payload = snapshot.payload if snapshot else None
        if isinstance(payload, dict) and isinstance(frozen.get("capturedAt"), str):
            payload = recompute_report_values(payload)
            if supplement and isinstance(supplement.payload.get("charts"), dict):
                charts = payload.setdefault("charts", {})
                extra = supplement.payload["charts"]
                charts["cash"] = copy.deepcopy(extra.get("cash") or [])
                charts["source_reports"] = {**(charts.get("source_reports") or {}),
                    **copy.deepcopy(extra.get("source_reports") or {})}
            try:
                rebuilt = build_snapshot(payload, month, captured_at=frozen["capturedAt"])
            except (FinancialHistoryError, KeyError, TypeError, ValueError):
                rebuilt = None
        if len(_LEGACY_CACHE) >= _LEGACY_CACHE_LIMIT:
            _LEGACY_CACHE.pop(next(iter(_LEGACY_CACHE)))
        _LEGACY_CACHE[key] = rebuilt
    rebuilt = _LEGACY_CACHE[key]
    if rebuilt is None:
        return history
    return {**history, "snapshot": copy.deepcopy(rebuilt)}


def reader_history(history, *, organization_id, month, display=None):
    """What every reader of a saved update projects: upgraded, with current settings."""
    if not isinstance(history, dict):
        return history
    upgraded = upgrade_legacy_history(history, organization_id=organization_id, month=month)
    return {**upgraded, "display": normalize_display(display if display is not None else financial_display(organization_id))}


def history_for_revision(draft, memo, current, snapshot, *, captured_at, financial_history_snapshot=None):
    """Ignore numerical memo payloads; retain snapshots until an explicit refresh."""
    prior = copy.deepcopy((current.structured_memo if current else {}).get("financial_history"))
    if isinstance(prior, dict):
        # Read-time settings are never persisted with a revision.
        prior.pop("display", None)
    requested = memo.pop("_financial_history_selection", None)
    memo.pop("financial_history", None)
    if requested is None and financial_history_snapshot is not None:
        raise FinancialHistoryError("Choose how to include the refreshed financial chart before saving.")
    if requested is None:
        return copy.deepcopy(prior)
    selection = validate_selection(requested)
    frozen = copy.deepcopy(prior.get("snapshot")) if isinstance(prior, dict) else None
    source = copy.deepcopy(prior.get("_source")) if isinstance(prior, dict) else None
    if financial_history_snapshot is not None:
        # A provider refresh has separate evidence from the written update.
        # Use its original capture timestamp so save matches the exact preview.
        captured = financial_history_snapshot.payload.get("financial_refresh", {}).get("capturedAt", captured_at)
        frozen = build_snapshot(financial_history_snapshot.payload, draft.month, captured_at=captured)
        source = {"snapshotId": financial_history_snapshot.pk, "snapshotHash": financial_history_snapshot.content_hash}
    elif selection["enabled"] and (selection["refresh"] or not frozen or frozen.get("reportingMonth") != draft.month.isoformat()):
        frozen = build_snapshot(snapshot.payload, draft.month, captured_at=captured_at)
        source = {"snapshotId": snapshot.pk, "snapshotHash": snapshot.content_hash}
    result = {"enabled": selection["enabled"], "shareAmounts": selection["shareAmounts"], "snapshot": frozen}
    if source is not None:
        result["_source"] = source
    return result


def history_review_evidence(revision):
    """Resolve the chart's original immutable input within its startup and month."""
    from startup_updates.models import MonthlyEvidenceSnapshot
    history = (revision.structured_memo or {}).get("financial_history")
    source = history.get("_source") if isinstance(history, dict) else None
    if (not isinstance(source, dict) or type(source.get("snapshotId")) is not int
            or not isinstance(source.get("snapshotHash"), str)):
        return None
    # A later narrative revision may have fresh evidence while retaining the
    # approved chart. Never substitute that newer evidence for the chart's input.
    snapshot = MonthlyEvidenceSnapshot.objects.filter(
        pk=source["snapshotId"], content_hash=source["snapshotHash"],
        organization_id=revision.snapshot.organization_id, month=revision.snapshot.month,
    ).first()
    if snapshot is None:
        return None
    payload = snapshot.payload
    if not isinstance(payload, dict):
        return None
    # Captured snapshots carry their own hash outside the hashed body; older
    # snapshots may instead hash the complete payload. Validate either encoding.
    unhashed = {key: value for key, value in payload.items() if key != "hash"}
    if content_hash(payload) != snapshot.content_hash and not (
        payload.get("hash") == snapshot.content_hash and content_hash(unhashed) == snapshot.content_hash
    ):
        return None
    return {"snapshotId": snapshot.pk, "snapshotHash": snapshot.content_hash,
        **{key: copy.deepcopy(payload.get(key)) for key in ("period", "metrics", "charts")}}


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


def history_owner_preview(history, *, share_amounts=True):
    """Project retained chart data for its owner, independently of disclosure."""
    if not isinstance(history, dict):
        return None
    return history_payload({**history, "enabled": True, "shareAmounts": share_amounts})


BASES = ("cash", "accrual")
DISPLAY_KEY = "financial_display"
_SERIES_FIELDS = {"accrual": ("revenue", "costs"), "cash": ("cashIn", "cashOut"), "stripe": _STRIPE_KEYS}


def normalize_display(value):
    """Founder reporting choices for charts; cash basis and Stripe-in-Xero by default."""
    value = value if isinstance(value, dict) else {}
    return {"basis": value["basis"] if value.get("basis") in BASES else "cash",
        "stripeInXero": value["stripeInXero"] if type(value.get("stripeInXero")) is bool else True}


def financial_display(organization_id):
    """Read the startup's current chart settings; every saved chart follows them."""
    from startup_updates.models import StartupProfile
    config = StartupProfile.objects.filter(organization_id=organization_id).values_list(
        "progress_configuration", flat=True).first()
    return normalize_display((config or {}).get(DISPLAY_KEY))


def with_display(memo, organization_id, *, display=None):
    """Attach read-time chart settings to a memo copy; never persisted."""
    history = memo.get("financial_history") if isinstance(memo, dict) else None
    if isinstance(history, dict):
        memo["financial_history"] = {**history, "display": normalize_display(
            display if display is not None else financial_display(organization_id))}
    return memo


def _validated_points(rows, month, cutoff, keys):
    """Exactly six ordered calendar slots whose coverage agrees with the cutoff."""
    if not isinstance(rows, list) or len(rows) != 6:
        return None
    points = []
    for index, row in enumerate(rows):
        point_month = _month_shift(month, index - 5)
        if not isinstance(row, dict) or row.get("month") != point_month.isoformat() or type(row.get("partial")) is not bool:
            return None
        values = {key: _number(row.get(key)) for key in keys}
        if any(row.get(key) is not None and values[key] is None for key in keys):
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
    return points


def _has_values(points, keys):
    return bool(points) and any(point[key] is not None for point in points for key in keys)


def _display_points(frozen, version, month, cutoff, display):
    """Choose the founder's basis and apply the Stripe merge rule.

    Returns (points, basis, fallback, stripe_mode) or None for a malformed chart.
    Stripe is never added to Xero when its payouts already land in a Xero bank
    account (the default), because Xero then already counts that money.
    """
    accrual = _validated_points(frozen.get("points"), month, cutoff, _SERIES_FIELDS["accrual"])
    if accrual is None:
        return None
    series = {"accrual": accrual}
    for name in ("cash", "stripe"):
        stored = frozen.get(name) if version == 2 else None
        if stored is None:
            series[name] = None
            continue
        if not isinstance(stored, dict):
            return None
        series[name] = _validated_points(stored.get("points"), month, cutoff, _SERIES_FIELDS[name])
        if series[name] is None:
            return None
    basis, fallback = display["basis"], None
    if basis == "cash" and not _has_values(series["cash"], _SERIES_FIELDS["cash"]):
        reason = frozen.get("cashUnavailable") if version == 2 else None
        fallback = reason if reason in {"scope"} else ("legacy" if version == 1 else "unavailable")
        basis = "accrual"
    primary = series[basis]
    money_in, money_out = _SERIES_FIELDS[basis]
    stripe = series["stripe"] if _has_values(series["stripe"], _STRIPE_KEYS) else None
    xero_present = _has_values(primary, (money_in, money_out))
    if stripe is not None and not xero_present and not _has_values(series["accrual"], _SERIES_FIELDS["accrual"]):
        mode = "only"
    elif stripe is not None and xero_present:
        mode = "withinXero" if display["stripeInXero"] else "added"
    else:
        mode, stripe = None, None
    if mode == "only":
        # Stripe alone is not a fallback for a missing Xero basis: it follows the
        # founder's basis directly (payouts for cash, net sales for accrual).
        basis, fallback = display["basis"], None
    points = []
    for index, point in enumerate(primary):
        revenue, costs = point[money_in], point[money_out]
        partial, end = point["partial"], point["periodEnd"]
        share = None
        stripe_point = stripe[index] if stripe else None
        if stripe_point and stripe_point["periodEnd"]:
            share = stripe_point["payouts"] if basis == "cash" else stripe_point["sales"] - stripe_point["refunds"]
            if mode == "only":
                # Stripe cannot see the rest of the business, so it never supplies costs.
                revenue, costs = share, None
                partial, end = stripe_point["partial"], stripe_point["periodEnd"]
            elif mode == "added" and revenue is not None:
                revenue += share
                if basis == "accrual" and costs is not None:
                    costs += stripe_point["fees"]
            elif mode == "added":
                share = None
        if revenue is None and costs is None:
            share, end, partial = None, None, False
        points.append({"month": point["month"], "revenue": revenue, "costs": costs,
            "stripe": share, "partial": partial, "periodEnd": end})
    return points, basis, fallback, mode


def history_payload(history):
    """Allowlist the approved disclosure; relative mode reveals no raw amounts."""
    selection = history_selection(history)
    if not selection or not selection["enabled"]:
        return None
    frozen = history.get("snapshot")
    if not isinstance(frozen, dict) or frozen.get("schemaVersion") not in (1, 2):
        return None
    version = frozen["schemaVersion"]
    try:
        month = date.fromisoformat(frozen["reportingMonth"])
        cutoff = date.fromisoformat(frozen["cutoff"])
        captured = datetime.fromisoformat(frozen["capturedAt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (month.day != 1 or not month <= cutoff <= _month_end(month) or captured.tzinfo is None
            or frozen.get("currency") not in CURRENCIES or frozen.get("rangeStart") != _month_shift(month, -5).isoformat()
            or frozen.get("rangeEnd") != _month_end(month).isoformat()):
        return None
    chosen = _display_points(frozen, version, month, cutoff, normalize_display(history.get("display")))
    if chosen is None:
        return None
    points, basis, fallback, stripe_mode = chosen
    if not any(point[key] is not None for point in points for key in ("revenue", "costs")):
        return None
    maximum = max((abs(point[key]) for point in points for key in ("revenue", "costs", "stripe") if point[key] is not None), default=0)
    if not selection["shareAmounts"]:
        for point in points:
            for key in ("revenue", "costs", "stripe"):
                if point[key] is not None:
                    point[key] = point[key] / maximum * 100 if maximum else 0.0
    final_end = points[-1]["periodEnd"]
    if final_end and final_end < cutoff.isoformat():
        cutoff = date.fromisoformat(final_end)
    return {"schemaVersion": 1, "reportingMonth": month.isoformat(),
        "rangeStart": frozen["rangeStart"], "rangeEnd": frozen["rangeEnd"], "cutoff": cutoff.isoformat(),
        "capturedAt": frozen["capturedAt"], "shareAmounts": selection["shareAmounts"],
        "currency": frozen["currency"] if selection["shareAmounts"] else None,
        "basis": basis, "basisFallback": fallback, "stripe": stripe_mode, "points": points}


def history_labels(chart):
    """Plain names for the two series and the chart, for any reader."""
    cash = (chart or {}).get("basis") == "cash"
    return {"in": "Cash in" if cash else "Revenue", "out": "Cash out" if cash else "Costs",
        "title": "Cash in and out" if cash else "Revenue and costs",
        "source": ("Xero Bank Summary (cash basis)" if cash else "Xero Profit and Loss (accrual basis)")
            if (chart or {}).get("stripe") != "only" else "Stripe balance (Stripe sales only)"}


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
    labels = history_labels(chart)
    return {"reportingMonth": month.strftime("%B %Y"), "points": points, "segments": segments,
        "inLabel": labels["in"], "outLabel": labels["out"], "title": labels["title"], "sourceLabel": labels["source"],
        "unit": chart["currency"] if chart["shareAmounts"] else "Relative scale · peak = 100",
        "cutoff": chart["cutoff"], "partial": chart["cutoff"] < chart["rangeEnd"],
        "ticks": [{"y": position(low + (high - low) * index / 4),
            "top": round(position(low + (high - low) * index / 4) / 220 * 100, 2),
            "label": axis_label(low + (high - low) * index / 4)} for index in range(5)]}
