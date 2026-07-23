from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from content_analytics.models import (
    AnalyticsProvisionStatus,
    AnalyticsSite,
    AnalyticsSyncSource,
    ArticlePerformanceReport,
    SearchConsoleProperty,
)
from content_analytics.services.config import (
    analytics_platform_is_ready,
    disable_analytics_site,
    provision_analytics_site,
    public_analytics_config,
    tracking_delivery_configuration_error,
    umami_is_configured,
)
from content_analytics.services.reporting import build_analytics_summary
from content_analytics.services.search_console import (
    SearchConsoleConfigurationError,
    SearchConsoleVerificationError,
    service_account_email,
    verify_search_console_property,
)
from content_analytics.services.sync import sync_organization_analytics
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


def _context(request):
    # Reuse the same active-company authorization boundary as every Vibe
    # Marketing endpoint. Import locally to avoid URL-import cycles.
    from content_factory.vibe_marketing_views import _resolve_context_or_response

    return _resolve_context_or_response(request)


def _scaffold_has_current_analytics(organization, config, site) -> bool:
    if not (config and config.articles_scaffolded and site and site.external_website_id):
        return False
    for run in ContentFactoryRun.objects.filter(
        domain=organization.domain,
        workflow="article_system_setup",
        status=ContentFactoryRunStatus.COMPLETED,
    ).order_by("-updated_at")[:10]:
        run_request = run.run_request if isinstance(run.run_request, dict) else {}
        analytics_config = run_request.get("analytics_config") if isinstance(run_request.get("analytics_config"), dict) else {}
        run_result = run.result if isinstance(run.result, dict) else {}
        setup_result = (
            run_result.get("article_system_setup")
            if isinstance(run_result.get("article_system_setup"), dict)
            else {}
        )
        seed_proof = (
            run_result.get("article_seed_proof")
            if isinstance(run_result.get("article_seed_proof"), dict)
            else setup_result.get("article_seed_proof")
            if isinstance(setup_result.get("article_seed_proof"), dict)
            else {}
        )
        analytics_proof = (
            seed_proof.get("analytics")
            if isinstance(seed_proof.get("analytics"), dict)
            else {}
        )
        manifest_proof = (
            analytics_proof.get("article_manifest")
            if isinstance(analytics_proof.get("article_manifest"), dict)
            else {}
        )
        try:
            manifest_requested = max(int(manifest_proof.get("requested_count") or 0), 0)
            manifest_applied = max(int(manifest_proof.get("applied_count") or 0), 0)
        except (TypeError, ValueError):
            manifest_requested = 1
            manifest_applied = 0
        requested_website_id = str(
            analytics_config.get("website_id") or analytics_config.get("websiteId") or ""
        )
        proof_website_id = str(
            analytics_proof.get("website_id") or analytics_proof.get("websiteId") or ""
        )
        if (
            analytics_config.get("enabled")
            and requested_website_id == site.external_website_id
            and analytics_proof.get("status") == "applied"
            and proof_website_id == site.external_website_id
            and manifest_applied >= manifest_requested
        ):
            return True
    return False


def analytics_status_payload(organization) -> dict:
    site = AnalyticsSite.objects.filter(organization=organization).first()
    gsc = SearchConsoleProperty.objects.filter(organization=organization).first()
    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    public_config = public_analytics_config(organization)
    scaffold_current = _scaffold_has_current_analytics(organization, config, site)
    platform_error = tracking_delivery_configuration_error()
    if not umami_is_configured():
        platform_error = "Umami management credentials are not configured."
    requires_scaffold_update = bool(
        site
        and site.enabled
        and site.provision_status == AnalyticsProvisionStatus.PROVISIONED
        and config
        and config.articles_scaffolded
        and not scaffold_current
    )
    if not site:
        overall_status = "not_enabled"
    elif not site.enabled:
        overall_status = "disabled"
    elif public_config.get("enabled"):
        overall_status = "ready"
    elif site.provision_status == AnalyticsProvisionStatus.ERROR:
        overall_status = "error"
    else:
        overall_status = "pending"
    message = (
        f"The tracking configuration exists, but synchronization is unavailable: {platform_error}"
        if overall_status == "ready" and scaffold_current and platform_error
        else "Article analytics are collecting and ready to report."
        if overall_status == "ready" and scaffold_current
        else "Analytics are provisioned, but the published article scaffold must be updated before collection is guaranteed."
        if overall_status == "ready" and requires_scaffold_update
        else site.last_error
        if site and site.last_error
        else platform_error
        if platform_error
        else "Article analytics are not enabled yet."
    )
    gsc_payload = {
        "status": gsc.status if gsc else "not_connected",
        "connected": bool(gsc and gsc.status == "verified"),
        "property": gsc.site_url if gsc else "",
        "siteUrl": gsc.site_url if gsc else "",
        "accessMethod": gsc.access_method if gsc else "service_account",
        "permissionLevel": gsc.permission_level if gsc else "",
        "serviceAccountEmail": service_account_email(),
        "lastVerifiedAt": gsc.last_verified_at if gsc else None,
        "lastSyncedAt": gsc.last_synced_at if gsc else None,
        "lastError": gsc.last_error if gsc else "",
        "message": (
            "Search Console is connected."
            if gsc and gsc.status == "verified"
            else gsc.last_error
            if gsc and gsc.last_error
            else f"Share the Search Console property with {service_account_email()}, then verify it here."
            if service_account_email()
            else "Connect Google with Search Console read-only access, then verify the property here."
        ),
    }
    return {
        "status": overall_status,
        "state": overall_status,
        "available": bool(analytics_platform_is_ready() or site),
        "enabled": bool(site and site.enabled),
        "collecting": bool(public_config.get("enabled") and scaffold_current),
        "provider": "umami",
        "lastSyncedAt": site.last_synced_at if site else None,
        "message": message,
        "platformReady": analytics_platform_is_ready(),
        "platformError": platform_error,
        "behavior": {
            "provider": "umami",
            "enabled": bool(site and site.enabled),
            "ready": bool(public_config.get("enabled")),
            "provisionStatus": site.provision_status if site else "not_started",
            "websiteId": site.external_website_id if site else "",
            "lastSyncedAt": site.last_synced_at if site else None,
            "lastError": site.last_error if site else "",
            "requiresScaffoldUpdate": requires_scaffold_update,
            "scaffoldHasCurrentAnalytics": scaffold_current,
            "collectionMayContinueOnDeployedPages": bool(site and not site.enabled and site.external_website_id),
            "disableSemantics": (
                "Disabling stops MLAI synchronization and removes analytics collection from future generated scaffolds. "
                "Already-deployed pages can continue anonymous collection until their scaffold is updated."
            ),
            "rawStoreSemantics": (
                "Umami owns anonymous raw session/event rows; mlai-backend stores only daily article aggregates."
            ),
        },
        "gsc": gsc_payload,
        "searchConsole": gsc_payload,
        "publicConfig": public_config,
    }


def _requested_range(request):
    today = timezone.now().date()
    default_end = today - timedelta(days=1)
    raw_range = str(request.query_params.get("range") or "").strip().lower()
    raw_days = request.query_params.get("days")
    range_label = ""
    if raw_range == "16m":
        raw_days = 480
        range_label = "16m"
    elif raw_range.endswith("d") and raw_range[:-1].isdigit():
        raw_days = raw_range[:-1]
    try:
        days = max(1, min(int(raw_days or 28), 730))
    except (TypeError, ValueError):
        days = 28
    end_date = parse_date(str(request.query_params.get("end") or request.query_params.get("endDate") or "")) or default_end
    start_date = parse_date(str(request.query_params.get("start") or request.query_params.get("startDate") or ""))
    start_date = start_date or (end_date - timedelta(days=days - 1))
    if start_date > end_date:
        raise ValueError("startDate must be on or before endDate.")
    if (end_date - start_date).days >= 730:
        raise ValueError("Analytics ranges cannot exceed 730 days.")
    return start_date, end_date, range_label or f"{(end_date - start_date).days + 1}d"


class VibeMarketingAnalyticsStatusView(APIView):
    def get(self, request):
        context, error = _context(request)
        if error:
            return error
        return Response(analytics_status_payload(context.organization), status=status.HTTP_200_OK)


class VibeMarketingAnalyticsEnableView(APIView):
    def post(self, request):
        context, error = _context(request)
        if error:
            return error
        try:
            provision_analytics_site(context.organization)
        except Exception as exc:
            payload = analytics_status_payload(context.organization)
            payload["detail"] = str(exc)
            return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(analytics_status_payload(context.organization), status=status.HTTP_200_OK)


class VibeMarketingAnalyticsDisableView(APIView):
    def post(self, request):
        context, error = _context(request)
        if error:
            return error
        disable_analytics_site(context.organization)
        return Response(analytics_status_payload(context.organization), status=status.HTTP_200_OK)


class VibeMarketingAnalyticsSummaryView(APIView):
    def get(self, request):
        context, error = _context(request)
        if error:
            return error
        try:
            start_date, end_date, range_label = _requested_range(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        payload = build_analytics_summary(
            context.organization,
            start_date=start_date,
            end_date=end_date,
        )
        payload["range"] = range_label
        payload["status"] = analytics_status_payload(context.organization)
        return Response(payload, status=status.HTTP_200_OK)


class VibeMarketingArticleAnalyticsView(APIView):
    def get(self, request, article_id):
        context, error = _context(request)
        if error:
            return error
        article = WrittenArticle.objects.filter(
            organization=context.organization,
            id=article_id,
            publish_status=ArticlePublishStatus.LIVE,
        ).first()
        if not article:
            return Response({"detail": "Tracked live article not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            start_date, end_date, range_label = _requested_range(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        payload = build_analytics_summary(
            context.organization,
            start_date=start_date,
            end_date=end_date,
            article=article,
        )
        payload["range"] = range_label
        payload["article"] = payload["articles"][0] if payload.get("articles") else None
        payload["status"] = analytics_status_payload(context.organization)
        return Response(payload, status=status.HTTP_200_OK)


class VibeMarketingSearchConsoleVerifyView(APIView):
    def post(self, request):
        context, error = _context(request)
        if error:
            return error
        try:
            verify_search_console_property(
                organization=context.organization,
                requested_site_url=str(request.data.get("siteUrl") or request.data.get("site_url") or ""),
                access_method=str(request.data.get("accessMethod") or request.data.get("access_method") or ""),
                user=request.user,
            )
        except (SearchConsoleConfigurationError, SearchConsoleVerificationError) as exc:
            payload = analytics_status_payload(context.organization)
            payload["detail"] = str(exc)
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            payload = analytics_status_payload(context.organization)
            payload["detail"] = f"Search Console verification failed: {exc}"
            return Response(payload, status=status.HTTP_502_BAD_GATEWAY)
        return Response(analytics_status_payload(context.organization), status=status.HTTP_200_OK)


class VibeMarketingAnalyticsSyncView(APIView):
    def post(self, request):
        context, error = _context(request)
        if error:
            return error
        requested_source = str(request.data.get("source") or "all").strip().lower()
        if requested_source not in {"all", AnalyticsSyncSource.UMAMI, AnalyticsSyncSource.SEARCH_CONSOLE}:
            return Response({"detail": "source must be all, umami, or search_console."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            days = int(request.data.get("days")) if request.data.get("days") is not None else None
        except (TypeError, ValueError):
            return Response({"detail": "days must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        force = bool(request.data.get("force"))
        sources = (
            [AnalyticsSyncSource.UMAMI, AnalyticsSyncSource.SEARCH_CONSOLE]
            if requested_source == "all"
            else [requested_source]
        )
        results = []
        failures = []
        for source in sources:
            try:
                results.append(
                    sync_organization_analytics(
                        context.organization,
                        source=source,
                        days=days,
                        force=force,
                    )
                )
            except (AnalyticsSite.DoesNotExist, SearchConsoleProperty.DoesNotExist) as exc:
                failures.append({"source": source, "error": "Source is not configured."})
            except Exception as exc:
                failures.append({"source": source, "error": str(exc)})
        return Response(
            {
                "status": analytics_status_payload(context.organization),
                "sync": {"state": "partial" if failures else "succeeded", "results": results, "failures": failures},
            },
            status=status.HTTP_207_MULTI_STATUS if failures and results else status.HTTP_502_BAD_GATEWAY if failures else status.HTTP_200_OK,
        )


def _report_summary_payload(report: ArticlePerformanceReport) -> dict:
    payload = report.payload if isinstance(report.payload, dict) else {}
    return {
        "id": report.pk,
        "reportDate": report.report_date.isoformat(),
        "generatedAt": report.generated_at.isoformat() if report.generated_at else None,
        "windowStart": report.window_start.isoformat(),
        "windowEnd": report.window_end.isoformat(),
        "dataThroughDate": (
            report.data_through_date.isoformat() if report.data_through_date else None
        ),
        "schemaVersion": report.schema_version,
        "headline": payload.get("headline") or {},
        "categoriesSummary": payload.get("categoriesSummary") or {},
    }


class VibeMarketingAnalyticsReportsView(APIView):
    """Newest-first daily brief summaries for the active company."""

    def get(self, request):
        context, error = _context(request)
        if error:
            return error
        try:
            limit = int(request.query_params.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 90))
        reports = ArticlePerformanceReport.objects.filter(
            organization=context.organization
        ).order_by("-report_date")[:limit]
        return Response(
            {"reports": [_report_summary_payload(report) for report in reports]},
            status=status.HTTP_200_OK,
        )


class VibeMarketingAnalyticsReportDetailView(APIView):
    """One immutable brief: summary fields plus the stored payload verbatim."""

    def get(self, request, report_id: int):
        context, error = _context(request)
        if error:
            return error
        report = ArticlePerformanceReport.objects.filter(
            organization=context.organization,
            pk=report_id,
        ).first()
        if report is None:
            return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)
        body = _report_summary_payload(report)
        body["payload"] = report.payload if isinstance(report.payload, dict) else {}
        return Response(body, status=status.HTTP_200_OK)
