"""Calendar-day agent shares from durable, public reporter session history."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import TruncDate

from .models import TokenUsageSession
from .token_agent_leaderboard import SOURCE_LABELS
from .token_usage import TOKEN_FIELDS, normalized_token_total


HISTORY_WINDOWS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}


def daily_agent_history(window, anchor, now):
    """Read bounded history without storing duplicate snapshots on page views.

    Ingest already upserts (account, agent, session, model) durably and
    idempotently. Grouping those rows by session start date matches the main
    leaderboard and also includes reporter backfills. Later reports may revise
    the day a session began. Private/deleted accounts never enter this series.
    The external federation has no dated session contract and is excluded.
    """
    date_from = anchor - timedelta(days=HISTORY_WINDOWS[window] - 1)
    zone = ZoneInfo(settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE)
    groups = (
        TokenUsageSession.objects.filter(
            account__is_public=True,
            started_at__lte=now,
            started_at__gte=datetime.combine(date_from, time.min, tzinfo=zone),
            started_at__lt=datetime.combine(
                anchor + timedelta(days=1), time.min, tzinfo=zone
            ),
        )
        .annotate(usage_date=TruncDate("started_at", tzinfo=zone))
        .values("usage_date", "source")
        .annotate(**{field: Sum(field) for field in TOKEN_FIELDS})
        .order_by("usage_date", "source")
    )
    return {
        "scope": "mlai",
        "window": window,
        "timezone": settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE,
        "basis": "session_started_at",
        "date_from": date_from.isoformat(),
        "date_to": anchor.isoformat(),
        "points": history_points(groups, date_from, anchor),
    }


def history_points(groups, date_from, date_to):
    """Return every date; zero-total days have no shares, never invented usage."""
    days = {}
    for group in groups:
        day = group["usage_date"]
        if not date_from <= day <= date_to:
            continue
        source = group["source"]
        total = normalized_token_total(source, group)
        if total <= 0:
            continue
        agents = days.setdefault(day, {})
        agents[source] = agents.get(source, 0) + total
    points = []
    day = date_from
    while day <= date_to:
        agents = [
            {
                "source": source,
                "display_name": SOURCE_LABELS.get(
                    source, source.replace("_", " ").replace("-", " ").title()
                ),
                "grand_total": total,
            }
            for source, total in sorted(
                days.get(day, {}).items(), key=lambda item: (-item[1], item[0])
            )
        ]
        points.append(
            {
                "date": day.isoformat(),
                "grand_total": sum(agent["grand_total"] for agent in agents),
                "agents": agents,
            }
        )
        day += timedelta(days=1)
    return points
