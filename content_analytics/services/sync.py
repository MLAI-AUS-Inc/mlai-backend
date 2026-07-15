from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    AnalyticsSyncSource,
    AnalyticsSyncState,
    AnalyticsSyncStatus,
    ArticleAnalyticsLocation,
    ArticleBehaviorDaily,
    ArticleSearchDaily,
    ArticleSearchQueryDaily,
    ArticleTrafficSourceDaily,
    SearchConsoleProperty,
    SearchConsolePropertyStatus,
)
from content_analytics.services.locations import (
    location_for_day,
    location_windows,
    reconcile_article_locations,
)
from content_analytics.services.search_console import (
    fetch_article_window as fetch_search_console_article_window,
    service_for_property as service_for_search_console_property,
)
from content_analytics.services.umami import UmamiClient, classify_referrer, normalize_domain, normalize_path
from content_factory.models import ArticlePublishStatus, WrittenArticle


SYNC_LEASE_MINUTES = 30


def article_canonical_path(article: WrittenArticle) -> str:
    path = normalize_path(article.canonical_path)
    if path:
        return path
    expected_domain = normalize_domain(article.organization.domain)
    for value in (article.canonical_url, article.live_url):
        raw = str(value or "").strip()
        if not raw:
            continue
        parsed = urlsplit(raw)
        if normalize_domain(parsed.hostname or "") == expected_domain:
            return normalize_path(parsed.path)
    return ""


def _locations_by_article(
    articles: list[WrittenArticle],
    *,
    start_date: date,
    end_date: date,
) -> dict[object, list[ArticleAnalyticsLocation]]:
    """Reconcile signal-bypassing writes, then fetch one bounded timeline set."""

    reconcile_article_locations(articles)
    locations = ArticleAnalyticsLocation.objects.filter(
        article_id__in=[article.pk for article in articles],
        valid_from__lte=end_date,
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=start_date)).order_by(
        "article_id", "valid_from", "id"
    )
    by_article: dict[object, list[ArticleAnalyticsLocation]] = {}
    for location in locations:
        by_article.setdefault(location.article_id, []).append(location)
    return by_article


def _claim_state(organization, source: str, *, force: bool = False) -> AnalyticsSyncState | None:
    now = timezone.now()
    with transaction.atomic():
        state, _created = AnalyticsSyncState.objects.select_for_update().get_or_create(
            organization=organization,
            source=source,
        )
        if not force and state.lease_expires_at and state.lease_expires_at > now:
            return None
        state.status = AnalyticsSyncStatus.RUNNING
        state.lease_expires_at = now + timedelta(minutes=SYNC_LEASE_MINUTES)
        state.last_attempted_at = now
        state.last_error = ""
        state.save(
            update_fields=["status", "lease_expires_at", "last_attempted_at", "last_error", "updated_at"]
        )
        return state


def _finish_state(state: AnalyticsSyncState, *, end_date: date, cursor: dict | None = None) -> None:
    state.status = AnalyticsSyncStatus.SUCCEEDED
    state.lease_expires_at = None
    state.synced_through = end_date
    state.last_completed_at = timezone.now()
    state.last_error = ""
    if cursor is not None:
        state.cursor = cursor
    state.save(
        update_fields=[
            "status",
            "lease_expires_at",
            "synced_through",
            "last_completed_at",
            "last_error",
            "cursor",
            "updated_at",
        ]
    )


def _fail_state(state: AnalyticsSyncState, exc: Exception) -> None:
    state.status = AnalyticsSyncStatus.FAILED
    state.lease_expires_at = None
    state.last_error = str(exc)
    state.save(update_fields=["status", "lease_expires_at", "last_error", "updated_at"])


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _source_defaults(row: dict) -> dict[str, int | bool]:
    def as_int(value):
        try:
            return max(int(float(value or 0)), 0)
        except (TypeError, ValueError):
            return 0

    return {
        "pageviews": as_int(row.get("pageviews")),
        "visitors": as_int(row.get("visitors")),
        "visits": as_int(row.get("visits")),
        "cta_impression_count": as_int(row.get("cta_impression_count")),
        "cta_click_count": as_int(row.get("cta_click_count")),
        "conversion_attribution_available": bool(row.get("conversion_attribution_available")),
    }


def sync_umami_site(
    site: AnalyticsSite,
    *,
    start_date: date,
    end_date: date,
    client: UmamiClient | None = None,
) -> dict:
    if not site.enabled or site.provision_status != AnalyticsProvisionStatus.PROVISIONED:
        return {"source": "umami", "status": "skipped", "reason": "site_not_enabled"}
    if not site.external_website_id:
        return {"source": "umami", "status": "skipped", "reason": "site_not_provisioned"}
    client = client or UmamiClient()
    articles = list(
        WrittenArticle.objects.filter(
            organization=site.organization,
            publish_status=ArticlePublishStatus.LIVE,
        ).order_by("created_at")
    )
    locations_by_article = _locations_by_article(
        articles,
        start_date=start_date,
        end_date=end_date,
    )
    synced_rows = 0
    skipped_articles = 0
    for article in articles:
        article_locations = locations_by_article.get(article.pk, [])
        if not article_locations:
            skipped_articles += 1
            continue
        article_synced = False
        for day in _days(start_date, end_date):
            location = location_for_day(article, day, locations=article_locations)
            if location is None:
                continue
            result = client.fetch_article_day(
                site.external_website_id,
                path=location.canonical_path,
                day=day,
            )
            ArticleBehaviorDaily.objects.update_or_create(
                article=article,
                date=day,
                defaults={
                    "organization": site.organization,
                    **result.stats,
                    **result.milestones,
                    "source_updated_at": timezone.now(),
                },
            )
            with transaction.atomic():
                ArticleTrafficSourceDaily.objects.filter(article=article, date=day).delete()
                attributed_visits = 0
                attributed_impressions = 0
                attributed_clicks = 0
                source_payloads = {}
                for row in result.referrers:
                    raw_name = str(row.get("name") or row.get("x") or "").strip()
                    category, source_name = classify_referrer(raw_name)
                    defaults = _source_defaults(row)
                    key = (category, source_name)
                    aggregate = source_payloads.setdefault(
                        key,
                        {
                            "pageviews": 0,
                            "visitors": 0,
                            "visits": 0,
                            "cta_impression_count": 0,
                            "cta_click_count": 0,
                            "conversion_attribution_available": True,
                        },
                    )
                    metric_names = ("pageviews", "visitors", "visits", "cta_impression_count", "cta_click_count")
                    if row.get("evidence_kind") == "utm":
                        # Referrer and UTM can describe the same visit. Merge UTM
                        # evidence as a conservative union lower-bound, never sum it.
                        for metric in metric_names:
                            aggregate[metric] = max(aggregate[metric], int(defaults[metric]))
                        aggregate["conversion_attribution_available"] = bool(
                            aggregate["conversion_attribution_available"]
                            and defaults["conversion_attribution_available"]
                        )
                    else:
                        for metric in metric_names:
                            aggregate[metric] += int(defaults[metric])
                        aggregate["conversion_attribution_available"] = bool(
                            aggregate["conversion_attribution_available"]
                            and defaults["conversion_attribution_available"]
                        )
                # Defensive allocation cap: unioned source visits/pageviews never
                # exceed the article totals. This also bounds Direct residuals.
                remaining_visits = int(result.stats.get("visits") or 0)
                remaining_pageviews = int(result.stats.get("pageviews") or 0)
                remaining_impressions = int(result.milestones.get("cta_impression_count") or 0)
                remaining_clicks = int(result.milestones.get("cta_click_count") or 0)
                ordered_sources = sorted(
                    source_payloads.items(),
                    key=lambda item: (-int(item[1]["visits"]), item[0][0], item[0][1]),
                )
                for key, aggregate in ordered_sources:
                    if key[0] == "direct":
                        continue
                    aggregate["visits"] = min(int(aggregate["visits"]), remaining_visits)
                    remaining_visits -= int(aggregate["visits"])
                    aggregate["pageviews"] = min(int(aggregate["pageviews"]), remaining_pageviews)
                    remaining_pageviews -= int(aggregate["pageviews"])
                    if aggregate["conversion_attribution_available"]:
                        aggregate["cta_impression_count"] = min(
                            int(aggregate["cta_impression_count"]),
                            remaining_impressions,
                        )
                        aggregate["cta_click_count"] = min(
                            int(aggregate["cta_click_count"]),
                            remaining_clicks,
                        )
                        remaining_impressions -= int(aggregate["cta_impression_count"])
                        remaining_clicks -= int(aggregate["cta_click_count"])
                        attributed_impressions += int(aggregate["cta_impression_count"])
                        attributed_clicks += int(aggregate["cta_click_count"])
                attributed_visits = sum(int(payload["visits"]) for payload in source_payloads.values())
                direct_visits = max(int(result.stats.get("visits") or 0) - attributed_visits, 0)
                direct_key = ("direct", "Direct or unknown")
                if direct_visits or direct_key in source_payloads or result.source_attribution_complete:
                    direct = source_payloads.setdefault(
                        direct_key,
                        {
                            "pageviews": 0,
                            "visitors": 0,
                            "visits": 0,
                            "cta_impression_count": 0,
                            "cta_click_count": 0,
                            "conversion_attribution_available": False,
                        },
                    )
                    non_direct_visits = sum(
                        int(payload["visits"])
                        for key, payload in source_payloads.items()
                        if key != direct_key
                    )
                    direct["visits"] = min(
                        int(direct["visits"]) + direct_visits,
                        max(int(result.stats.get("visits") or 0) - non_direct_visits, 0),
                    )
                    if result.source_attribution_complete:
                        direct["cta_impression_count"] = max(
                            int(result.milestones.get("cta_impression_count") or 0) - attributed_impressions,
                            0,
                        )
                        direct["cta_click_count"] = max(
                            int(result.milestones.get("cta_click_count") or 0) - attributed_clicks,
                            0,
                        )
                        direct["conversion_attribution_available"] = True
                    else:
                        direct["cta_impression_count"] = 0
                        direct["cta_click_count"] = 0
                        direct["conversion_attribution_available"] = False
                source_rows = [
                    ArticleTrafficSourceDaily(
                        organization=site.organization,
                        article=article,
                        date=day,
                        source_category=category,
                        source_name=source_name,
                        **defaults,
                    )
                    for (category, source_name), defaults in source_payloads.items()
                ]
                ArticleTrafficSourceDaily.objects.bulk_create(source_rows)
            synced_rows += 1
            article_synced = True
        if not article_synced:
            skipped_articles += 1
    site.last_synced_at = timezone.now()
    site.last_error = ""
    site.save(update_fields=["last_synced_at", "last_error", "updated_at"])
    return {
        "source": "umami",
        "status": "succeeded",
        "articles": len(articles),
        "skipped_articles": skipped_articles,
        "daily_rows": synced_rows,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def sync_search_console_property(
    prop: SearchConsoleProperty,
    *,
    start_date: date,
    end_date: date,
) -> dict:
    if not prop.sync_enabled or prop.status != SearchConsolePropertyStatus.VERIFIED:
        return {"source": "search_console", "status": "skipped", "reason": "property_not_enabled"}
    articles = list(
        WrittenArticle.objects.filter(
            organization=prop.organization,
            publish_status=ArticlePublishStatus.LIVE,
        ).order_by("created_at")
    )
    locations_by_article = _locations_by_article(
        articles,
        start_date=start_date,
        end_date=end_date,
    )
    synced_rows = 0
    skipped_articles = 0
    search_console_service = None
    for article in articles:
        article_windows = location_windows(
            article,
            start_date,
            end_date,
            locations=locations_by_article.get(article.pk, []),
        )
        if not article_windows:
            skipped_articles += 1
            continue
        if search_console_service is None:
            search_console_service = service_for_search_console_property(prop)
        results_by_day = {}
        for article_window in article_windows:
            window = fetch_search_console_article_window(
                prop,
                article,
                article_window.start_date,
                article_window.end_date,
                canonical_url=article_window.location.canonical_url,
                service=search_console_service,
            )
            results_by_day.update(window.days)
        query_records = []
        synced_days = sorted(results_by_day)
        with transaction.atomic():
            for day in synced_days:
                result = results_by_day[day]
                # This is the aggregate grain only. Country/device rows, if added in
                # future, must use non-empty dimensions and never enter headline sums.
                ArticleSearchDaily.objects.update_or_create(
                    article=article,
                    date=day,
                    engine="google",
                    surface="web",
                    country="",
                    device="",
                    defaults={"organization": prop.organization, **result.aggregate},
                )
                query_records.extend(
                    ArticleSearchQueryDaily(
                        organization=prop.organization,
                        article=article,
                        date=day,
                        engine="google",
                        surface="web",
                        **query,
                    )
                    for query in result.queries
                )
                synced_rows += 1
            ArticleSearchQueryDaily.objects.filter(
                article=article,
                date__in=synced_days,
                engine="google",
                surface="web",
            ).delete()
            ArticleSearchQueryDaily.objects.bulk_create(query_records)
    prop.last_synced_at = timezone.now()
    prop.last_error = ""
    prop.save(update_fields=["last_synced_at", "last_error", "updated_at"])
    return {
        "source": "search_console",
        "status": "succeeded",
        "articles": len(articles),
        "skipped_articles": skipped_articles,
        "daily_rows": synced_rows,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _gsc_windows(state: AnalyticsSyncState, *, end_date: date, explicit_days: int | None) -> tuple[list[tuple[date, date]], dict]:
    cursor = dict(state.cursor or {})
    if explicit_days:
        return [(end_date - timedelta(days=max(1, explicit_days) - 1), end_date)], cursor
    lookback = max(1, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_LOOKBACK_DAYS", 3)))
    rolling_start = end_date - timedelta(days=lookback - 1)
    windows = [(rolling_start, end_date)]

    retention_days = max(lookback, int(getattr(settings, "CONTENT_ANALYTICS_GSC_INITIAL_BACKFILL_DAYS", 480)))
    target_start = end_date - timedelta(days=retention_days - 1)
    backfill_before = parse_date(str(cursor.get("backfill_before") or "")) or (rolling_start - timedelta(days=1))
    if backfill_before >= target_start:
        chunk_days = max(1, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_MAX_BACKFILL_DAYS_PER_RUN", 7)))
        chunk_start = max(target_start, backfill_before - timedelta(days=chunk_days - 1))
        windows.append((chunk_start, backfill_before))
        cursor["backfill_before"] = (chunk_start - timedelta(days=1)).isoformat()
        cursor["backfill_target"] = target_start.isoformat()
        cursor["backfill_complete"] = False
    else:
        cursor.pop("backfill_before", None)
        cursor["backfill_target"] = target_start.isoformat()
        cursor["backfill_complete"] = True
    return windows, cursor


def _umami_windows(
    state: AnalyticsSyncState,
    *,
    end_date: date,
    explicit_days: int | None,
    first_available_on: date | None = None,
) -> tuple[list[tuple[date, date]], dict]:
    """Build a rolling window plus one bounded outage-recovery chunk."""

    cursor = dict(state.cursor or {})
    if explicit_days:
        start_date = end_date - timedelta(days=max(1, explicit_days) - 1)
        return [(start_date, end_date)], cursor

    lookback = max(1, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_LOOKBACK_DAYS", 3)))
    rolling_start = end_date - timedelta(days=lookback - 1)
    windows = [(rolling_start, end_date)]
    gap_end = rolling_start - timedelta(days=1)

    catchup_next = parse_date(str(cursor.get("umami_catchup_next") or ""))
    if catchup_next is None and state.synced_through and state.synced_through < gap_end:
        catchup_next = state.synced_through + timedelta(days=1)
    if catchup_next is None and first_available_on and first_available_on <= gap_end:
        # A newly-created sync state has no synced_through cursor. Seed its
        # first bounded catch-up from when collection could first have existed
        # instead of silently skipping everything before the rolling window.
        catchup_next = first_available_on
    if catchup_next is None or catchup_next > gap_end:
        cursor.pop("umami_catchup_next", None)
        cursor["umami_catchup_complete"] = True
        return windows, cursor

    retention_days = max(
        lookback,
        int(getattr(settings, "CONTENT_ANALYTICS_UMAMI_RETENTION_DAYS", 120)),
    )
    retention_start = end_date - timedelta(days=retention_days - 1)
    catchup_next = max(catchup_next, retention_start)
    if catchup_next > gap_end:
        cursor.pop("umami_catchup_next", None)
        cursor["umami_catchup_complete"] = True
        return windows, cursor
    chunk_days = max(
        1,
        int(getattr(settings, "CONTENT_ANALYTICS_SYNC_MAX_BACKFILL_DAYS_PER_RUN", 30)),
    )
    chunk_end = min(catchup_next + timedelta(days=chunk_days - 1), gap_end)
    windows.append((catchup_next, chunk_end))
    next_day = chunk_end + timedelta(days=1)
    if next_day <= gap_end:
        cursor["umami_catchup_next"] = next_day.isoformat()
        cursor["umami_catchup_complete"] = False
    else:
        cursor.pop("umami_catchup_next", None)
        cursor["umami_catchup_complete"] = True
    return windows, cursor


def sync_organization_analytics(
    organization,
    *,
    source: str,
    days: int | None = None,
    force: bool = False,
) -> dict:
    state = _claim_state(organization, source, force=force)
    if state is None:
        return {"source": source, "status": "skipped", "reason": "sync_already_running"}
    try:
        today = timezone.now().date()
        if source == AnalyticsSyncSource.UMAMI:
            end_date = today - timedelta(days=1)
            site = AnalyticsSite.objects.get(organization=organization)
            first_available_at = site.last_provisioned_at or site.created_at
            first_available_on = (
                timezone.localtime(first_available_at).date()
                if first_available_at and timezone.is_aware(first_available_at)
                else first_available_at.date() if first_available_at else None
            )
            windows, cursor = _umami_windows(
                state,
                end_date=end_date,
                explicit_days=days,
                first_available_on=first_available_on,
            )
            results = [sync_umami_site(site, start_date=start, end_date=end) for start, end in windows]
            if not cursor.get("umami_catchup_complete", True):
                interval = max(60, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_INTERVAL_SECONDS", 86400)))
                AnalyticsSite.objects.filter(pk=site.pk).update(
                    last_synced_at=timezone.now() - timedelta(seconds=interval)
                )
            result = {"source": source, "status": "succeeded", "windows": results}
        elif source == AnalyticsSyncSource.SEARCH_CONSOLE:
            lag_days = max(1, int(getattr(settings, "CONTENT_ANALYTICS_GSC_FINALIZATION_LAG_DAYS", 3)))
            end_date = today - timedelta(days=lag_days)
            prop = SearchConsoleProperty.objects.get(organization=organization)
            windows, cursor = _gsc_windows(state, end_date=end_date, explicit_days=days)
            results = [sync_search_console_property(prop, start_date=start, end_date=end) for start, end in windows]
            if not cursor.get("backfill_complete"):
                interval = max(60, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_INTERVAL_SECONDS", 86400)))
                SearchConsoleProperty.objects.filter(pk=prop.pk).update(
                    last_synced_at=timezone.now() - timedelta(seconds=interval)
                )
            result = {"source": source, "status": "succeeded", "windows": results}
        else:
            raise ValueError(f"Unsupported analytics sync source: {source}")
        _finish_state(state, end_date=end_date, cursor=cursor)
        return result
    except Exception as exc:
        _fail_state(state, exc)
        if source == AnalyticsSyncSource.UMAMI:
            AnalyticsSite.objects.filter(organization=organization).update(last_error=str(exc))
        elif source == AnalyticsSyncSource.SEARCH_CONSOLE:
            SearchConsoleProperty.objects.filter(organization=organization).update(last_error=str(exc))
        raise


def sync_due_analytics(*, source: str = "", limit: int = 10, days: int | None = None, force: bool = False) -> dict:
    sources = [source] if source else [AnalyticsSyncSource.UMAMI, AnalyticsSyncSource.SEARCH_CONSOLE]
    results = []
    failures = []
    due_before = timezone.now() - timedelta(
        seconds=max(60, int(getattr(settings, "CONTENT_ANALYTICS_SYNC_INTERVAL_SECONDS", 86400)))
    )
    for source_name in sources:
        if source_name == AnalyticsSyncSource.UMAMI:
            queryset = AnalyticsSite.objects.select_related("organization").filter(
                enabled=True,
                provision_status=AnalyticsProvisionStatus.PROVISIONED,
            )
            if not force:
                queryset = queryset.filter(Q(last_synced_at__isnull=True) | Q(last_synced_at__lte=due_before))
            organizations = [
                site.organization
                for site in queryset.order_by(F("last_synced_at").asc(nulls_first=True), "id")[: max(1, limit)]
            ]
        else:
            queryset = SearchConsoleProperty.objects.select_related("organization").filter(
                sync_enabled=True,
                status=SearchConsolePropertyStatus.VERIFIED,
            )
            if not force:
                queryset = queryset.filter(Q(last_synced_at__isnull=True) | Q(last_synced_at__lte=due_before))
            organizations = [
                prop.organization
                for prop in queryset.order_by(F("last_synced_at").asc(nulls_first=True), "id")[: max(1, limit)]
            ]
        for organization in organizations:
            try:
                results.append(
                    {
                        "organization_id": organization.id,
                        **sync_organization_analytics(
                            organization,
                            source=source_name,
                            days=days,
                            force=force,
                        ),
                    }
                )
            except Exception as exc:
                failures.append(
                    {"organization_id": organization.id, "source": source_name, "error": str(exc)}
                )
    return {"status": "partial" if failures else "succeeded", "results": results, "failures": failures}
