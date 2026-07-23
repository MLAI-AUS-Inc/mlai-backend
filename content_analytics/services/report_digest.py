"""Compact digest of the latest article-performance report for other messages.

The daily topic reminder (integrations.services.notification_adapters) leads
with this digest so founders see how their published articles are doing before
choosing the next topics to write. Channel-agnostic: plain values and
preformatted display strings only, no markup — each channel renders its own
framing. Import direction is one-way (this module never imports the
notification adapters), so the adapters can import it without a cycle.
"""
from __future__ import annotations

from typing import Any, Optional

from django.conf import settings
from django.utils import timezone

from content_analytics.models import (
    ArticlePerformanceReport,
    ArticlePerformanceReportCategory,
)

# A reminder must not lead with a week the founder has already moved past; if
# report generation has been failing this long, drop the section rather than
# resurrect stale numbers.
DIGEST_MAX_AGE_DAYS = 3
TOP_PAGE_LIMIT = 3
ATTENTION_PAGE_LIMIT = 3


def latest_report_digest(
    organization,
    *,
    max_age_days: int = DIGEST_MAX_AGE_DAYS,
) -> Optional[dict[str, Any]]:
    report = (
        ArticlePerformanceReport.objects.filter(organization=organization)
        .order_by("-report_date")
        .first()
    )
    if report is None:
        return None
    if (timezone.localdate() - report.report_date).days > max_age_days:
        return None
    return report_digest(report)


def report_digest(report: ArticlePerformanceReport) -> dict[str, Any]:
    payload = report.payload if isinstance(report.payload, dict) else {}
    headline = payload.get("headline") if isinstance(payload.get("headline"), dict) else {}
    articles = [row for row in (payload.get("articles") or []) if isinstance(row, dict)]
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    window_days = _int(window.get("days")) or 7

    human_visits = _int(headline.get("humanVisits"))
    cta_clickers = _int(headline.get("ctaClickers"))
    engaged_rate_display = _pct_display(headline.get("engagedReaderRate"))
    conversion_display = _pct_display(headline.get("ctaConversionRate"), decimals=1)
    visits_delta_display = _delta_display(headline.get("visitsDelta"))

    if human_visits > 0:
        summary_line = (
            f"{human_visits} visit{'s' if human_visits != 1 else ''}"
            f" ({visits_delta_display} vs prior)"
            f" · {engaged_rate_display} engaged"
            f" · {cta_clickers} CTA clicker{'s' if cta_clickers != 1 else ''}"
        )
    else:
        summary_line = f"No measured visits in the last {window_days} days yet."

    top_pages: list[dict[str, Any]] = []
    for row in articles:  # payload order is visits-desc
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        if _int(metrics.get("visits")) <= 0:
            break
        top_pages.append(
            {
                "rank": len(top_pages) + 1,
                "title": _row_title(row),
                "url": _https_url(row.get("canonicalUrl")),
                "summary": _page_summary(row),
            }
        )
        if len(top_pages) >= TOP_PAGE_LIMIT:
            break

    attention_rows = [
        row
        for row in articles
        if str(row.get("category") or "") == ArticlePerformanceReportCategory.NEEDS_ATTENTION
    ]
    attention_pages = [
        {
            "title": _row_title(row),
            "url": _https_url(row.get("canonicalUrl")),
            "summary": _attention_summary(row),
        }
        for row in attention_rows[:ATTENTION_PAGE_LIMIT]
    ]

    return {
        "domain": report.organization.domain,
        "report_date": report.report_date.isoformat(),
        "window_days": window_days,
        "human_visits": human_visits,
        "visits_delta_display": visits_delta_display,
        "engaged_rate_display": engaged_rate_display,
        "cta_clickers": cta_clickers,
        "conversion_display": conversion_display,
        "summary_line": summary_line,
        "top_pages": top_pages,
        "attention_pages": attention_pages,
        "extra_attention_count": max(len(attention_rows) - ATTENTION_PAGE_LIMIT, 0),
        "brief_url": brief_url(),
    }


def brief_url() -> str:
    base_url = str(getattr(settings, "FOUNDER_TOOLS_URL", "") or "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}/founder-tools/marketing#analytics"


def _row_title(row: dict[str, Any]) -> str:
    return str(row.get("title") or row.get("slug") or "Untitled article").strip()


def _page_summary(row: dict[str, Any]) -> str:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    visits = _int(metrics.get("visits"))
    lead = f"{visits} visit{'s' if visits != 1 else ''}"
    delta = _int(row.get("visitsDelta"))
    # A delta only means something once the page has a prior week to compare;
    # a brand-new page's "+visits" delta is noise.
    if _int(row.get("priorVisits")) > 0 and delta:
        lead += f" ({_delta_display(delta)})"
    parts = [lead]
    if _int(metrics.get("engaged30Visits")) > 0:
        parts.append(f"{_pct_display(metrics.get('engagedReaderRate'))} engaged")
    cta_clicks = _int(metrics.get("ctaClickVisits"))
    if cta_clicks > 0:
        parts.append(f"{cta_clicks} CTA click{'s' if cta_clicks != 1 else ''}")
    category = str(row.get("category") or "")
    if category in (
        ArticlePerformanceReportCategory.TOP_PERFORMER,
        ArticlePerformanceReportCategory.HIGH_INTEREST,
    ):
        label = str(row.get("categoryLabel") or "").strip()
        if label:
            parts.append(label)
    return " · ".join(parts)


def _attention_summary(row: dict[str, Any]) -> str:
    reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
    for reason in reasons:
        text = str(reason or "").strip()
        if text:
            return text
    return str(row.get("categoryLabel") or "Needs attention")


def _https_url(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw.startswith("https://") and len(raw) <= 2048 else ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pct_display(rate: Any, *, decimals: int = 0) -> str:
    try:
        value = float(rate or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return f"{value * 100:.{decimals}f}%"


def _delta_display(value: Any) -> str:
    delta = _int(value)
    return f"+{delta}" if delta > 0 else str(delta)
