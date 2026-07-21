"""Self-throttling dispatcher for daily article-performance briefs.

Ticked every scheduler-loop pass (see ``run_scheduled_discovery``). Each
enabled analytics org gets at most one immutable report per org-local date,
generated after the configured local hour. The ``ArticlePerformanceReport``
unique constraint is the idempotency record, so a tick is a cheap existence
check per org once today's report exists.
"""
from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    ArticlePerformanceReport,
)
from content_analytics.services.reports import generate_article_performance_report
from content_factory.models import OrganizationContentConfig
from organizations.models import Organization

logger = logging.getLogger(__name__)

# Bound the per-tick detail list so the scheduler's stdout stays readable even
# with many orgs; counters remain exact.
MAX_RESULT_ROWS = 20


def reports_enabled() -> bool:
    return bool(getattr(settings, "CONTENT_ANALYTICS_REPORTS_ENABLED", False))


def _zone(name: str) -> ZoneInfo:
    candidate = str(name or "").strip()
    if candidate:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Invalid analytics report timezone %r; using default.", candidate)
    return ZoneInfo(settings.CONTENT_ANALYTICS_REPORT_DEFAULT_TIMEZONE)


def _timezones_by_organization(organization_ids) -> dict:
    rows = OrganizationContentConfig.objects.filter(
        organization_id__in=organization_ids
    ).values_list("organization_id", "default_timezone")
    return {org_id: tz for org_id, tz in rows}


def run_daily_article_report_scheduler(*, now=None) -> dict:
    """Generate due reports. Safe to tick every loop; never raises per-org."""
    if not reports_enabled():
        return {"status": "disabled", "generated": 0}

    now = now or timezone.now()
    sites = list(
        AnalyticsSite.objects.filter(
            enabled=True,
            provision_status=AnalyticsProvisionStatus.PROVISIONED,
        ).select_related("organization")
    )
    timezones = _timezones_by_organization([site.organization_id for site in sites])
    local_hour = int(settings.CONTENT_ANALYTICS_REPORT_LOCAL_HOUR)

    generated = existing = not_due = failed = 0
    results: list[dict] = []
    for site in sites:
        organization = site.organization
        local_now = now.astimezone(_zone(timezones.get(site.organization_id, "")))
        report_date = local_now.date()
        if local_now.hour < local_hour:
            not_due += 1
            continue
        if ArticlePerformanceReport.objects.filter(
            organization=organization, report_date=report_date
        ).exists():
            existing += 1
            continue
        try:
            _, created = generate_article_performance_report(organization, report_date)
        except Exception as exc:
            logger.exception(
                "Daily article report generation failed for %s.", organization.domain
            )
            failed += 1
            if len(results) < MAX_RESULT_ROWS:
                results.append(
                    {
                        "domain": organization.domain,
                        "report_date": report_date.isoformat(),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            continue
        if created:
            generated += 1
        else:
            existing += 1
        if len(results) < MAX_RESULT_ROWS:
            results.append(
                {
                    "domain": organization.domain,
                    "report_date": report_date.isoformat(),
                    "status": "generated" if created else "existing",
                }
            )

    return {
        "status": "ok",
        "generated": generated,
        "existing": existing,
        "not_due": not_due,
        "failed": failed,
        "results": results,
    }


def generate_report_for_domain(
    domain: str,
    *,
    report_date: date | None = None,
    force: bool = False,
) -> dict:
    """Manual/pilot generation for one org. Bypasses the kill switch and the
    local-hour gate — invoking it is the explicit operator intent."""
    organization = Organization.objects.filter(domain=str(domain or "").strip()).first()
    if organization is None:
        return {"status": "failed", "error": f"Unknown organization domain: {domain!r}"}
    if report_date is None:
        tz_name = (
            OrganizationContentConfig.objects.filter(organization=organization)
            .values_list("default_timezone", flat=True)
            .first()
        )
        report_date = timezone.now().astimezone(_zone(tz_name or "")).date()
    try:
        report, created = generate_article_performance_report(
            organization, report_date, force=force
        )
    except Exception as exc:
        logger.exception("Manual article report generation failed for %s.", domain)
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "generated" if created else ("regenerated" if force else "existing"),
        "domain": organization.domain,
        "report_date": report.report_date.isoformat(),
        "report_id": report.pk,
        "articles": len(report.payload.get("articles", [])),
    }
