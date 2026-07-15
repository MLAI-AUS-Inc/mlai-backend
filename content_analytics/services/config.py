from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify
from urllib.parse import urlsplit

from content_analytics.models import AnalyticsProvisionStatus, AnalyticsSite
from content_analytics.services.umami import UmamiClient, normalize_domain
from content_factory.models import ArticlePublishStatus, WrittenArticle


TRACKING_EVENTS = {
    "engaged_30": "cf_engaged_30s",
    "scroll_50": "cf_scroll_50",
    "scroll_90": "cf_scroll_90",
    "cta_impression": "cf_cta_impression",
    "cta_click": "cf_cta_click",
}
ARTICLE_ANALYTICS_MANIFEST_MAX_ENTRIES = 500


def analytics_article_manifest(organization, *, limit: int | None = None) -> list[dict[str, str]]:
    """Return a bounded, public-safe identity map for existing articles.

    Article-system setup uses this to backfill the generated registry when
    analytics is added to an existing site. Live rows take priority if an
    unusually large account reaches the manifest cap.
    """

    configured_limit = int(
        limit
        if limit is not None
        else getattr(settings, "CONTENT_ANALYTICS_ARTICLE_MANIFEST_LIMIT", 500)
    )
    effective_limit = min(max(configured_limit, 0), ARTICLE_ANALYTICS_MANIFEST_MAX_ENTRIES)
    if not effective_limit:
        return []
    # Setup reads the repository's default branch. PR-open and written-only
    # articles normally are not present there, so including them would produce
    # a permanently partial manifest and falsely mark a healthy live scaffold
    # stale. Merged and sitemap-confirmed live rows are the eligible set.
    queryset = WrittenArticle.objects.filter(
        organization=organization,
        publish_status__in=[ArticlePublishStatus.LIVE, ArticlePublishStatus.MERGED],
    ).exclude(slug="")
    live_rows = list(
        queryset.filter(publish_status=ArticlePublishStatus.LIVE)
        .order_by("-created_at", "slug")
        .values_list("slug", "category", "analytics_id")[:effective_limit]
    )
    remaining = effective_limit - len(live_rows)
    merged_rows = []
    if remaining:
        merged_rows = list(
            queryset.filter(publish_status=ArticlePublishStatus.MERGED)
            .order_by("-created_at", "slug")
            .values_list("slug", "category", "analytics_id")[:remaining]
        )
    manifest = []
    for raw_slug, raw_category, analytics_id in [*live_rows, *merged_rows]:
        article_slug = str(raw_slug or "").strip().strip("/")
        if "/" not in article_slug:
            category_slug = slugify(str(raw_category or "")) or "featured"
            article_slug = f"{category_slug}/{article_slug}"
        manifest.append(
            {"slug": article_slug, "analytics_article_id": str(analytics_id)}
        )
    return manifest


def umami_is_configured() -> bool:
    return bool(
        str(getattr(settings, "UMAMI_BASE_URL", "") or "").strip()
        and (
            str(getattr(settings, "UMAMI_API_TOKEN", "") or "").strip()
            or (
                str(getattr(settings, "UMAMI_USERNAME", "") or "").strip()
                and str(getattr(settings, "UMAMI_PASSWORD", "") or "").strip()
            )
        )
    )


def tracking_delivery_configuration_error() -> str:
    script_url = str(getattr(settings, "CONTENT_ANALYTICS_TRACKER_SCRIPT_URL", "") or "").strip()
    host_url = str(getattr(settings, "CONTENT_ANALYTICS_HOST_URL", "") or "").strip().rstrip("/")
    if not script_url or not host_url:
        return "CONTENT_ANALYTICS_TRACKER_SCRIPT_URL and CONTENT_ANALYTICS_HOST_URL are required."
    proxy_enabled = bool(getattr(settings, "CONTENT_ANALYTICS_FIRST_PARTY_PROXY_ENABLED", False))
    for label, value in (("tracker script", script_url), ("host", host_url)):
        if value.startswith("/"):
            if not proxy_enabled:
                return f"A relative analytics {label} URL requires CONTENT_ANALYTICS_FIRST_PARTY_PROXY_ENABLED=true."
            continue
        parsed = urlsplit(value)
        allowed_schemes = {"https"} if not getattr(settings, "DEBUG", False) else {"http", "https"}
        if parsed.scheme not in allowed_schemes or not parsed.netloc:
            return f"The analytics {label} URL must be an absolute {'HTTPS' if not getattr(settings, 'DEBUG', False) else 'HTTP(S)'} URL."
    return ""


def analytics_platform_is_ready() -> bool:
    return bool(umami_is_configured() and not tracking_delivery_configuration_error())


def public_analytics_config(organization, *, analytics_article_id=None) -> dict:
    try:
        site = organization.analytics_site
    except AnalyticsSite.DoesNotExist:
        site = None
    enabled = bool(
        site
        and site.enabled
        and site.provision_status == AnalyticsProvisionStatus.PROVISIONED
        and site.external_website_id
        and site.tracker_script_url
        and site.collector_url
    )
    apex_domain = normalize_domain(organization.domain) if organization else ""
    data_domains = list(dict.fromkeys([value for value in (apex_domain, f"www.{apex_domain}" if apex_domain else "") if value]))
    payload = {
        "schema_version": 1,
        "enabled": enabled,
        "provider": "umami",
        "website_id": site.external_website_id if enabled else "",
        "tracker_script_url": (
            site.tracker_script_url
            if site and site.tracker_script_url
            else str(getattr(settings, "CONTENT_ANALYTICS_TRACKER_SCRIPT_URL", "") or "")
        ),
        "collector_url": (
            site.collector_url
            if site and site.collector_url
            else str(getattr(settings, "CONTENT_ANALYTICS_HOST_URL", "") or "")
        ),
        "data_domains": data_domains,
        "events": TRACKING_EVENTS,
        "identify_visitors": False,
        "session_replay": False,
        "heatmaps": False,
    }
    if analytics_article_id:
        payload["analytics_article_id"] = str(analytics_article_id)
    return payload


def provision_analytics_site(organization) -> AnalyticsSite:
    domain = normalize_domain(organization.domain)
    tracker_script_url = str(getattr(settings, "CONTENT_ANALYTICS_TRACKER_SCRIPT_URL", "") or "").strip()
    host_url = str(getattr(settings, "CONTENT_ANALYTICS_HOST_URL", "") or "").strip().rstrip("/")
    configuration_error = tracking_delivery_configuration_error()
    if configuration_error:
        raise RuntimeError(configuration_error)
    site, _created = AnalyticsSite.objects.get_or_create(
        organization=organization,
        defaults={
            "domain": domain,
            "enabled": True,
            "tracker_script_url": tracker_script_url,
            "collector_url": host_url,
            "team_id": str(getattr(settings, "UMAMI_TEAM_ID", "") or ""),
        },
    )
    site.domain = domain
    site.enabled = True
    site.provision_status = AnalyticsProvisionStatus.PENDING
    site.last_error = ""
    site.tracker_script_url = tracker_script_url
    site.collector_url = host_url
    site.team_id = site.team_id or str(getattr(settings, "UMAMI_TEAM_ID", "") or "")
    site.save()
    try:
        remote = UmamiClient().ensure_website(name=organization.name or domain, domain=domain, team_id=site.team_id)
        website_id = str(remote.get("id") or "").strip()
        if not website_id:
            raise RuntimeError("Umami did not return a website id.")
        site.external_website_id = website_id
        site.provision_status = AnalyticsProvisionStatus.PROVISIONED
        site.last_provisioned_at = timezone.now()
        site.last_error = ""
        site.save(
            update_fields=[
                "external_website_id",
                "provision_status",
                "last_provisioned_at",
                "last_error",
                "updated_at",
            ]
        )
    except Exception as exc:
        site.provision_status = AnalyticsProvisionStatus.ERROR
        site.last_error = str(exc)
        site.save(update_fields=["provision_status", "last_error", "updated_at"])
        raise
    return site


def analytics_config_for_content_factory(
    organization,
    *,
    analytics_article_id=None,
    provision_if_missing: bool = False,
) -> dict:
    site = AnalyticsSite.objects.filter(organization=organization).first()
    should_provision = bool(
        provision_if_missing
        and analytics_platform_is_ready()
        and (
            site is None
            or (
                site.enabled
                and site.provision_status in {AnalyticsProvisionStatus.PENDING, AnalyticsProvisionStatus.ERROR}
            )
        )
    )
    if should_provision:
        try:
            provision_analytics_site(organization)
        except Exception:
            # Analytics is intentionally non-blocking for article setup and
            # generation. The site row/status retains the actionable error.
            pass
    return public_analytics_config(organization, analytics_article_id=analytics_article_id)


def disable_analytics_site(organization) -> AnalyticsSite | None:
    site = AnalyticsSite.objects.filter(organization=organization).first()
    if not site:
        return None
    site.enabled = False
    site.provision_status = AnalyticsProvisionStatus.DISABLED
    site.save(update_fields=["enabled", "provision_status", "updated_at"])
    return site
