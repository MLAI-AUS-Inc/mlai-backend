"""Pure topic metric normalization shared by persistence and the founder API.

Missing provider observations never become a measured zero or the historical
model's difficulty=50 default. A chart and its trend always share one source.
"""

from datetime import date
import math


VERIFIED_DIFFICULTY_SOURCES = {"dataforseo_labs", "dataforseo_bulk"}


def value_for(mapping, *keys, default=None):
    """Read aliases without dropping valid zero/false observations."""
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return default


def finite_number(value):
    """Return a finite number; reject booleans and missing observations."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def difficulty_metrics(mapping):
    """Expose only verified, in-range difficulty scores to readers."""
    source = str(value_for(mapping, "difficulty_source", "difficultySource", default="missing"))
    number = finite_number(mapping.get("difficulty"))
    available = (
        source in VERIFIED_DIFFICULTY_SOURCES and number is not None
        and 0 <= number <= 100 and number.is_integer()
    )
    state = value_for(mapping, "difficulty_status", "difficultyStatus")
    reason = value_for(mapping, "difficulty_reason", "difficultyReason")
    return {
        "difficulty": int(number) if available else None,
        "difficultySource": source,
        "difficultyStatus": "available" if available else "error" if state == "error" else "unavailable",
        "difficultyReason": "" if available else str(reason or "The search provider did not return a verified difficulty score for this keyword."),
    }


def dated_history(values):
    """Return real dated observations from the latest six calendar months.

    Provider monthly rows retain their year/month shape. Daily series retain
    dates; missing/invalid observations are omitted, never interpolated.
    """
    observations = {}
    if not isinstance(values, list):
        return []
    for row in values:
        if not isinstance(row, dict):
            continue
        number = finite_number(value_for(row, "search_volume", "searchVolume", "volume", "value"))
        if number is None or number < 0:
            continue
        try:
            if row.get("year") is not None and row.get("month") is not None:
                year = finite_number(row["year"])
                month = finite_number(row["month"])
                if year is None or month is None or not year.is_integer() or not month.is_integer():
                    continue
                day = date(int(year), int(month), 1)
                clean = {"year": day.year, "month": day.month, "search_volume": number}
            else:
                day = date.fromisoformat(str(value_for(row, "date", "timestamp", default=""))[:10])
                clean = {"date": day.isoformat(), "volume": number}
        except (TypeError, ValueError, OverflowError):
            continue
        observations[day] = clean
    if not observations:
        return []
    latest = max(observations)
    earliest_month = latest.year * 12 + latest.month - 5
    return [row for day, row in sorted(observations.items()) if day.year * 12 + day.month >= earliest_month]


def _monthly_direction(history):
    """Compare equal early/late windows; gaps and no demand are unknown."""
    if len(history) < 2 or any("year" not in row for row in history):
        return "unknown", None
    months = [row["year"] * 12 + row["month"] for row in history]
    if any(right - left != 1 for left, right in zip(months, months[1:])):
        return "unknown", None
    return _direction([row["search_volume"] for row in history])


def _direction(values):
    if len(values) < 2:
        return "unknown", None
    half = len(values) // 2
    start = sum(values[:half]) / half
    end = sum(values[-half:]) / half
    if not start:
        return ("breakout", None) if end else ("unknown", None)
    percent = (end - start) / start * 100
    direction = "breakout" if percent >= 100 else "rising" if percent > 15 else "declining" if percent < -15 else "stable"
    return direction, round(percent, 1)


def _velocity_history(velocity):
    history = dated_history(value_for(velocity, "monthly_searches", "monthlySearches", default=[]))
    if history:
        return history
    values = value_for(velocity, "daily_volumes", "dailyVolumes", default=[])
    history = dated_history(values)
    if history:
        return history
    dates = value_for(velocity, "dates", "trend_dates", "trendDates", "daily_dates", "dailyDates", default=[])
    if isinstance(values, list) and isinstance(dates, list) and len(values) == len(dates):
        return dated_history([{"date": day, "volume": number} for day, number in zip(dates, values)])
    return []


def topic_metric_payload(mapping):
    """Normalize flat discovery options and stored keyword velocity snapshots."""
    velocity = value_for(mapping, "velocity_data", "velocity", default={})
    velocity = velocity if isinstance(velocity, dict) else {}
    velocity_history = _velocity_history(velocity)
    monthly_history = dated_history(value_for(mapping, "monthly_searches", "monthlySearches", default=[]))
    # monthly_searches is the Google volume column. Keep it as the primary
    # series when present, independently of an older AI-search velocity row.
    google_monthly = bool(monthly_history) and all("year" in row for row in monthly_history)
    use_velocity = bool(velocity_history) and not google_monthly
    history = monthly_history if google_monthly else velocity_history or monthly_history or _velocity_history(mapping)
    is_monthly = bool(history) and all("year" in row for row in history)

    if use_velocity:
        source = str(value_for(velocity, "source", default="unknown"))
        basis = str(value_for(velocity, "basis", default="unknown"))
        estimated = value_for(velocity, "is_estimated", "isEstimated", default=True)
        period = value_for(velocity, "period_label", "periodLabel", default="")
    else:
        source = str(value_for(mapping, "monthly_searches_source", "monthlySearchesSource", "trend_source", "trendSource", default="unknown"))
        basis = str(value_for(mapping, "monthly_searches_basis", "monthlySearchesBasis", "trend_basis", "trendBasis", default="unknown"))
        estimated = value_for(mapping, "trend_is_estimated", "trendIsEstimated", default=True)
        period = value_for(mapping, "trend_period_label", "trendPeriodLabel", default="")
        # Stored monthly_searches is Google volume, not the AI-search/Trends
        # series that may have supplied a legacy option's flat trend metadata.
        if monthly_history and is_monthly and basis not in {"search_volume", "google_search_volume"}:
            source, basis, estimated, period = "dataforseo_labs", "search_volume", False, ""

    if is_monthly:
        state, percent = _monthly_direction(history)
        first, last = history[0], history[-1]
        period = f"{date(first['year'], first['month'], 1):%b %Y} – {date(last['year'], last['month'], 1):%b %Y}"
    else:
        state, percent = _direction([row["volume"] for row in history])

    descriptions = {
        "breakout": "Search interest has grown sharply over the measured period.",
        "rising": "Search interest is rising over the measured period.",
        "stable": "Search interest is broadly stable over the measured period.",
        "declining": "Search interest is declining over the measured period.",
        "unknown": "There is not enough consecutive search history to establish a trend.",
    }
    return {
        **difficulty_metrics(mapping),
        "monthlySearches": history,
        "monthlySearchesSource": source if history else "unknown",
        "monthlySearchesBasis": basis if history else "unknown",
        "trendStatus": state,
        "trendPercent": percent,
        "trendDescription": descriptions[state],
        "trendSource": source if history else "unknown",
        "trendBasis": basis if history else "unknown",
        "trendPeriodLabel": period if history else "",
        "trendIsEstimated": estimated if history else True,
        "trendReason": str(value_for(mapping, "trend_reason", "trendReason", default="")) if state == "unknown" else "",
        "trendCountry": value_for(mapping, "trend_country", "trendCountry"),
        "trendLanguage": value_for(mapping, "trend_language", "trendLanguage"),
        "metricsCheckedAt": value_for(mapping, "metrics_checked_at", "metricsCheckedAt"),
        "trendLastUpdatedAt": value_for(mapping, "trend_last_updated_at", "trendLastUpdatedAt"),
    }


def keyword_measurement_defaults(mapping):
    """Build sparse update defaults so failed lookups cannot erase evidence."""
    defaults = {}
    difficulty = difficulty_metrics(mapping)
    if difficulty["difficultyStatus"] == "available":
        defaults.update(difficulty=difficulty["difficulty"], difficulty_source=difficulty["difficultySource"])
    history = dated_history(value_for(mapping, "monthly_searches", "monthlySearches", default=[]))
    if history:
        defaults["monthly_searches"] = history
    return defaults


def metric_history_recency(metrics):
    """Rank normalized bundles by observed date, then known provider dates."""
    history = metrics.get("monthlySearches") or []
    if not history:
        return ("", "", "")
    last = history[-1]
    observed = date(last["year"], last["month"], 1).isoformat() if "year" in last else last["date"]
    return (observed, str(metrics.get("trendLastUpdatedAt") or ""), str(metrics.get("metricsCheckedAt") or ""))


def velocity_snapshot_defaults(mapping):
    """Only persist observed, dated series; empty fallbacks cannot hide history."""
    velocity = value_for(mapping, "velocity_data", "velocity", default={})
    if not isinstance(velocity, dict):
        return None
    history = _velocity_history(velocity)
    if not history:
        return None
    metrics = topic_metric_payload({"velocity": velocity})
    # The existing model's enum has no unknown value. Unclassifiable monthly
    # observations remain in the keyword JSON field and are exposed as unknown
    # by the API; do not create a fake stable snapshot that hides older evidence.
    if metrics["trendStatus"] == "unknown":
        return None
    score = finite_number(value_for(velocity, "velocity_score", "velocityScore"))
    absolute = finite_number(value_for(velocity, "absolute_volume", "absoluteVolume"))
    return {
        "absolute_volume": int(max(0, absolute or 0)),
        "velocity_score": (metrics["trendPercent"] / 100) if metrics["trendPercent"] is not None else score or 0,
        "trend_status": metrics["trendStatus"],
        "daily_volumes": history,
        "source": str(metrics["trendSource"])[:32],
        "basis": str(metrics["trendBasis"])[:32],
        "period_label": str(metrics["trendPeriodLabel"])[:64],
        "is_estimated": metrics["trendIsEstimated"] is not False,
    }
