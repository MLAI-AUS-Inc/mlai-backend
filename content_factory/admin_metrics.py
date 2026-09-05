"""Admin usage metrics for the Vibe Marketing product.

The durable workflow-run, article, points-ledger and top-up records are the
sources of truth.  In particular, articles are counted from ``WrittenArticle``
rather than completed runs so retries and revisions do not inflate output.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet
from django.utils import timezone

from core.actor_ids import internal_actor_user_id
from roo.models import Ledger, PointsPurchase
from startup_updates.models import UserStartupBinding
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

from .models import ArticlePublishStatus, WrittenArticle
from .vibe_marketing_workflows import DISCOVERY_WORKFLOWS, VIBE_MARKETING_WORKFLOWS


RESEARCH_COMPLETE_STATUSES = frozenset(
    {ContentFactoryRunStatus.COMPLETED, ContentFactoryRunStatus.AWAITING_CONFIRMATION}
)
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
TIMELINE_MONTHS = 6


def normalize_usage_range(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in {*RANGE_DAYS, "all"} else DEFAULT_RANGE


def _period_bounds(range_key: str, now: datetime):
    if range_key == "all":
        return None, now, None, None
    duration = timedelta(days=RANGE_DAYS[range_key])
    start = now - duration
    return start, now, start - duration, start


def _apply_created_period(queryset: QuerySet, start: datetime | None, end: datetime):
    queryset = queryset.filter(created_at__lt=end)
    if start is not None:
        queryset = queryset.filter(created_at__gte=start)
    return queryset


def _apply_purchase_period(queryset: QuerySet, start: datetime | None, end: datetime):
    """Filter by paid time, falling back to creation for legacy paid rows."""

    before_end = Q(paid_at__lt=end) | Q(paid_at__isnull=True, created_at__lt=end)
    queryset = queryset.filter(before_end)
    if start is not None:
        after_start = Q(paid_at__gte=start) | Q(
            paid_at__isnull=True, created_at__gte=start
        )
        queryset = queryset.filter(after_start)
    return queryset


def _direct_user_id(actor: str, user_ids: set[int], slack_to_user: dict[str, int]):
    actor = str(actor or "").strip()
    if not actor:
        return None
    candidate = internal_actor_user_id(actor)
    if candidate is None:
        try:
            candidate = int(actor)
        except (TypeError, ValueError):
            candidate = None
    if candidate in user_ids:
        return candidate
    return slack_to_user.get(actor)


def _run_user_map(run_rows: Iterable[dict[str, Any]]) -> dict[str, set[int]]:
    """Resolve each run actor to a user, with an organization-binding fallback."""

    rows = list(run_rows)
    actor_values = {
        str(row.get("slack_user_id") or "").strip()
        for row in rows
        if row.get("slack_user_id")
    }
    possible_ids: set[int] = set()
    slack_values: set[str] = set()
    for actor in actor_values:
        candidate = internal_actor_user_id(actor)
        if candidate is None:
            try:
                candidate = int(actor)
            except (TypeError, ValueError):
                candidate = None
        if candidate is None:
            slack_values.add(actor)
        else:
            possible_ids.add(candidate)

    User = get_user_model()
    existing_user_ids = set(
        User.objects.filter(id__in=possible_ids).values_list("id", flat=True)
    )
    slack_to_user = dict(
        User.objects.filter(slack_id__in=slack_values).values_list("slack_id", "id")
    )

    organization_ids = {
        row["organization_id"] for row in rows if row.get("organization_id") is not None
    }
    bindings_by_org: dict[int, set[int]] = defaultdict(set)
    for organization_id, user_id in UserStartupBinding.objects.filter(
        organization_id__in=organization_ids
    ).values_list("organization_id", "user_id"):
        bindings_by_org[organization_id].add(user_id)

    resolved: dict[str, set[int]] = {}
    for row in rows:
        user_id = _direct_user_id(
            row.get("slack_user_id", ""), existing_user_ids, slack_to_user
        )
        user_ids = {user_id} if user_id is not None else set()
        if not user_ids and row.get("organization_id") is not None:
            user_ids.update(bindings_by_org.get(row["organization_id"], set()))
        resolved[str(row["run_id"])] = user_ids
    return resolved


def _article_user_ids(
    articles: Iterable[dict[str, Any]], active_user_ids: set[int]
) -> set[int]:
    rows = list(articles)
    run_ids = {row["source_run_id"] for row in rows if row.get("source_run_id")}
    source_runs = list(
        ContentFactoryRun.objects.filter(run_id__in=run_ids).values(
            "run_id", "slack_user_id", "organization_id"
        )
    )
    users_by_run = _run_user_map(source_runs)

    organization_ids = {
        row["organization_id"] for row in rows if row.get("organization_id") is not None
    }
    users_by_org: dict[int, set[int]] = defaultdict(set)
    for organization_id, user_id in UserStartupBinding.objects.filter(
        organization_id__in=organization_ids
    ).values_list("organization_id", "user_id"):
        users_by_org[organization_id].add(user_id)

    user_ids: set[int] = set()
    for row in rows:
        resolved = users_by_run.get(str(row.get("source_run_id") or ""), set())
        if not resolved:
            resolved = users_by_org.get(row.get("organization_id"), set())
        user_ids.update(resolved & active_user_ids)
    return user_ids


def _points_metrics(start: datetime | None, end: datetime) -> dict[str, int]:
    rows = _apply_created_period(
        Ledger.objects.filter(source="CONTENT_FACTORY"), start, end
    ).values_list("kind", "delta")
    gross_spent = 0
    refunded = 0
    for kind, delta in rows:
        delta = int(delta or 0)
        if kind == "SPEND" and delta < 0:
            gross_spent += abs(delta)
        elif kind == "REFUND" and delta > 0:
            refunded += delta
    return {
        "grossPointsSpent": gross_spent,
        "refundedPoints": refunded,
        "netPointsSpent": gross_spent - refunded,
    }


def _summary_for_period(start: datetime | None, end: datetime) -> tuple[dict, dict]:
    runs = list(
        _apply_created_period(
            ContentFactoryRun.objects.filter(workflow__in=VIBE_MARKETING_WORKFLOWS),
            start,
            end,
        ).values(
            "run_id",
            "workflow",
            "status",
            "slack_user_id",
            "organization_id",
            "domain",
        )
    )
    users_by_run = _run_user_map(runs)
    active_user_ids = set().union(*users_by_run.values()) if users_by_run else set()
    active_startups = {
        ("organization", row["organization_id"])
        if row.get("organization_id") is not None
        else ("domain", str(row.get("domain") or "").strip().lower())
        for row in runs
        if row.get("organization_id") is not None
        or str(row.get("domain") or "").strip()
    }

    articles = list(
        _apply_created_period(WrittenArticle.objects.all(), start, end).values(
            "source_run_id", "organization_id", "publish_status"
        )
    )
    live_articles = [
        article
        for article in articles
        if article["publish_status"] == ArticlePublishStatus.LIVE
    ]
    article_user_ids = _article_user_ids(articles, active_user_ids)
    live_article_user_ids = _article_user_ids(live_articles, active_user_ids)

    researched_user_ids: set[int] = set()
    for row in runs:
        if (
            row["workflow"] in DISCOVERY_WORKFLOWS
            and row["status"] in RESEARCH_COMPLETE_STATUSES
        ):
            researched_user_ids.update(users_by_run.get(str(row["run_id"]), set()))

    purchases = _apply_purchase_period(
        PointsPurchase.objects.filter(
            status="paid",
            currency__iexact="aud",
            user_id__in=active_user_ids,
        ),
        start,
        end,
    )
    purchase_rows = list(
        purchases.values_list("user_id", "points_amount", "amount_cents")
    )
    points = _points_metrics(start, end)
    summary = {
        "activeUsers": len(active_user_ids),
        "activeStartups": len(active_startups),
        "articlesCreated": len(articles),
        "articlesLive": len(live_articles),
        **points,
        "purchasers": len({row[0] for row in purchase_rows}),
        "pointsPurchased": sum(row[1] for row in purchase_rows),
        "purchaseRevenueCents": sum(row[2] for row in purchase_rows),
        "currency": "AUD",
        "failedRuns": sum(
            row["status"] == ContentFactoryRunStatus.FAILED for row in runs
        ),
        "blockedRuns": sum(
            row["status"] == ContentFactoryRunStatus.BLOCKED for row in runs
        ),
    }
    funnel = {
        "started": len(active_user_ids),
        "researched": len(researched_user_ids & active_user_ids),
        "article_created": len(article_user_ids),
        "article_live": len(live_article_user_ids),
    }
    return summary, funnel


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _previous_month(value: datetime) -> datetime:
    return (value - timedelta(days=1)).replace(day=1)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _timeline(now: datetime) -> list[dict[str, Any]]:
    cursor = _month_start(now)
    for _ in range(TIMELINE_MONTHS - 1):
        cursor = _previous_month(cursor)

    timeline = []
    current_month = _month_start(now)
    while cursor <= current_month:
        month_end = min(_next_month(cursor), now + timedelta(microseconds=1))
        summary, _ = _summary_for_period(cursor, month_end)
        timeline.append(
            {
                "period": cursor.strftime("%Y-%m"),
                "label": f"{calendar.month_abbr[cursor.month]} {cursor.year}",
                "activeUsers": summary["activeUsers"],
                "articlesCreated": summary["articlesCreated"],
                "netPointsSpent": summary["netPointsSpent"],
            }
        )
        cursor = _next_month(cursor)
    return timeline


def build_vibe_marketing_admin_usage_payload(
    range_value: str | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or timezone.now()
    range_key = normalize_usage_range(range_value)
    start, end, previous_start, previous_end = _period_bounds(range_key, now)
    summary, funnel_counts = _summary_for_period(start, end)
    previous = None
    if previous_start is not None and previous_end is not None:
        previous, _ = _summary_for_period(previous_start, previous_end)

    funnel_labels = {
        "started": "Started",
        "researched": "Completed research",
        "article_created": "Created an article",
        "article_live": "Published live",
    }
    return {
        "range": {
            "key": range_key,
            "start": start.isoformat() if start else None,
            "end": end.isoformat(),
            "previousStart": previous_start.isoformat() if previous_start else None,
            "previousEnd": previous_end.isoformat() if previous_end else None,
        },
        "summary": summary,
        "previous": previous,
        "funnel": [
            {"key": key, "label": label, "users": funnel_counts[key]}
            for key, label in funnel_labels.items()
        ],
        "timeline": _timeline(now),
        "asOf": now.isoformat(),
    }
