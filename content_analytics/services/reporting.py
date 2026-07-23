from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max, Sum
from django.utils import timezone

from content_analytics.models import (
    AnalyticsSite,
    AnalyticsSyncState,
    ArticleBehaviorDaily,
    ArticleSearchDaily,
    ArticleSearchQueryDaily,
    ArticleTrafficSourceDaily,
    SearchConsoleProperty,
)
from content_factory.models import ArticlePublishStatus, WrittenArticle


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value, digits=6) -> float:
    try:
        return round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0.0


def _rate(numerator, denominator) -> float | None:
    denominator_value = _float(denominator, 10)
    if denominator_value <= 0:
        return None
    return round(_float(numerator, 10) / denominator_value, 6)


def _empty_behavior() -> dict:
    return {
        "pageviews": 0,
        "visitors": 0,
        "visits": 0,
        "bounces": 0,
        "engaged_30_count": 0,
        "scroll_50_count": 0,
        "scroll_90_count": 0,
        "cta_impression_count": 0,
        "cta_click_count": 0,
        "outbound_click_count": 0,
    }


def _behavior_totals(queryset) -> dict:
    fields = list(_empty_behavior())
    raw = queryset.aggregate(**{field: Sum(field) for field in fields})
    return {field: _int(raw.get(field)) for field in fields}


def _search_totals(queryset) -> dict:
    # Headline rows are aggregate-grain only. Future country/device rows are
    # intentionally excluded so they cannot double-count totals.
    rows = list(
        queryset.filter(country="", device="").values("clicks", "impressions", "position")
    )
    if not rows:
        return {
            "searchClicks": None,
            "searchImpressions": None,
            "searchCtr": None,
            "averagePosition": None,
        }
    clicks = sum((row["clicks"] or Decimal("0") for row in rows), Decimal("0"))
    impressions = sum((row["impressions"] or Decimal("0") for row in rows), Decimal("0"))
    position_weight = sum(
        ((row["position"] or Decimal("0")) * (row["impressions"] or Decimal("0")) for row in rows),
        Decimal("0"),
    )
    return {
        "searchClicks": _float(clicks, 4),
        "searchImpressions": _float(impressions, 4),
        "searchCtr": _rate(clicks, impressions),
        "averagePosition": _float(position_weight / impressions, 4) if impressions else None,
    }


def _metric_payload(behavior: dict, search: dict) -> dict:
    visits = behavior["visits"]
    cta_impressions = behavior["cta_impression_count"]
    return {
        "pageviews": behavior["pageviews"],
        "visitors": behavior["visitors"],
        "visits": visits,
        # Umami's single-page visit definition is shown explicitly rather than
        # being presented as proof of poor engagement.
        "singlePageVisits": behavior["bounces"],
        "singlePageVisitRate": _rate(behavior["bounces"], visits),
        "engaged30Visits": behavior["engaged_30_count"],
        "engaged30": behavior["engaged_30_count"],
        "engagedReaderRate": _rate(behavior["engaged_30_count"], visits),
        "scroll50Visits": behavior["scroll_50_count"],
        "scroll50": behavior["scroll_50_count"],
        "scroll50Rate": _rate(behavior["scroll_50_count"], visits),
        "scroll90Visits": behavior["scroll_90_count"],
        "scroll90": behavior["scroll_90_count"],
        "scroll90Rate": _rate(behavior["scroll_90_count"], visits),
        "ctaImpressionVisits": cta_impressions,
        "ctaImpressions": cta_impressions,
        "ctaVisibilityRate": _rate(cta_impressions, visits),
        "ctaClickVisits": behavior["cta_click_count"],
        "ctaClicks": behavior["cta_click_count"],
        "ctaClickThroughRate": _rate(behavior["cta_click_count"], cta_impressions),
        "visitorToCtaRate": _rate(behavior["cta_click_count"], visits),
        "ctaConversionRate": _rate(behavior["cta_click_count"], visits),
        "outboundClickVisits": behavior["outbound_click_count"],
        **search,
    }


def _article_payload(article, behavior_qs, search_qs) -> dict:
    return {
        "id": str(article.id),
        "analyticsId": str(article.analytics_id),
        "title": article.title,
        "slug": article.slug,
        "canonicalUrl": article.canonical_url or article.live_url or "",
        "canonicalPath": article.canonical_path or "",
        "publishStatus": article.publish_status,
        "metrics": _metric_payload(_behavior_totals(behavior_qs), _search_totals(search_qs)),
    }


def build_analytics_summary(
    organization,
    *,
    start_date: date,
    end_date: date,
    article: WrittenArticle | None = None,
) -> dict:
    behavior = ArticleBehaviorDaily.objects.filter(
        organization=organization,
        date__range=(start_date, end_date),
    )
    search = ArticleSearchDaily.objects.filter(
        organization=organization,
        date__range=(start_date, end_date),
    )
    sources = ArticleTrafficSourceDaily.objects.filter(
        organization=organization,
        date__range=(start_date, end_date),
    )
    articles_qs = WrittenArticle.objects.filter(
        organization=organization,
        publish_status=ArticlePublishStatus.LIVE,
    ).order_by("-created_at")
    if article is not None:
        behavior = behavior.filter(article=article)
        search = search.filter(article=article)
        sources = sources.filter(article=article)
        articles_qs = articles_qs.filter(pk=article.pk)

    source_totals = {}
    for row in sources.values(
        "source_category",
        "source_name",
        "pageviews",
        "visitors",
        "visits",
        "cta_impression_count",
        "cta_click_count",
        "conversion_attribution_available",
    ):
        key = (row["source_category"], row["source_name"])
        aggregate = source_totals.setdefault(
            key,
            {
                "source_category": key[0],
                "source_name": key[1],
                "pageviews": 0,
                "visitors": 0,
                "visits": 0,
                "cta_impressions": 0,
                "cta_clicks": 0,
                "conversion_attribution_available": True,
            },
        )
        aggregate["pageviews"] += _int(row["pageviews"])
        aggregate["visitors"] += _int(row["visitors"])
        aggregate["visits"] += _int(row["visits"])
        aggregate["cta_impressions"] += _int(row["cta_impression_count"])
        aggregate["cta_clicks"] += _int(row["cta_click_count"])
        if _int(row["visits"]) and not row["conversion_attribution_available"]:
            aggregate["conversion_attribution_available"] = False
    source_rows = sorted(
        source_totals.values(),
        key=lambda row: (-row["visits"], row["source_category"], row["source_name"]),
    )
    serialized_sources = [
        {
            "key": f"{row['source_category']}:{row['source_name']}",
            "label": row["source_name"],
            "category": row["source_category"],
            "name": row["source_name"],
            "pageviews": _int(row["pageviews"]),
            "visitors": _int(row["visitors"]),
            "visits": _int(row["visits"]),
            "ctaImpressionVisits": _int(row["cta_impressions"]),
            "ctaClickVisits": _int(row["cta_clicks"]),
            "ctaClicks": _int(row["cta_clicks"]),
            "isAi": row["source_category"] == "ai",
            "conversionAttributionAvailable": bool(row["conversion_attribution_available"]),
            "visitorToCtaRate": (
                _rate(row["cta_clicks"], row["visits"])
                if row["conversion_attribution_available"]
                else None
            ),
        }
        for row in source_rows
    ]

    analytics_site = AnalyticsSite.objects.filter(organization=organization).first()
    gsc_property = SearchConsoleProperty.objects.filter(organization=organization).first()
    sync_states = {
        state.source: state
        for state in AnalyticsSyncState.objects.filter(organization=organization)
    }
    behavior_through = behavior.aggregate(value=Max("date"))["value"]
    search_through = search.filter(country="", device="").aggregate(value=Max("date"))["value"]
    updated_candidates = [
        value
        for value in (
            analytics_site.last_synced_at if analytics_site else None,
            gsc_property.last_synced_at if gsc_property else None,
        )
        if value
    ]
    updated_at = max(updated_candidates) if updated_candidates else None
    now = timezone.now()
    stale_sources = []
    if analytics_site and analytics_site.enabled:
        if not analytics_site.last_synced_at or analytics_site.last_synced_at < now - timedelta(days=2):
            stale_sources.append("behavior analytics")
    if gsc_property and gsc_property.sync_enabled:
        if not gsc_property.last_synced_at or gsc_property.last_synced_at < now - timedelta(days=5):
            stale_sources.append("Search Console")
    stale = bool(stale_sources)
    freshness = {
        "analyticsDataThrough": behavior_through,
        "searchDataThrough": search_through,
        "updatedAt": updated_at,
        "stale": stale,
        "message": (
            f"Stale or unsynced source: {', '.join(stale_sources)}."
            if stale_sources
            else "Analytics have not synced yet."
            if updated_at is None
            else "Analytics are up to date within provider reporting delays."
        ),
        "behaviorThrough": behavior_through,
        "searchThrough": search_through,
        "umamiLastSyncedAt": analytics_site.last_synced_at if analytics_site else None,
        "searchConsoleLastSyncedAt": gsc_property.last_synced_at if gsc_property else None,
        "sources": {
            key: {
                "status": state.status,
                "syncedThrough": state.synced_through,
                "lastCompletedAt": state.last_completed_at,
                "lastError": state.last_error,
            }
            for key, state in sync_states.items()
        },
    }
    payload = {
        "range": f"{(end_date - start_date).days + 1}d",
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": {
            "start": start_date,
            "end": end_date,
            "days": (end_date - start_date).days + 1,
        },
        "totals": _metric_payload(_behavior_totals(behavior), _search_totals(search)),
        "articles": [
            _article_payload(
                item,
                behavior.filter(article=item),
                search.filter(article=item),
            )
            for item in articles_qs
        ],
        "sources": serialized_sources,
        "freshness": freshness,
        "metricSemantics": {
            "milestones": "unique_visits",
            "singlePageVisitRate": "Umami pageview-only single-page visits; custom events do not reduce it.",
            "sourceConversion": "null in the daily sync; CTA milestones are exact overall but source-level CTA attribution is deferred.",
            "aiAttribution": "detected referrals only; direct or stripped-referrer visits cannot be classified.",
        },
    }
    if article is not None:
        query_rows = (
            ArticleSearchQueryDaily.objects.filter(
                article=article,
                date__range=(start_date, end_date),
            )
            .values("query")
            .annotate(clicks=Sum("clicks"), impressions=Sum("impressions"))
            .order_by("-clicks", "-impressions", "query")[:50]
        )
        payload["queries"] = [
            {
                "query": row["query"],
                "clicks": _float(row["clicks"], 4),
                "impressions": _float(row["impressions"], 4),
                "ctr": _rate(row["clicks"], row["impressions"]),
            }
            for row in query_rows
        ]
    return payload
