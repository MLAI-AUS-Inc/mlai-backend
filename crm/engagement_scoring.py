"""Deterministic activity ratings, independent of consent, delivery and storage.

All observations need their own source date and stable identifier. Scores use
Melbourne calendar days and deliberately give no credit to email opens.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable
from zoneinfo import ZoneInfo

VERSION = "mlai-observed-v2-daily"
MELBOURNE = ZoneInfo("Australia/Melbourne")
SOURCES = ("luma", "slack", "app", "newsletter")
SOURCE_KINDS = {
    "luma": frozenset(("checkin", "registration")),
    "slack": frozenset(("slack_message",)),
    "app": frozenset(("app_login",)),
    "newsletter": frozenset((
        "newsletter_click", "newsletter_comment", "newsletter_share",
        "newsletter_view", "newsletter_open",
    )),
}
SOURCE_STATUSES = frozenset(("available", "partial", "stale", "unavailable"))


@dataclass(frozen=True)
class ActivityEvidence:
    """One dated observation; ``event_id`` identifies the underlying source action.

    Check-in and registration observations for one Luma event must use the same
    event ID. ``human_verified`` distinguishes filtered human newsletter actions
    from clicks that could have come from security scanners.
    """

    source: str
    kind: str
    event_id: str
    occurred_at: date | datetime | str
    human_verified: bool = False


def _temporal(value) -> tuple[date, datetime | None]:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Activity timestamps must be ISO dates or timezone-aware datetimes.") from exc
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Activity datetimes must include a timezone.")
        return value.astimezone(MELBOURNE).date(), value
    if isinstance(value, date):
        return value, None
    raise ValueError("Activity timestamps must be dates or timezone-aware datetimes.")


def recency_weight(age_days: int) -> float:
    """Weight a dated action over the 0–29, 30–89 and 90–179 day windows."""

    if type(age_days) is not int:
        raise ValueError("Activity age must be a whole number of calendar days.")
    if 0 <= age_days < 30:
        return 1.0
    if 30 <= age_days < 90:
        return 0.5
    if 90 <= age_days < 180:
        return 0.25
    return 0.0


def _source_coverage(coverage: dict) -> dict:
    if not isinstance(coverage, dict):
        raise ValueError("Source coverage must be an object.")
    result = {}
    for source in SOURCES:
        entry = coverage.get(source, {"status": "unavailable", "detail": "No source coverage supplied."})
        if not isinstance(entry, dict) or entry.get("status") not in SOURCE_STATUSES:
            raise ValueError(f"Invalid coverage status for {source}.")
        if "expected" in entry and type(entry["expected"]) is not bool:
            raise ValueError(f"Coverage expectation must be a boolean for {source}.")
        result[source] = deepcopy(entry)
    return result


def _observations(activities, as_of_day, cutoff):
    observations = {}
    for item in activities:
        if not isinstance(item, ActivityEvidence):
            raise ValueError("Activities must be ActivityEvidence records.")
        if item.source not in SOURCE_KINDS or item.kind not in SOURCE_KINDS[item.source]:
            raise ValueError("Unknown activity source or kind.")
        if not isinstance(item.event_id, str) or not item.event_id.strip():
            raise ValueError("Each activity needs a stable source event ID.")
        if type(item.human_verified) is not bool:
            raise ValueError("Human verification must be a boolean.")
        day, instant = _temporal(item.occurred_at)
        if day > as_of_day or (instant is not None and instant > cutoff):
            continue
        key = (item.source, item.kind, item.event_id)
        previous = observations.get(key)
        # An older copy wins rather than making a replay look newly active.
        if previous is None or day < previous[1]:
            observations[key] = (item, day)
        elif day == previous[1] and item.human_verified and not previous[0].human_verified:
            observations[key] = (item, day)
    return [
        observation for observation in observations.values()
        if recency_weight((as_of_day - observation[1]).days)
    ]


def score_engagement(
    activities: Iterable[ActivityEvidence], *, as_of: date | datetime | str, coverage: dict,
) -> dict | None:
    """Return a rating snapshot, or None when no current source can be assessed.

    Coverage stays attached to the result. ``refresh_safe`` is false when an
    expected source is stale/unavailable, allowing the caller to retain the
    previous rating instead of treating an outage as inactivity. This function
    reads no preferences and performs no I/O.
    """

    as_of_day, as_of_instant = _temporal(as_of)
    cutoff = as_of_instant or datetime.combine(as_of_day, time.max, tzinfo=MELBOURNE)
    sources = _source_coverage(coverage)
    if not any(entry["status"] in ("available", "partial") for entry in sources.values()):
        return None
    observations = _observations(activities, as_of_day, cutoff)
    # Dated evidence remains real even when the source is temporarily stale;
    # the coverage flag prevents interpreting absent new activity as a zero.
    usable = [(item, day) for item, day in observations if sources[item.source]["status"] != "unavailable"]
    by_kind = {}
    for item, day in usable:
        by_kind.setdefault(item.kind, []).append((item, day))
    weight = lambda day: recency_weight((as_of_day - day).days)
    checkins = by_kind.get("checkin", [])
    checked_ids = {item.event_id for item, _ in checkins}
    registrations = [
        (item, day) for item, day in by_kind.get("registration", [])
        if item.event_id not in checked_ids
    ]
    slack_days = {day for _, day in by_kind.get("slack_message", [])}
    login_days = [day for _, day in by_kind.get("app_login", [])]
    # Only latest-login recency is known; repeated logins do not invent usage frequency.
    latest_login = max(login_days, default=None)
    strong_dates = {day for _, day in checkins} | slack_days

    newsletter = {}
    for kind in ("newsletter_comment", "newsletter_share", "newsletter_click", "newsletter_view"):
        rows = by_kind.get(kind, [])
        # A newsletter source event can be observed more than once on one day;
        # days cap each action class to avoid volume dominating the rating.
        days = {}
        for item, day in rows:
            days[day] = days.get(day, False) or item.human_verified
        newsletter[kind] = days
    comment_days = {day for day, verified in newsletter["newsletter_comment"].items() if verified}
    share_days = {day for day, verified in newsletter["newsletter_share"].items() if verified}
    verified_click_days = {day for day, verified in newsletter["newsletter_click"].items() if verified}
    unverified_click_days = {day for day, verified in newsletter["newsletter_click"].items() if not verified}
    strong_dates |= comment_days | share_days | verified_click_days
    comment_points = min(10, 5 * sum(map(weight, comment_days)))
    share_points = min(6, 3 * sum(map(weight, share_days)))
    verified_click_points = min(10, 3 * sum(map(weight, verified_click_days)))
    # Unfiltered clicks and views are weak and cannot satisfy strong-activity gates.
    weak_click_points = 2 * max((weight(day) for day in unverified_click_days), default=0)
    view_points = min(4, 0.5 * sum(map(weight, newsletter["newsletter_view"])))
    newsletter_points = min(15, comment_points + share_points + verified_click_points + min(5, weak_click_points + view_points))
    points = {
        "events": min(60, 20 * sum(weight(day) for _, day in checkins)),
        "registrations": min(6, 2 * sum(weight(day) for _, day in registrations)),
        "slack": min(50, 5 * sum(map(weight, slack_days))),
        "app": 5 * weight(latest_login) if latest_login else 0,
        "newsletter": newsletter_points,
    }
    score = round(min(100, sum(points.values())), 2)
    stars = 1 if score < 5 else 2 if score < 15 else 3 if score < 30 else 4 if score < 50 else 5
    strong_30 = any((as_of_day - day).days < 30 for day in strong_dates)
    strong_90 = any((as_of_day - day).days < 90 for day in strong_dates)
    if not strong_90:
        stars = min(stars, 3)
    if not strong_30 or len(strong_dates) < 3:
        stars = min(stars, 4)
    reasons = []
    if checkins:
        reasons.append(f"{len(checkins)} event check-ins in 180d")
    if slack_days:
        reasons.append(f"{len(slack_days)} public Slack active days in 180d")
    if registrations:
        reasons.append(f"{len(registrations)} registrations without confirmed attendance in 180d")
    if latest_login:
        reasons.append(f"Latest login {(as_of_day - latest_login).days}d ago; frequency unavailable")
    if comment_days or share_days or verified_click_days:
        reasons.append("Verified newsletter interaction")
    if weak_click_points or view_points:
        reasons.append("Weak newsletter click/view evidence")
    if not reasons:
        reasons.append("No scored recent activity observed in available sources")
    unavailable = [source for source, entry in sources.items() if entry["status"] == "unavailable"]
    stale = [source for source, entry in sources.items() if entry["status"] == "stale"]
    limited = [source for source, entry in sources.items() if entry["status"] != "available"]
    refresh_safe = not any(
        entry["status"] in ("stale", "unavailable") and entry.get("expected", True)
        for entry in sources.values()
    )
    summary = "All sources available." if not limited else "Limited source coverage: " + ", ".join(limited) + ". Missing evidence is not inactivity."
    if sources["newsletter"].get("substack_automatic") is False:
        summary += " Substack uses dated import history; fresh subscriber activity is not connected."
    return {
        "stars": stars, "score": score, "version": VERSION,
        "as_of": as_of_day.isoformat(), "reason": "; ".join(reasons),
        "coverage": {
            "provisional": bool(limited), "summary": summary, "sources": sources,
            "unavailable_sources": unavailable, "stale_sources": stale,
            "refresh_safe": refresh_safe,
            "channel_points": {key: round(value, 2) for key, value in points.items()},
            "strong_dates_180d": len(strong_dates), "strong_activity_30d": strong_30,
            "app_activity_available": sources["app"]["status"] in ("available", "partial"),
            "app_usage_frequency_available": False, "email_opens_scored": False,
            "newsletter_live_substack_available": sources["newsletter"].get("substack_automatic") is True,
        },
    }
