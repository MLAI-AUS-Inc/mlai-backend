from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from content_analytics.models import (
    ArticleAnalyticsLocation,
    ArticleAnalyticsLocationSource,
)
from content_analytics.services.umami import normalize_domain, normalize_path
from content_factory.models import ArticlePublishStatus, WrittenArticle


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleLocationWindow:
    location: ArticleAnalyticsLocation
    start_date: date
    end_date: date


def _clean_url(raw_url: str, *, expected_domain: str) -> tuple[str, str] | None:
    text = str(raw_url or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if normalize_domain(parsed.hostname) != expected_domain:
        return None
    exact_path = parsed.path or "/"
    canonical_url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), exact_path, "", ""))
    return canonical_url, normalize_path(exact_path)


def canonical_location_values(article: WrittenArticle) -> tuple[str, str, str, object | None] | None:
    """Return a same-domain URL/path pair and its evidence metadata.

    Sitemap-confirmed ``live_url`` wins when available. Query strings and
    fragments are intentionally removed from canonical URLs. Umami paths use
    the same decodeURI/trailing-slash normalization as its event store, while
    the Search Console URL preserves the canonical path spelling.
    """

    expected_domain = normalize_domain(getattr(article.organization, "domain", ""))
    if not expected_domain:
        return None

    candidates: list[tuple[str, str, object | None]] = []
    if article.publish_status == ArticlePublishStatus.LIVE and article.live_url:
        candidates.append(
            (
                str(article.live_url),
                ArticleAnalyticsLocationSource.SITEMAP,
                article.live_verified_at,
            )
        )
    if article.canonical_url:
        candidates.append(
            (
                str(article.canonical_url),
                ArticleAnalyticsLocationSource.CANONICAL,
                None,
            )
        )
    if article.live_url:
        candidates.append(
            (
                str(article.live_url),
                ArticleAnalyticsLocationSource.SITEMAP,
                article.live_verified_at,
            )
        )
    for raw_url, source, confirmed_at in candidates:
        cleaned = _clean_url(raw_url, expected_domain=expected_domain)
        if cleaned:
            canonical_url, canonical_path = cleaned
            return canonical_url, canonical_path, source, confirmed_at

    canonical_path = normalize_path(article.canonical_path)
    if not canonical_path:
        return None
    canonical_url = f"https://{expected_domain}{canonical_path}"
    return canonical_url, canonical_path, ArticleAnalyticsLocationSource.GENERATED, None


def initial_location_date(article: WrittenArticle) -> date:
    # The first known path is safe to use for the article row's whole lifetime:
    # no prior location exists that a rolling sync could accidentally erase.
    # This also lets a new installation recover already-recorded Umami/GSC data.
    for value in (article.created_at, article.published_at, article.live_verified_at):
        if value:
            return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return timezone.localdate()


def _source_rank(source: str) -> int:
    return {
        ArticleAnalyticsLocationSource.MIGRATION: 0,
        ArticleAnalyticsLocationSource.GENERATED: 1,
        ArticleAnalyticsLocationSource.CANONICAL: 2,
        ArticleAnalyticsLocationSource.SITEMAP: 3,
    }.get(str(source or ""), 0)


def record_article_location(
    article: WrittenArticle,
    *,
    effective_on: date | None = None,
    source: str = "",
) -> ArticleAnalyticsLocation | None:
    """Reconcile the article's current canonical location into its timeline.

    The parent article row is locked to serialize concurrent callbacks. A
    second location change on the same calendar day updates that day's row
    rather than closing it at ``day - 1`` and producing an invalid interval.
    """

    if not article or not article.pk:
        return None
    with transaction.atomic():
        current_article = (
            WrittenArticle.objects.select_for_update()
            .select_related("organization")
            .get(pk=article.pk)
        )
        values = canonical_location_values(current_article)
        if values is None:
            return None
        canonical_url, canonical_path, inferred_source, confirmed_at = values
        location_source = str(source or inferred_source)
        locations = ArticleAnalyticsLocation.objects.select_for_update().filter(
            article=current_article
        )
        active = locations.filter(valid_to__isnull=True).order_by("-valid_from", "-id").first()
        if effective_on is None:
            effective_on = timezone.localdate() if locations.exists() else initial_location_date(current_article)

        if active and active.canonical_url == canonical_url and active.canonical_path == canonical_path:
            update_fields = []
            if _source_rank(location_source) > _source_rank(active.source):
                active.source = location_source
                update_fields.append("source")
            if confirmed_at and (not active.confirmed_at or confirmed_at > active.confirmed_at):
                active.confirmed_at = confirmed_at
                update_fields.append("confirmed_at")
            if update_fields:
                active.save(update_fields=[*update_fields, "updated_at"])
            return active

        if active and active.valid_from > effective_on:
            # This can only arise from an explicitly backdated reconciliation.
            # Keep the already-valid timeline and treat it as a same-start
            # correction rather than manufacturing an overlapping interval.
            effective_on = active.valid_from

        if active and active.valid_from == effective_on:
            active.organization = current_article.organization
            active.canonical_url = canonical_url
            active.canonical_path = canonical_path
            active.source = location_source
            active.confirmed_at = confirmed_at
            active.save(
                update_fields=[
                    "organization",
                    "canonical_url",
                    "canonical_path",
                    "source",
                    "confirmed_at",
                    "updated_at",
                ]
            )
            return active

        if active:
            active.valid_to = effective_on - timedelta(days=1)
            active.save(update_fields=["valid_to", "updated_at"])

        same_start = locations.filter(valid_from=effective_on).first()
        if same_start:
            same_start.organization = current_article.organization
            same_start.canonical_url = canonical_url
            same_start.canonical_path = canonical_path
            same_start.valid_to = None
            same_start.source = location_source
            same_start.confirmed_at = confirmed_at
            same_start.save(
                update_fields=[
                    "organization",
                    "canonical_url",
                    "canonical_path",
                    "valid_to",
                    "source",
                    "confirmed_at",
                    "updated_at",
                ]
            )
            return same_start

        return ArticleAnalyticsLocation.objects.create(
            organization=current_article.organization,
            article=current_article,
            canonical_url=canonical_url,
            canonical_path=canonical_path,
            valid_from=effective_on,
            source=location_source,
            confirmed_at=confirmed_at,
        )


def reconcile_article_locations(articles: list[WrittenArticle]) -> None:
    """Best-effort safety net for updates that bypass Django model signals."""

    for article in articles:
        try:
            record_article_location(article)
        except Exception:
            logger.warning(
                "article_analytics_location_reconcile_failed article=%s",
                getattr(article, "pk", ""),
                exc_info=True,
            )


def location_for_day(
    article: WrittenArticle,
    day: date,
    *,
    locations: list[ArticleAnalyticsLocation] | None = None,
) -> ArticleAnalyticsLocation | None:
    candidates = locations
    if candidates is None:
        return (
            ArticleAnalyticsLocation.objects.filter(
                article=article,
                valid_from__lte=day,
            )
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day))
            .order_by("-valid_from", "-id")
            .first()
        )
    for location in sorted(candidates, key=lambda row: (row.valid_from, row.pk or 0), reverse=True):
        if location.valid_from <= day and (location.valid_to is None or location.valid_to >= day):
            return location
    return None


def location_windows(
    article: WrittenArticle,
    start_date: date,
    end_date: date,
    *,
    locations: list[ArticleAnalyticsLocation] | None = None,
) -> list[ArticleLocationWindow]:
    if end_date < start_date:
        raise ValueError("Analytics location window end date cannot be before its start date.")
    if locations is None:
        locations = list(
            ArticleAnalyticsLocation.objects.filter(
                article=article,
                valid_from__lte=end_date,
            )
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=start_date))
            .order_by("valid_from", "id")
        )
    windows = []
    for location in locations:
        window_start = max(start_date, location.valid_from)
        window_end = min(end_date, location.valid_to or end_date)
        if window_start <= window_end:
            windows.append(
                ArticleLocationWindow(
                    location=location,
                    start_date=window_start,
                    end_date=window_end,
                )
            )
    return sorted(windows, key=lambda window: (window.start_date, window.location.pk or 0))
