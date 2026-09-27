"""Immutable daily article-performance report snapshots.

Builds the "Article Performance Brief": a point-in-time JSON payload over a
rolling window of the per-day aggregate tables, persisted once per
(organization, report_date) and never silently recomputed. The live dashboard
keeps using ``reporting.build_analytics_summary`` (recompute-on-read); this
module exists so a delivered brief stays exactly as the customer first saw it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F, Max, Q, Sum

from content_analytics.models import (
    AnalyticsSite,
    AnalyticsSyncSource,
    AnalyticsSyncState,
    ArticleBehaviorDaily,
    ArticlePerformanceReport,
    ArticlePerformanceReportCategory,
    ArticleSearchDaily,
    ArticleTrafficSourceDaily,
    SearchConsoleProperty,
    SearchConsolePropertyStatus,
)
from content_analytics.services.reporting import (
    _behavior_totals,
    _float,
    _int,
    _metric_payload,
    _rate,
    _search_totals,
)
from content_analytics.services.article_health_evidence import (
    build_article_health_evidence,
    research_evidence_bundles,
)
from content_factory.models import ArticlePublishStatus, ResearchedKeyword, WrittenArticle
from workflow_runs.models import ContentFactoryRun

REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReportWindows:
    """Inclusive date bounds: the window covers completed days only."""

    window_start: date
    window_end: date
    prior_window_start: date
    prior_window_end: date


def report_windows(report_date: date, *, window_days: int | None = None) -> ReportWindows:
    days = int(window_days or settings.CONTENT_ANALYTICS_REPORT_WINDOW_DAYS)
    days = max(days, 1)
    window_end = report_date - timedelta(days=1)
    window_start = window_end - timedelta(days=days - 1)
    prior_window_end = window_start - timedelta(days=1)
    prior_window_start = prior_window_end - timedelta(days=days - 1)
    return ReportWindows(
        window_start=window_start,
        window_end=window_end,
        prior_window_start=prior_window_start,
        prior_window_end=prior_window_end,
    )


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _pct(rate: float | None, *, decimals: int = 0) -> str:
    if rate is None:
        return "0%"
    return f"{rate * 100:.{decimals}f}%"


def _delta(current: int, prior: int) -> int:
    return _int(current) - _int(prior)


def _rate_delta(current: float | None, prior: float | None) -> float | None:
    if current is None and prior is None:
        return None
    return round((current or 0.0) - (prior or 0.0), 6)


def _categorize(behavior: dict, *, window_days: int) -> tuple[str, list[str]]:
    visits = _int(behavior.get("visits"))
    min_visits = _int(settings.CONTENT_ANALYTICS_REPORT_MIN_VISITS)
    if visits <= 0 or visits < min_visits:
        return (
            ArticlePerformanceReportCategory.GATHERING_DATA,
            [
                f"Only {visits} visit{'s' if visits != 1 else ''} in the last "
                f"{window_days} days — not enough data for a reliable read."
            ],
        )

    conversion = _rate(behavior.get("cta_click_count"), visits)
    engaged = _rate(behavior.get("engaged_30_count"), visits)
    cta_reach = _rate(behavior.get("cta_impression_count"), visits)
    reasons: list[str] = []

    if (conversion or 0.0) >= settings.CONTENT_ANALYTICS_REPORT_TOP_CONVERSION_RATE:
        category = ArticlePerformanceReportCategory.TOP_PERFORMER
        reasons.append(
            f"Strong landing-to-CTA conversion ({_pct(conversion, decimals=1)} of visits click a CTA)."
        )
    elif (engaged or 0.0) >= settings.CONTENT_ANALYTICS_REPORT_HIGH_ENGAGED_RATE:
        category = ArticlePerformanceReportCategory.HIGH_INTEREST
        reasons.append(
            f"Readers engage ({_pct(engaged)} reach 30 active seconds) but few click a CTA "
            f"({_pct(conversion, decimals=1)} conversion)."
        )
    else:
        category = ArticlePerformanceReportCategory.NEEDS_ATTENTION
        reasons.append(
            f"Visitors arrive but leave without engaging ({_pct(engaged)} reach 30 active seconds, "
            f"{_pct(conversion, decimals=1)} CTA conversion)."
        )

    if (cta_reach or 0.0) < settings.CONTENT_ANALYTICS_REPORT_LOW_CTA_REACH_RATE:
        reasons.append(
            f"A CTA becomes visible in only {_pct(cta_reach)} of visits — placement may be limiting clicks."
        )
    return category, reasons


def _source_mix(source_qs) -> list[dict]:
    rows = (
        source_qs.values("source_category")
        .annotate(
            visits=Sum("visits"),
            pageviews=Sum("pageviews"),
            cta_clicks=Sum("cta_click_count"),
        )
        .order_by()
    )
    mix = [
        {
            "category": row["source_category"],
            "visits": _int(row["visits"]),
            "pageviews": _int(row["pageviews"]),
            "ctaClickVisits": _int(row["cta_clicks"]),
            "isAi": row["source_category"] == "ai",
        }
        for row in rows
    ]
    return sorted(mix, key=lambda row: (-row["visits"], row["category"]))


def _per_article_source_visits(source_qs) -> dict:
    per_article: dict = {}
    rows = (
        source_qs.values("article_id", "source_category")
        .annotate(visits=Sum("visits"))
        .order_by()
    )
    for row in rows:
        bucket = per_article.setdefault(row["article_id"], {})
        bucket[row["source_category"]] = _int(row["visits"])
    return per_article


def _search_block(organization, windows: ReportWindows) -> dict:
    gsc = SearchConsoleProperty.objects.filter(organization=organization).first()
    connected = bool(gsc and gsc.status == SearchConsolePropertyStatus.VERIFIED)
    block: dict = {
        "connected": connected,
        "syncEnabled": bool(gsc.sync_enabled) if gsc else False,
    }
    if not connected:
        return block
    search_qs = ArticleSearchDaily.objects.filter(
        organization=organization,
        date__range=(windows.window_start, windows.window_end),
    )
    block.update(_search_totals(search_qs))
    block["dataThrough"] = _iso(
        search_qs.filter(country="", device="").aggregate(value=Max("date"))["value"]
    )
    return block


def _article_health_evidence(organization, articles, report_date):
    """Load existing research in batches, preserving the organization boundary."""
    article_ids = [article.id for article in articles]
    keywords = {str(article.primary_keyword or "").strip().lower() for article in articles}
    keyword_rows = list(ResearchedKeyword.objects.filter(
        Q(written_article_id__in=article_ids) | Q(keyword_normalized__in=keywords),
        organization=organization,
    ).values("written_article_id", "keyword", "keyword_normalized", "competitor_urls"))
    source_run_ids = [article.source_run_id for article in articles if article.source_run_id]
    # Latest saved observations augment the original writing run as research is
    # refreshed. Existing run JSON carries evidence without a schema migration.
    runs = ContentFactoryRun.objects.filter(
        Q(run_id__in=source_run_ids)
        | Q(result__has_key="article_health_evidence")
        | Q(result__has_key="articleHealthEvidence"),
        organization=organization,
    ).order_by("-updated_at").values_list("result", flat=True)[:100]
    observations = [bundle for result in runs for bundle in research_evidence_bundles(result)]
    return {
        article.id: build_article_health_evidence(
            article,
            keywords=[
                row for row in keyword_rows
                if row["written_article_id"] == article.id
                or (row["written_article_id"] is None
                    and row["keyword_normalized"] == str(article.primary_keyword or "").strip().lower())
            ],
            observations=observations,
            as_of=report_date,
        )
        for article in articles
    }


def _per_article_search(organization, windows, *, connected):
    """Keep unavailable and not-yet-synced Google observations distinct from zero."""
    if not connected:
        return {}, None
    state = AnalyticsSyncState.objects.filter(
        organization=organization, source=AnalyticsSyncSource.SEARCH_CONSOLE,
    ).first()
    synced_through = state.synced_through if state else None
    rows = ArticleSearchDaily.objects.filter(
        organization=organization,
        date__range=(windows.window_start, windows.window_end),
        country="", device="", engine="google", surface="web",
    ).values("article_id").annotate(
        clicks=Sum("clicks"), impressions=Sum("impressions"),
        position_weight=Sum(F("position") * F("impressions")),
        data_through=Max("date"),
    ).order_by()
    return {
        row["article_id"]: {
            "searchClicks": _float(row["clicks"], 4),
            "searchImpressions": _float(row["impressions"], 4),
            "searchCtr": _rate(row["clicks"], row["impressions"]),
            "averagePosition": _float(row["position_weight"] / row["impressions"], 4)
            if row["impressions"] else None,
            "searchDataThroughDate": _iso(row["data_through"]),
        }
        for row in rows
    }, synced_through


def build_article_performance_payload(organization, report_date: date) -> dict:
    """Compute the brief payload. Pure read; JSON-safe (dates are ISO strings)."""
    window_days = max(int(settings.CONTENT_ANALYTICS_REPORT_WINDOW_DAYS), 1)
    windows = report_windows(report_date, window_days=window_days)

    behavior_window = ArticleBehaviorDaily.objects.filter(
        organization=organization,
        date__range=(windows.window_start, windows.window_end),
    )
    behavior_prior = ArticleBehaviorDaily.objects.filter(
        organization=organization,
        date__range=(windows.prior_window_start, windows.prior_window_end),
    )
    sources_window = ArticleTrafficSourceDaily.objects.filter(
        organization=organization,
        date__range=(windows.window_start, windows.window_end),
    )

    totals_behavior = _behavior_totals(behavior_window)
    prior_behavior = _behavior_totals(behavior_prior)
    search_block = _search_block(organization, windows)
    empty_search = {
        "searchClicks": None,
        "searchImpressions": None,
        "searchCtr": None,
        "averagePosition": None,
    }
    totals = _metric_payload(totals_behavior, empty_search)
    prior_totals = _metric_payload(prior_behavior, empty_search)

    # Live articles always appear (zero-traffic ones as "gathering data"); any
    # article that drew window traffic appears even if it is no longer live.
    live_articles = WrittenArticle.objects.filter(
        organization=organization,
        publish_status=ArticlePublishStatus.LIVE,
    )
    traffic_article_ids = set(behavior_window.values_list("article_id", flat=True))
    article_ids = set(live_articles.values_list("id", flat=True)) | traffic_article_ids
    articles = list(WrittenArticle.objects.filter(organization=organization, id__in=article_ids))
    health_evidence = _article_health_evidence(organization, articles, report_date)
    search_by_article, search_synced_through = _per_article_search(
        organization, windows, connected=search_block["connected"],
    )

    window_by_article = {
        row["article_id"]: row
        for row in behavior_window.values("article_id").annotate(
            **{field: Sum(field) for field in (
                "pageviews",
                "visitors",
                "visits",
                "bounces",
                "engaged_30_count",
                "scroll_50_count",
                "scroll_90_count",
                "cta_impression_count",
                "cta_click_count",
                "outbound_click_count",
            )}
        )
    }
    prior_visits_by_article = {
        row["article_id"]: _int(row["visits"])
        for row in behavior_prior.values("article_id").annotate(visits=Sum("visits"))
    }
    source_visits_by_article = _per_article_source_visits(sources_window)

    article_rows = []
    category_counts = {choice.value: 0 for choice in ArticlePerformanceReportCategory}
    for article in articles:
        raw = window_by_article.get(article.id) or {}
        behavior = {
            field: _int(raw.get(field))
            for field in (
                "pageviews",
                "visitors",
                "visits",
                "bounces",
                "engaged_30_count",
                "scroll_50_count",
                "scroll_90_count",
                "cta_impression_count",
                "cta_click_count",
                "outbound_click_count",
            )
        }
        category, reasons = _categorize(behavior, window_days=window_days)
        category_counts[category] += 1
        article_sources = source_visits_by_article.get(article.id, {})
        prior_visits = prior_visits_by_article.get(article.id, 0)
        article_search = search_by_article.get(article.id)
        health_evidence[article.id].update({
            "searchDataThroughDate": article_search.get("searchDataThroughDate") if article_search else None,
            "searchWindowComplete": bool(
                article_search and search_synced_through and search_synced_through >= windows.window_end
            ),
        })
        article_rows.append(
            {
                "id": str(article.id),
                "analyticsId": str(article.analytics_id),
                "title": article.title,
                "slug": article.slug,
                "canonicalUrl": article.canonical_url or article.live_url or "",
                "canonicalPath": article.canonical_path or "",
                "publishStatus": article.publish_status,
                "metrics": _metric_payload(behavior, article_search or empty_search),
                "priorVisits": prior_visits,
                "visitsDelta": _delta(behavior["visits"], prior_visits),
                "searchVisits": _int(article_sources.get("search")),
                "aiVisits": _int(article_sources.get("ai")),
                "category": category,
                "categoryLabel": ArticlePerformanceReportCategory(category).label,
                "reasons": reasons,
                "healthEvidence": health_evidence[article.id],
            }
        )
    article_rows.sort(key=lambda row: (-row["metrics"]["visits"], row["title"].lower()))

    site = AnalyticsSite.objects.filter(organization=organization).first()
    umami_state = AnalyticsSyncState.objects.filter(
        organization=organization,
        source=AnalyticsSyncSource.UMAMI,
    ).first()
    data_through = umami_state.synced_through if umami_state else None
    if data_through is None:
        data_through = behavior_window.aggregate(value=Max("date"))["value"]
    if data_through is not None and data_through > windows.window_end:
        data_through = windows.window_end

    notes = ["Known bots are excluded at collection; counts are human-like visits."]
    if site is None or not site.enabled:
        notes.append("Analytics collection is not enabled for this site.")
    if data_through is not None and data_through < windows.window_end:
        notes.append(
            f"Behavior data was available through {data_through.isoformat()} when this report "
            "was generated; later days had not synced yet."
        )

    deltas = {
        "visits": _delta(totals["visits"], prior_totals["visits"]),
        "engaged30Visits": _delta(totals["engaged30Visits"], prior_totals["engaged30Visits"]),
        "ctaClickVisits": _delta(totals["ctaClickVisits"], prior_totals["ctaClickVisits"]),
        "engagedReaderRate": _rate_delta(
            totals["engagedReaderRate"], prior_totals["engagedReaderRate"]
        ),
        "ctaConversionRate": _rate_delta(
            totals["ctaConversionRate"], prior_totals["ctaConversionRate"]
        ),
    }

    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "reportDate": _iso(report_date),
        "window": {
            "start": _iso(windows.window_start),
            "end": _iso(windows.window_end),
            "days": window_days,
        },
        "priorWindow": {
            "start": _iso(windows.prior_window_start),
            "end": _iso(windows.prior_window_end),
            "days": window_days,
        },
        "dataThroughDate": _iso(data_through),
        "headline": {
            "humanVisits": totals["visits"],
            "engagedReaderRate": totals["engagedReaderRate"],
            "engagedReaders": totals["engaged30Visits"],
            "ctaClickers": totals["ctaClickVisits"],
            "ctaConversionRate": totals["ctaConversionRate"],
            "visitsDelta": deltas["visits"],
            "ctaClickersDelta": deltas["ctaClickVisits"],
        },
        "totals": totals,
        "priorTotals": prior_totals,
        "deltas": deltas,
        "articles": article_rows,
        "categoriesSummary": category_counts,
        "sources": _source_mix(sources_window),
        "search": search_block,
        "notes": notes,
        "metricSemantics": {
            "milestones": "unique_visits",
            "engagedReaderRate": "Unique visits reaching 30 active seconds ÷ visits.",
            "ctaConversionRate": "Unique visits with a CTA click ÷ visits.",
            "ctaClickThroughRate": "Unique visits with a CTA click ÷ visits where a CTA was visible.",
        },
    }


def generate_article_performance_report(
    organization,
    report_date: date,
    *,
    force: bool = False,
) -> tuple[ArticlePerformanceReport, bool]:
    """Return the immutable report for (organization, report_date).

    An existing row is returned untouched unless ``force=True``, which rebuilds
    the payload in place (same row, same identity) for explicit ops repair.
    """
    existing = ArticlePerformanceReport.objects.filter(
        organization=organization,
        report_date=report_date,
    ).first()
    if existing and not force:
        return existing, False

    windows = report_windows(report_date)
    payload = build_article_performance_payload(organization, report_date)
    data_through_raw = payload.get("dataThroughDate")
    data_through = date.fromisoformat(data_through_raw) if data_through_raw else None
    fields = {
        "window_start": windows.window_start,
        "window_end": windows.window_end,
        "prior_window_start": windows.prior_window_start,
        "prior_window_end": windows.prior_window_end,
        "data_through_date": data_through,
        "schema_version": REPORT_SCHEMA_VERSION,
        "payload": payload,
    }
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.save(update_fields=list(fields.keys()))
        return existing, False
    try:
        report = ArticlePerformanceReport.objects.create(
            organization=organization,
            report_date=report_date,
            **fields,
        )
    except IntegrityError:
        # Concurrent generator won the unique constraint; the stored row wins.
        return (
            ArticlePerformanceReport.objects.get(
                organization=organization,
                report_date=report_date,
            ),
            False,
        )
    return report, True
