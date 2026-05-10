from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
import time
from datetime import timedelta, timezone as datetime_timezone
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from content_factory.article_system import resolve_article_system
from content_factory.contract import CONTENT_FACTORY_REQUEST_SOURCE
from content_factory.google_baseline import collect_verified_google_metrics, google_baseline_connection_status
from content_factory.models import (
    ContentFactoryHealingPromotionState,
    ContentFactoryHealingRecord,
    AISaturation,
    ClusterMembership,
    KeywordStatus,
    KeywordVelocity,
    OrganizationContentConfig,
    PAQuestion,
    ResearchedKeyword,
    TopicFeedback,
    VibeMarketingComponentComment,
    VibeMarketingComponentCommentStatus,
    WebsiteBaselineSnapshot,
    WrittenArticle,
)
from content_factory.topic_feedback import (
    list_topic_feedback,
    normalize_topic_feedback_keyword,
    record_topic_feedback,
    restore_topic_feedback,
    serialize_topic_feedback,
)
from content_factory.topic_coverage import build_topic_coverage_memory, match_covered_topic
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import (
    apply_shared_startup_details,
    actor_ids_for_user,
    ensure_company_organization,
    founder_actor_id_for_user,
    get_founder_company_context,
    get_or_create_founder_profile,
    normalize_company_domain,
    normalize_company_linkedin_url,
    resolve_active_company,
    string_list_from_value,
)
from integrations import http_client
from integrations.models import UserIntegration
from integrations.services.article_generation import ArticleGenerationError, ensure_valid_org_token
from integrations.services.github import ScanError, TokenRefreshError, build_github_auth_url, ensure_valid_token
from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStep,
    ContentFactoryRunStatus,
    ContentFactoryStepStatus,
)


logger = logging.getLogger(__name__)


class _AnyContentRenderer(BaseRenderer):
    media_type = "*/*"
    format = "proxy"
    charset = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, bytes):
            return data
        return json.dumps(data or {}).encode("utf-8")


VIBE_MARKETING_WORKFLOWS = {
    "repo_scan",
    "content_factory_scan",
    "auto_discovery",
    "content_factory_discovery",
    "article_generation",
    "content_factory_article",
    "direct_generate",
    "confirmed_topic",
    "article_revision",
    "daily_discovery",
    "startup_autofill",
    "website_baseline",
    "vibe_marketing_daily_replay",
}
SCAN_WORKFLOWS = {"repo_scan", "content_factory_scan"}
DISCOVERY_WORKFLOWS = {"auto_discovery", "content_factory_discovery", "daily_discovery"}
ARTICLE_WORKFLOWS = {"article_generation", "content_factory_article", "direct_generate", "confirmed_topic", "article_revision"}
BASELINE_WORKFLOWS = {"website_baseline"}
REMOTE_REQUIRED_WORKFLOWS = {
    "article_generation",
    "content_factory_article",
    "direct_generate",
    "confirmed_topic",
    "repo_scan",
    "content_factory_scan",
    "auto_discovery",
    "content_factory_discovery",
    "daily_discovery",
    "website_baseline",
}
BASELINE_FRESH_DAYS = 30
WORKFLOW_STEP_DEFS = [
    {
        "id": "profile",
        "label": "Startup profile",
        "phase": "Setup",
        "href": "/founder-tools/marketing/create?step=startupDetails",
        "check": "websiteProfile",
        "summary": "Company, audience, competitors, and seed keywords.",
    },
    {
        "id": "baseline",
        "label": "Website baseline",
        "phase": "Setup",
        "href": "/founder-tools/marketing/create?step=baseline",
        "check": "baseline",
        "summary": "Capture website health before article work starts.",
    },
    {
        "id": "repo",
        "label": "Repository",
        "phase": "Setup",
        "href": "/founder-tools/marketing/create?step=github",
        "check": "github",
        "summary": "Connect the repository used for exact article previews and publishing.",
    },
    {
        "id": "article_system",
        "label": "Article system",
        "phase": "Setup",
        "href": "/founder-tools/marketing/create?step=articleSystem",
        "check": "scaffold",
        "summary": "Detect or prepare the article route, registry, and publish target.",
    },
    {
        "id": "research",
        "label": "Research topics",
        "phase": "Plan",
        "href": "/founder-tools/marketing/create?step=research",
        "check": "research",
        "summary": "Find article candidates from the company context.",
    },
    {
        "id": "choose_topic",
        "label": "Choose topic",
        "phase": "Plan",
        "href": "/founder-tools/marketing/create?step=chooseArticle",
        "summary": "Pick a discovered topic or enter a custom article brief.",
    },
    {
        "id": "generate",
        "label": "Generate article",
        "phase": "Create",
        "href": "/founder-tools/marketing/create?step=writeCheck",
        "check": "write",
        "summary": "Generate the article and package its content artifacts.",
    },
    {
        "id": "review",
        "label": "Review article",
        "phase": "Create",
        "href": "/founder-tools/marketing/create?step=editArticle",
        "summary": "Review the exact live article preview and leave component comments.",
    },
    {
        "id": "revise",
        "label": "Revise article",
        "phase": "Create",
        "href": "/founder-tools/marketing/create?step=editArticle",
        "summary": "Send component comments for AI revision and accept the result.",
    },
    {
        "id": "package",
        "label": "Package ready",
        "phase": "Publish",
        "href": "/founder-tools/marketing/create?step=reviewPublish",
        "check": "contentPackage",
        "summary": "Content-only delivery artifacts are ready to inspect or promote.",
    },
    {
        "id": "publish",
        "label": "Publish to site",
        "phase": "Publish",
        "href": "/founder-tools/marketing/create?step=reviewPublish",
        "check": "publish",
        "summary": "Promote the package into a PR, preview URL, or CMS publish flow.",
    },
    {
        "id": "automation",
        "label": "Daily automation",
        "phase": "Automate",
        "href": "/founder-tools/marketing/create?step=dailyAutomation",
        "check": "dailyAutomation",
        "summary": "Enable recurring topic discovery with human review.",
    },
]
WORKFLOW_STEP_IDS = [step["id"] for step in WORKFLOW_STEP_DEFS]
RUNNING_RUN_STATUSES = {ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.RUNNING}
FAILED_RUN_STATUSES = {ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.CANCELLED, ContentFactoryRunStatus.DENIED}
ARTICLE_DELIVERY_MODES = {"content_only", "review_draft", "publish_code"}
LEGACY_REVIEW_BLOCKING_DELIVERY_MODES = {"content_only", "publish_code"}


def _camel_list(value):
    return string_list_from_value(value)


def _request_value(data, *keys, default=None):
    for key in keys:
        if key in data:
            return data.get(key)
    return default


def _bool_from_request(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _stored_article_delivery_mode(config) -> str:
    mode = str(
        config.article_delivery_mode
        or getattr(settings, "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE", "")
        or ""
    ).strip()
    return mode if mode in ARTICLE_DELIVERY_MODES else ""


def _article_repo_is_review_capable(config, *, github_ready=None, article_ready=None) -> bool:
    if github_ready is None:
        github_ready = config.github_connection_state == "connected" and bool(config.github_repo)
    if article_ready is None:
        article_system = resolve_article_system(config)
        article_ready = article_system.get("state") in {
            "ready",
            "detected",
            "registry_driven_seo_ready",
            "article_system_ready",
        } or bool(config.publish_targets)
    return bool(github_ready and article_ready)


def _effective_article_delivery_mode(config, *, requested_mode=None, explicit=False, github_ready=None, article_ready=None) -> str:
    requested = str(requested_mode or "").strip()
    if explicit and requested in ARTICLE_DELIVERY_MODES:
        return requested

    stored = _stored_article_delivery_mode(config)
    if _article_repo_is_review_capable(config, github_ready=github_ready, article_ready=article_ready):
        if stored in LEGACY_REVIEW_BLOCKING_DELIVERY_MODES or not stored:
            return "review_draft"
    return stored or "content_only"


def _company_id_from_request(request):
    return (
        request.query_params.get("company_id")
        or request.query_params.get("companyId")
        or request.data.get("company_id")
        or request.data.get("companyId")
    )


def _resolve_context_or_response(request, *, require_domain=True):
    try:
        context = get_founder_company_context(request.user, company_id=_company_id_from_request(request))
    except VibeRaisingCompany.DoesNotExist:
        return None, Response(
            {"detail": "Create or select a founder company first.", "redirect": "/founder-tools/company-setup"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except PermissionError:
        return None, Response({"detail": "Only founders can access Vibe Marketing."}, status=status.HTTP_403_FORBIDDEN)
    except Exception as exc:
        if require_domain:
            return None, Response({"detail": str(exc) or "Company domain is required."}, status=status.HTTP_400_BAD_REQUEST)
        return None, Response({"detail": str(exc) or "Unable to resolve company."}, status=status.HTTP_400_BAD_REQUEST)

    if require_domain and not context.organization.domain:
        return None, Response({"detail": "Company domain is required."}, status=status.HTTP_400_BAD_REQUEST)
    return context, None


def _get_config(organization):
    config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
    return config


def _assign_config_actor(config, user) -> list[str]:
    actor_id = founder_actor_id_for_user(user)
    actor_aliases = actor_ids_for_user(user)
    current_actor_id = str(config.connected_slack_user_id or "").strip()
    update_fields = []
    if (
        not current_actor_id
        or current_actor_id in actor_aliases
        or current_actor_id.startswith("mlai_user:")
    ) and current_actor_id != actor_id:
        config.connected_slack_user_id = actor_id
        update_fields.append("connected_slack_user_id")
    return update_fields


def _clean_github_repo(value) -> str:
    return str(value or "").strip()


def _github_repo_matches(candidate: str, expected: str) -> bool:
    return bool(candidate and expected and candidate.casefold() == expected.casefold())


def _connected_github_response(config, *, status_value="already_connected"):
    return {
        "status": status_value,
        "connection_state": "connected",
        "github_repo": config.github_repo,
        "github_user_name": config.github_user_name,
        "credential_source": "org",
    }


def _promote_user_github_credentials_to_org(config, integration, github_repo: str):
    config.github_token_encrypted = integration.github_access_token
    config.github_refresh_token_encrypted = integration.github_refresh_token
    config.github_token_expires_at = integration.github_token_expires_at
    config.github_user_name = integration.github_user_name
    config.github_installation_id = integration.github_installation_id
    config.github_scopes = integration.github_scopes or []
    config.github_repo = github_repo
    config.save(
        update_fields=[
            "github_token_encrypted",
            "github_refresh_token_encrypted",
            "github_token_expires_at",
            "github_user_name",
            "github_installation_id",
            "github_scopes",
            "github_repo",
            "updated_at",
        ]
    )


def _connect_with_existing_github_credentials(config, *, domain: str, actor_id: str, requested_repo: str):
    configured_repo = _clean_github_repo(config.github_repo)
    if config.github_token_encrypted and configured_repo:
        try:
            ensure_valid_org_token(domain)
            config.refresh_from_db()
            return _connected_github_response(config)
        except (ArticleGenerationError, TokenRefreshError):
            pass

    integration = UserIntegration.objects.filter(slack_user_id=actor_id).first()
    if not integration or not integration.github_access_token:
        return None

    integration_repo = _clean_github_repo(integration.github_repo)
    target_repo = requested_repo or configured_repo
    if not integration_repo or not _github_repo_matches(integration_repo, target_repo):
        return None

    try:
        ensure_valid_token(actor_id)
        integration.refresh_from_db()
    except (ScanError, TokenRefreshError):
        return None

    _promote_user_github_credentials_to_org(config, integration, target_repo or integration_repo)
    return {
        "status": "already_connected",
        "connection_state": "connected",
        "github_repo": config.github_repo,
        "github_user_name": config.github_user_name,
        "credential_source": "user_promoted",
    }


def _run_belongs_to_context(run, context) -> bool:
    return normalize_company_domain(run.domain) == normalize_company_domain(context.organization.domain)


def _latest_runs_for_org(organization, limit=6):
    return list(
        ContentFactoryRun.objects.filter(domain=organization.domain, workflow__in=VIBE_MARKETING_WORKFLOWS)
        .exclude(status=ContentFactoryRunStatus.CANCELLED)
        .prefetch_related("steps")
        .order_by("-updated_at")[:limit]
    )


def _latest_run_matching(runs, workflows):
    return next((run for run in runs if run.workflow in workflows), None)


def _first_non_empty_mapping_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


def _append_candidate_source(target, value):
    if isinstance(value, list):
        target.extend(value)
    elif isinstance(value, dict):
        target.append(value)
    elif isinstance(value, str):
        target.append(value)


def _extract_topic_candidates_from_result(result):
    if not isinstance(result, dict):
        return []
    raw_candidates = []
    for mapping in (result, result.get("selection_data"), result.get("selection")):
        if not isinstance(mapping, dict):
            continue
        _append_candidate_source(
            raw_candidates,
            _first_non_empty_mapping_value(
                mapping,
                "options",
                "topic_options",
                "topics",
                "topic_candidates",
                "candidates",
                "keywords",
                "keyword_options",
            ),
        )
        _append_candidate_source(raw_candidates, mapping.get("selected"))
    if not raw_candidates:
        return []

    candidates = []
    for index, raw in enumerate(raw_candidates):
        if isinstance(raw, str):
            candidates.append(
                {
                    "id": str(index),
                    "keyword": raw,
                    "title": raw,
                    "reason": "",
                    "source": "discovery",
                }
            )
            continue
        if not isinstance(raw, dict):
            continue
        keyword = str(
            raw.get("keyword")
            or raw.get("target_keyword")
            or raw.get("targetKeyword")
            or raw.get("query")
            or raw.get("topic")
            or raw.get("title")
            or ""
        ).strip()
        title = str(
            raw.get("title")
            or raw.get("suggested_title")
            or raw.get("suggestedTitle")
            or raw.get("custom_title")
            or raw.get("customTitle")
            or raw.get("angle")
            or raw.get("headline")
            or keyword
        ).strip()
        if not keyword and not title:
            continue
        reason = raw.get("reason") or raw.get("selection_reason") or raw.get("rationale") or raw.get("explanation") or ""
        opportunity_score = (
            raw.get("opportunity_score")
            if raw.get("opportunity_score") is not None
            else raw.get("opportunityScore")
            if raw.get("opportunityScore") is not None
            else raw.get("opportunityIndex")
        )
        candidates.append(
            {
                "id": str(raw.get("id") or raw.get("keyword_id") or index),
                "keyword": keyword or title,
                "title": title or keyword,
                "reason": str(reason),
                "source": str(raw.get("source") or "discovery"),
                "intent": raw.get("intent"),
                "difficulty": raw.get("difficulty"),
                "difficultySource": raw.get("difficulty_source") or raw.get("difficultySource") or "missing",
                "opportunityScore": opportunity_score,
                "volume": raw.get("volume"),
                "volumeDisplay": raw.get("volume_display") or raw.get("volumeDisplay"),
                "trend": raw.get("trending_status") or raw.get("trend") or raw.get("trend_status"),
                "trendStatus": raw.get("trend_status") or raw.get("trendStatus") or raw.get("trending_status") or raw.get("trend"),
                "trendPercent": raw.get("trend_percent") or raw.get("trendPercent"),
                "trendDescription": raw.get("trend_description") or raw.get("trendDescription") or raw.get("stats_meaning") or raw.get("statsMeaning"),
                "trendLabel": raw.get("trending_label") or raw.get("trendLabel"),
                "statsMeaning": raw.get("stats_meaning") or raw.get("statsMeaning"),
                "whyRecommended": raw.get("why_recommended") or raw.get("whyRecommended"),
                "recommendationReason": raw.get("recommendation_reason") or raw.get("recommendationReason"),
                "aiSearches": raw.get("ai_search_volume") or raw.get("aiSearches") or raw.get("ai_searches"),
                "aiVolumeDisplay": raw.get("ai_volume_display") or raw.get("aiVolumeDisplay"),
                "monthlySearches": raw.get("monthly_searches") or raw.get("monthlySearches") or raw.get("daily_volumes") or raw.get("dailyVolumes") or [],
                "relatedKeywords": raw.get("related_keywords") or raw.get("relatedKeywords") or [],
                "paaQuestions": raw.get("paa_questions") or raw.get("paaQuestions") or [],
                "sourceRunId": raw.get("source_run_id") or raw.get("sourceRunId"),
            }
        )
    return candidates


def _safe_number(value, default=0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value):
    return value if isinstance(value, list) else []


def _trend_percent_from_velocity(velocity):
    if not velocity:
        return None
    score = velocity.get("velocityScore")
    if score is None:
        score = velocity.get("velocity_score")
    if score is None:
        return None
    try:
        return round(float(score) * 100)
    except (TypeError, ValueError):
        return None


def _trend_description(status):
    status = str(status or "").lower()
    if status in {"breakout", "rising", "growing"}:
        return "Interest is growing and more people are searching for this topic."
    if status == "declining":
        return "Interest is declining slightly over recent searches."
    if status == "stable":
        return "Steady interest with consistent search volume over time."
    return "Trend data is not available yet."


def _latest_keyword_velocity(keyword):
    try:
        snapshot = keyword.velocity_snapshots.first()
    except Exception:
        snapshot = None
    if not snapshot:
        return None
    return {
        "velocityScore": snapshot.velocity_score,
        "trendStatus": snapshot.trend_status,
        "absoluteVolume": snapshot.absolute_volume,
        "dailyVolumes": snapshot.daily_volumes or [],
    }


def _latest_keyword_saturation(keyword):
    try:
        snapshot = keyword.ai_saturation_snapshots.first()
    except Exception:
        snapshot = None
    if not snapshot:
        return None
    return {
        "saturationScore": snapshot.saturation_score,
        "aiOverviewPresent": snapshot.ai_overview_present,
        "aiOverviewQuality": snapshot.ai_overview_quality,
        "featuredSnippetPresent": snapshot.featured_snippet_present,
        "videoCarouselPresent": snapshot.video_carousel_present,
        "knowledgePanelPresent": snapshot.knowledge_panel_present,
        "hostilityScore": snapshot.hostility_score,
        "hostilityRecommendation": snapshot.hostility_recommendation,
        "serpFeatures": snapshot.serp_features or [],
    }


def _keyword_related_keywords(keyword, *, limit=6):
    related = []
    seen = {keyword.keyword_normalized}
    for value in _json_list(getattr(keyword, "related_keywords", None)):
        value = str(value or "").strip()
        normalized = value.lower().strip()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        related.append(value)
        if len(related) >= limit:
            return related
    try:
        memberships = keyword.cluster_memberships.all()
    except Exception:
        memberships = []
    for membership in memberships:
        try:
            member_keywords = membership.cluster.member_keywords.all()
        except Exception:
            member_keywords = []
        for member in member_keywords:
            member_keyword = getattr(member, "keyword", None)
            if member_keyword is None:
                continue
            normalized = getattr(member_keyword, "keyword_normalized", "") or member_keyword.keyword.lower().strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            related.append(member_keyword.keyword)
            if len(related) >= limit:
                return related
    return related


def _keyword_paa_questions(keyword, *, limit=4):
    try:
        questions = keyword.paa_questions.all()
    except Exception:
        questions = []
    return [
        {
            "question": question.question,
            "answerSnippet": question.answer_snippet,
            "depth": question.depth,
            "hasAiOverview": question.has_ai_overview,
        }
        for question in list(questions)[:limit]
    ]


def _keyword_is_available_for_topic_picker(keyword, *, include_written=False, coverage_memory=None):
    if include_written:
        return True
    if keyword.status in {KeywordStatus.WRITTEN, KeywordStatus.IN_PROGRESS, KeywordStatus.SKIPPED}:
        return False
    if keyword.written_article_id:
        return False
    if keyword.cooldown_until and keyword.cooldown_until > timezone.now():
        return False
    if coverage_memory and match_covered_topic(keyword=keyword.keyword, memory=coverage_memory):
        return False
    return True


def _topic_candidate_from_keyword(keyword):
    title = keyword.keyword
    intent = str(keyword.intent or "").replace("_", " ").strip()
    difficulty_source = keyword.difficulty_source or "legacy_default"
    reason_parts = []
    if keyword.opportunity_index:
        reason_parts.append(f"Opportunity score {keyword.opportunity_index:g}")
    if keyword.volume:
        reason_parts.append(f"{keyword.volume:,} monthly searches")
    if keyword.difficulty is not None and difficulty_source in {"dataforseo_labs", "dataforseo_bulk"}:
        reason_parts.append(f"difficulty {keyword.difficulty}/100")
    elif keyword.difficulty is not None:
        reason_parts.append("difficulty pending")
    if intent:
        reason_parts.append(f"{intent} intent")
    reason = ". ".join(reason_parts) or "Recommended from stored topic research."
    written_article = keyword.written_article
    already_written = bool(keyword.status == KeywordStatus.WRITTEN or written_article)
    velocity = _latest_keyword_velocity(keyword)
    monthly_searches = _json_list(getattr(keyword, "monthly_searches", None)) or (velocity or {}).get("dailyVolumes") or []
    trend_status = (velocity or {}).get("trendStatus")
    trend_percent = _trend_percent_from_velocity(velocity)
    return {
        "id": f"keyword:{keyword.id}",
        "keyword": keyword.keyword,
        "title": title,
        "reason": reason,
        "source": "researched_keyword",
        "intent": keyword.intent,
        "difficulty": keyword.difficulty,
        "difficultySource": difficulty_source,
        "opportunityScore": keyword.opportunity_index,
        "volume": keyword.volume,
        "tier": keyword.tier,
        "status": keyword.status,
        "alreadyWritten": already_written,
        "writtenArticle": _serialize_written_article(written_article) if written_article else None,
        "velocity": velocity,
        "monthlySearches": monthly_searches,
        "trendStatus": trend_status,
        "trendPercent": trend_percent,
        "trendDescription": _trend_description(trend_status),
        "aiSaturation": _latest_keyword_saturation(keyword),
        "relatedKeywords": _keyword_related_keywords(keyword),
        "paaQuestions": _keyword_paa_questions(keyword),
    }


def _serialize_topic_coverage_match(match):
    if not match:
        return None
    article = match.article
    keyword = match.keyword
    return {
        "source": match.source,
        "reason": match.reason,
        "matchType": match.match_type,
        "similarity": match.similarity,
        "keyword": keyword.keyword if keyword else match.record.text,
        "status": keyword.status if keyword else None,
        "writtenArticle": _serialize_written_article(article) if article else None,
    }


def _apply_topic_coverage_to_candidate(candidate, match):
    if not match:
        return candidate
    covered_topic = _serialize_topic_coverage_match(match)
    article = match.article
    return {
        **candidate,
        "alreadyWritten": bool(candidate.get("alreadyWritten") or article),
        "writtenArticle": candidate.get("writtenArticle") or (_serialize_written_article(article) if article else None),
        "coveredTopic": covered_topic,
    }


def _stored_keyword_topic_candidates(
    organization,
    *,
    include_written=False,
    limit=50,
    declined_keyword_keys=None,
    coverage_memory=None,
):
    declined_keyword_keys = declined_keyword_keys or set()
    if coverage_memory is None:
        coverage_memory = build_topic_coverage_memory(organization)
    fetch_limit = max(limit * 4, limit)
    keywords = (
        ResearchedKeyword.objects.filter(organization=organization)
        .select_related("written_article")
        .prefetch_related(
            Prefetch("velocity_snapshots", queryset=KeywordVelocity.objects.order_by("-captured_at")),
            Prefetch("ai_saturation_snapshots", queryset=AISaturation.objects.order_by("-captured_at")),
            Prefetch("paa_questions", queryset=PAQuestion.objects.order_by("depth", "order")),
            Prefetch(
                "cluster_memberships",
                queryset=ClusterMembership.objects.select_related("cluster").prefetch_related(
                    Prefetch(
                        "cluster__member_keywords",
                        queryset=ClusterMembership.objects.select_related("keyword").order_by(
                            "-keyword__opportunity_index",
                            "-keyword__volume",
                            "keyword__keyword",
                        ),
                    ),
                ),
            ),
        )
        .order_by("-opportunity_index", "-volume", "difficulty", "-metrics_updated_at")[:fetch_limit]
    )
    candidates = []
    for keyword in keywords:
        if normalize_topic_feedback_keyword(keyword.keyword) in declined_keyword_keys:
            continue
        coverage_match = match_covered_topic(keyword=keyword.keyword, memory=coverage_memory)
        if not _keyword_is_available_for_topic_picker(
            keyword,
            include_written=include_written,
            coverage_memory=coverage_memory,
        ):
            continue
        candidates.append(_apply_topic_coverage_to_candidate(_topic_candidate_from_keyword(keyword), coverage_match))
        if len(candidates) >= limit:
            break
    return candidates


def _normalize_keyword_memory(value) -> str:
    return normalize_topic_feedback_keyword(value)


def _serialize_written_article(article):
    return {
        "id": str(article.id),
        "title": article.title,
        "slug": article.slug,
        "keyword": article.primary_keyword,
        "articleUrl": article.article_url or "",
        "prUrl": article.pr_url or "",
        "writtenAt": article.published_at.isoformat() if article.published_at else article.created_at.isoformat(),
    }


def _written_topic_memory(organization):
    keywords = {
        keyword.keyword_normalized: keyword
        for keyword in ResearchedKeyword.objects.filter(organization=organization).select_related("written_article")
    }
    articles = list(WrittenArticle.objects.filter(organization=organization).order_by("-created_at")[:50])
    written_by_keyword = {
        _normalize_keyword_memory(article.primary_keyword): article
        for article in articles
        if _normalize_keyword_memory(article.primary_keyword)
    }
    written_by_slug = {article.slug: article for article in articles if article.slug}
    return {
        "keywords": keywords,
        "written_by_keyword": written_by_keyword,
        "written_by_slug": written_by_slug,
        "recent_articles": articles,
    }


def _candidate_written_article(candidate, memory):
    keyword_key = _normalize_keyword_memory(candidate.get("keyword"))
    title_slug = slugify(str(candidate.get("title") or ""))
    keyword = memory["keywords"].get(keyword_key)
    if keyword and (keyword.status == KeywordStatus.WRITTEN or keyword.written_article_id):
        return keyword.written_article
    return memory["written_by_keyword"].get(keyword_key) or memory["written_by_slug"].get(title_slug)


def _enrich_topic_candidates(
    organization,
    candidates,
    *,
    include_written=False,
    declined_keyword_keys=None,
    coverage_memory=None,
    written_memory=None,
):
    declined_keyword_keys = declined_keyword_keys or set()
    if written_memory is None:
        written_memory = _written_topic_memory(organization)
    if coverage_memory is None:
        coverage_memory = build_topic_coverage_memory(organization)
    enriched = []
    for candidate in candidates:
        keyword_key = _normalize_keyword_memory(candidate.get("keyword"))
        if keyword_key in declined_keyword_keys:
            continue
        keyword = written_memory["keywords"].get(keyword_key)
        written_article = _candidate_written_article(candidate, written_memory)
        coverage_match = match_covered_topic(
            keyword=candidate.get("keyword") or "",
            title=candidate.get("title") or "",
            memory=coverage_memory,
        )
        if coverage_match and coverage_match.article and not written_article:
            written_article = coverage_match.article
        already_written = bool(written_article or (keyword and keyword.status == KeywordStatus.WRITTEN))
        unavailable = bool(
            not include_written
            and (
                bool(
                    keyword
                    and (
                        keyword.status in {KeywordStatus.IN_PROGRESS, KeywordStatus.SKIPPED}
                        or (keyword.cooldown_until and keyword.cooldown_until > timezone.now())
                    )
                )
                or bool(coverage_match)
            )
        )
        enriched_candidate = _apply_topic_coverage_to_candidate({
            **candidate,
            "status": KeywordStatus.WRITTEN if already_written else (keyword.status if keyword else KeywordStatus.PENDING),
            "alreadyWritten": already_written,
            "writtenArticle": _serialize_written_article(written_article) if written_article else None,
        }, coverage_match)
        if include_written or (not already_written and not unavailable):
            enriched.append(enriched_candidate)
    return enriched


VERIFIED_DIFFICULTY_SOURCES = {"dataforseo_labs", "dataforseo_bulk"}


def _prefer_topic_difficulty(existing, candidate):
    existing_source = existing.get("difficultySource") or existing.get("difficulty_source") or "missing"
    candidate_source = candidate.get("difficultySource") or candidate.get("difficulty_source") or "missing"
    if existing_source not in VERIFIED_DIFFICULTY_SOURCES and candidate_source in VERIFIED_DIFFICULTY_SOURCES:
        return candidate.get("difficulty"), candidate_source
    return (
        existing.get("difficulty") if existing.get("difficulty") is not None else candidate.get("difficulty"),
        existing_source or candidate_source or "missing",
    )


def _topic_candidates_from_runs(
    runs,
    *,
    organization=None,
    include_written=False,
    declined_keyword_keys=None,
    coverage_memory=None,
    written_memory=None,
):
    run_candidates = []
    for run in runs:
        if run.workflow not in DISCOVERY_WORKFLOWS:
            continue
        candidates = _extract_topic_candidates_from_result(run.result or {})
        if candidates:
            for candidate in candidates:
                candidate["sourceRunId"] = candidate.get("sourceRunId") or run.run_id
            run_candidates = candidates
            break
    if organization is None:
        return run_candidates

    declined_keyword_keys = declined_keyword_keys or set()
    stored_candidates = _stored_keyword_topic_candidates(
        organization,
        include_written=include_written,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
    )
    enriched_run_candidates = _enrich_topic_candidates(
        organization,
        run_candidates,
        include_written=include_written,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        written_memory=written_memory,
    )
    merged = {}
    for candidate in [*stored_candidates, *enriched_run_candidates]:
        key = _normalize_keyword_memory(candidate.get("keyword"))
        if not key:
            continue
        existing = merged.get(key)
        if existing:
            difficulty, difficulty_source = _prefer_topic_difficulty(existing, candidate)
            merged[key] = {
                **existing,
                **{item_key: item_value for item_key, item_value in candidate.items() if item_value not in (None, "", [])},
                "volume": existing.get("volume") or candidate.get("volume"),
                "difficulty": difficulty,
                "difficultySource": difficulty_source,
                "opportunityScore": existing.get("opportunityScore") or candidate.get("opportunityScore"),
                "alreadyWritten": bool(existing.get("alreadyWritten") or candidate.get("alreadyWritten")),
                "writtenArticle": existing.get("writtenArticle") or candidate.get("writtenArticle"),
            }
        else:
            merged[key] = candidate

    return sorted(
        merged.values(),
        key=lambda candidate: (
            -_safe_number(candidate.get("opportunityScore")),
            -_safe_number(candidate.get("volume")),
            _safe_number(candidate.get("difficulty"), default=100),
            str(candidate.get("keyword") or ""),
        ),
    )


def _recent_written_topics(organization, *, limit=8):
    return [
        _serialize_written_article(article)
        for article in WrittenArticle.objects.filter(organization=organization).order_by("-created_at")[:limit]
    ]


def _has_completed_article_flow(organization, latest_runs=None):
    if WrittenArticle.objects.filter(organization=organization).exists():
        return True
    for run in latest_runs or []:
        if run.workflow not in ARTICLE_WORKFLOWS or run.status != ContentFactoryRunStatus.COMPLETED:
            continue
        content_package = _content_package_from_run(run)
        if content_package and content_package.get("contentPackaged"):
            return True
    return False


def _start_page_mode(organization, latest_runs=None):
    if not organization or not normalize_company_domain(getattr(organization, "domain", "")):
        return "first_article_setup"
    return "topic_picker" if _has_completed_article_flow(organization, latest_runs) else "first_article_setup"


def _topic_is_already_written(organization, *, keyword: str, title: str = ""):
    memory = _written_topic_memory(organization)
    keyword_key = _normalize_keyword_memory(keyword)
    title_slug = slugify(str(title or ""))
    keyword_row = memory["keywords"].get(keyword_key)
    if keyword_row and (keyword_row.status == KeywordStatus.WRITTEN or keyword_row.written_article_id):
        return keyword_row.written_article or memory["written_by_keyword"].get(keyword_key)
    exact_article = memory["written_by_keyword"].get(keyword_key) or memory["written_by_slug"].get(title_slug)
    if exact_article:
        return exact_article
    coverage_match = match_covered_topic(keyword=keyword, title=title, organization=organization)
    return coverage_match.article if coverage_match and coverage_match.article else None


def _mark_keyword_in_progress(organization, keyword_text: str):
    keyword_text = str(keyword_text or "").strip()
    if not keyword_text:
        return None
    keyword, _created = ResearchedKeyword.objects.get_or_create(
        organization=organization,
        keyword_normalized=_normalize_keyword_memory(keyword_text),
        defaults={"keyword": keyword_text, "status": KeywordStatus.IN_PROGRESS},
    )
    keyword_update_fields = []
    if keyword.keyword != keyword_text:
        keyword.keyword = keyword_text
        keyword_update_fields.append("keyword")
    if keyword.status not in {KeywordStatus.WRITTEN, KeywordStatus.IN_PROGRESS}:
        keyword.status = KeywordStatus.IN_PROGRESS
        keyword.status_changed_at = timezone.now()
        keyword_update_fields.extend(["status", "status_changed_at"])
    keyword.times_selected += 1
    keyword.last_selected_at = timezone.now()
    keyword_update_fields.extend(["times_selected", "last_selected_at"])
    if keyword_update_fields:
        keyword.save(update_fields=list(dict.fromkeys(keyword_update_fields)))
    return keyword


def _artifact_paths_from_run(run):
    artifact_paths = {}
    for step in run.steps.all():
        artifacts = step.artifacts or []
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if isinstance(artifact, str):
                name = artifact.rsplit("/", 1)[-1]
                artifact_paths.setdefault(name, artifact)
                continue
            if not isinstance(artifact, dict):
                continue
            path = artifact.get("path") or artifact.get("url") or artifact.get("href")
            if not path:
                continue
            name = artifact.get("name") or artifact.get("filename") or artifact.get("key") or str(path).rsplit("/", 1)[-1]
            artifact_paths[str(name)] = str(path)
    return artifact_paths


def _content_package_from_run(run):
    if not run:
        return None
    result = run.result or {}
    acceptance_summary = run.acceptance_summary or {}
    evidence_summary = acceptance_summary.get("evidence_summary") if isinstance(acceptance_summary.get("evidence_summary"), dict) else {}
    delivery_package = (
        result.get("delivery_package")
        or result.get("deliveryPackage")
        or result.get("content_package")
        or result.get("contentPackage")
        or {}
    )
    if not isinstance(delivery_package, dict):
        delivery_package = {}
    article_meta = result.get("article_meta") or result.get("articleMeta") or delivery_package.get("article_meta") or {}
    if not isinstance(article_meta, dict):
        article_meta = {}
    image_manifest = (
        result.get("image_manifest")
        or result.get("imageManifest")
        or delivery_package.get("image_manifest")
        or {}
    )
    if not isinstance(image_manifest, dict):
        image_manifest = {}

    artifact_paths = {}
    for source in (
        delivery_package.get("artifacts"),
        delivery_package.get("artifact_paths"),
        delivery_package,
        evidence_summary,
        _artifact_paths_from_run(run),
    ):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if not value:
                continue
            normalized_key = {
                "delivery_package": "delivery_package.json",
                "delivery_package_path": "delivery_package.json",
                "content_package_path": "delivery_package.json",
                "article_markdown": "article.md",
                "article_markdown_path": "article.md",
                "article_html": "article.html",
                "article_html_path": "article.html",
                "article_json": "article.json",
                "article_json_path": "article.json",
                "article_meta": "article_meta.json",
                "article_meta_path": "article_meta.json",
                "article_component_manifest": "article_component_manifest.json",
                "article_component_manifest_path": "article_component_manifest.json",
                "image_manifest": "image_manifest.json",
                "image_manifest_path": "image_manifest.json",
                "references": "references.json",
            }.get(key, key)
            if str(normalized_key).endswith((".json", ".md", ".html")) or str(key).endswith("_path"):
                artifact_paths[str(normalized_key)] = str(value)

    title = (
        delivery_package.get("title")
        or article_meta.get("title")
        or evidence_summary.get("content_package_title")
        or result.get("title")
    )
    slug = delivery_package.get("slug") or article_meta.get("slug") or evidence_summary.get("content_package_slug") or result.get("slug")
    target_keyword = (
        delivery_package.get("target_keyword")
        or delivery_package.get("targetKeyword")
        or article_meta.get("target_keyword")
        or article_meta.get("targetKeyword")
        or evidence_summary.get("content_package_target_keyword")
        or result.get("target_keyword")
        or result.get("targetKeyword")
    )
    content_packaged = bool(
        acceptance_summary.get("content_packaged")
        or delivery_package
        or artifact_paths
        or evidence_summary.get("content_package_path")
    )
    if not content_packaged:
        return None

    return {
        "title": title,
        "slug": slug,
        "targetKeyword": target_keyword,
        "artifactPaths": artifact_paths,
        "imageManifestStatus": image_manifest.get("status") or evidence_summary.get("image_manifest_status"),
        "heroImagePresent": bool(
            image_manifest.get("hero")
            or image_manifest.get("hero_image")
            or evidence_summary.get("hero_image_present")
            or evidence_summary.get("generated_hero_image")
            or evidence_summary.get("hero_image_url")
        ),
        "generatedInlineImageCount": (
            image_manifest.get("inline_count")
            if image_manifest.get("inline_count") is not None
            else evidence_summary.get("generated_inline_image_count")
        ),
        "imageErrorCount": (
            image_manifest.get("error_count")
            if image_manifest.get("error_count") is not None
            else evidence_summary.get("image_error_count")
            if evidence_summary.get("image_error_count") is not None
            else len(evidence_summary.get("image_errors") or [])
        ),
        "contentPackaged": content_packaged,
    }


def _persist_article_memory_from_run(*, organization, run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return None
    if run.status != ContentFactoryRunStatus.COMPLETED:
        return None
    content_package = _content_package_from_run(run)
    if not content_package or not content_package.get("contentPackaged"):
        return None

    result = run.result or {}
    title = str(content_package.get("title") or result.get("title") or "").strip()
    primary_keyword = str(
        content_package.get("targetKeyword")
        or result.get("target_keyword")
        or result.get("targetKeyword")
        or ""
    ).strip()
    if not title and primary_keyword:
        title = primary_keyword
    if not primary_keyword and title:
        primary_keyword = title
    if not title or not primary_keyword:
        return None

    slug = str(content_package.get("slug") or result.get("slug") or slugify(title) or run.run_id).strip()
    evidence = _publish_evidence_from_run(run)
    article, _created = WrittenArticle.objects.update_or_create(
        organization=organization,
        slug=slug,
        defaults={
            "title": title,
            "category": str(result.get("category") or "featured"),
            "article_url": evidence.get("previewUrl") or result.get("article_url") or "",
            "pr_url": evidence.get("prUrl") or result.get("pr_url") or "",
            "primary_keyword": primary_keyword,
            "published_at": timezone.now(),
        },
    )
    keyword, _keyword_created = ResearchedKeyword.objects.get_or_create(
        organization=organization,
        keyword_normalized=_normalize_keyword_memory(primary_keyword),
        defaults={"keyword": primary_keyword},
    )
    keyword.keyword = primary_keyword
    keyword.status = KeywordStatus.WRITTEN
    keyword.written_article = article
    keyword.status_changed_at = timezone.now()
    keyword.save(update_fields=["keyword", "status", "written_article", "status_changed_at"])
    return article


def _persist_completed_article_memory_if_possible(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS or run.status != ContentFactoryRunStatus.COMPLETED:
        return None
    domain = normalize_company_domain(run.domain)
    if not domain:
        return None
    organization = Organization.objects.filter(domain__iexact=domain).first()
    if organization is None:
        return None
    return _persist_article_memory_from_run(organization=organization, run=run)


def _component_manifest_from_run(run):
    if not run:
        return None
    result = run.result or {}
    delivery_package = (
        result.get("delivery_package")
        or result.get("deliveryPackage")
        or result.get("content_package")
        or result.get("contentPackage")
        or {}
    )
    if not isinstance(delivery_package, dict):
        delivery_package = {}
    manifest = (
        result.get("componentManifest")
        or result.get("component_manifest")
        or delivery_package.get("component_manifest")
        or delivery_package.get("componentManifest")
        or {}
    )
    if not isinstance(manifest, dict):
        manifest = {}
    artifact_paths = _artifact_paths_from_run(run)
    artifact_path = (
        manifest.get("artifactPath")
        or manifest.get("artifact_path")
        or artifact_paths.get("article_component_manifest.json")
        or artifact_paths.get("article_component_manifest")
        or artifact_paths.get("component_manifest")
    )
    components = manifest.get("components")
    if not isinstance(components, list):
        components = []
    if not components and not artifact_path:
        return None
    return {
        **manifest,
        "components": components,
        "artifactPath": artifact_path,
    }


def _live_preview_from_run(run):
    result = (run.result or {}) if run else {}
    payload = result.get("livePreview") or result.get("live_preview") or {}
    if not isinstance(payload, dict):
        payload = {}
    if run and payload:
        payload = _rewrite_live_preview_payload_for_browser(run.run_id, payload)
    return {
        "available": bool(payload.get("available")),
        "status": payload.get("status") or "not_started",
        "previewUrl": payload.get("previewUrl") or payload.get("preview_url") or "",
        "internalPreviewUrl": payload.get("internalPreviewUrl") or payload.get("internal_preview_url") or "",
        "proxyPath": payload.get("proxyPath") or payload.get("proxy_path") or "",
        "routePath": payload.get("routePath") or payload.get("route_path") or "",
        "exactRender": bool(payload.get("exactRender") or payload.get("exact_render")),
        "inspectorProtocolVersion": payload.get("inspectorProtocolVersion") or payload.get("inspector_protocol_version"),
        "inspectorMode": payload.get("inspectorMode") or payload.get("inspector_mode") or "",
        "error": payload.get("error") or "",
        "errorCode": payload.get("errorCode") or payload.get("error_code") or "",
        "retryable": bool(payload.get("retryable", True)),
        "workspacePath": payload.get("workspacePath") or payload.get("workspace_path") or "",
        "logPath": payload.get("logPath") or payload.get("log_path") or "",
        "failedPhase": payload.get("failedPhase") or payload.get("failed_phase") or "",
        "failedCommand": payload.get("failedCommand") or payload.get("failed_command") or "",
        "logExcerpt": payload.get("logExcerpt") or payload.get("log_excerpt") or "",
        "proofWarnings": payload.get("proofWarnings") or payload.get("proof_warnings") or [],
        "browserWarnings": payload.get("browserWarnings") or payload.get("browser_warnings") or [],
        "assetWarnings": payload.get("assetWarnings") or payload.get("asset_warnings") or [],
        "proofAttempts": payload.get("proofAttempts") or payload.get("proof_attempts") or [],
        "verificationSkippedForPreview": bool(
            payload.get("verificationSkippedForPreview")
            or payload.get("verification_skipped_for_preview")
        ),
        "renderMode": payload.get("renderMode") or payload.get("render_mode") or "",
        "renderConfidence": payload.get("renderConfidence") or payload.get("render_confidence") or "",
    }


def _content_factory_live_preview_proxy_prefix(run_id):
    return f"/api/runs/{run_id}/live-preview/proxy"


def _backend_live_preview_proxy_prefix(run_id):
    return f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/proxy"


def _backend_public_base_url():
    base_url = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").strip().rstrip("/")
    if base_url.startswith(("http://", "https://")):
        return base_url
    return ""


def _proxy_suffix_from_content_factory_payload(run_id, payload):
    proxy_path = str(payload.get("proxyPath") or payload.get("proxy_path") or "").strip()
    if not proxy_path:
        preview_url = str(payload.get("previewUrl") or payload.get("preview_url") or "").strip()
        if preview_url:
            parsed = urlsplit(preview_url)
            proxy_path = parsed.path
            if parsed.query:
                proxy_path = f"{proxy_path}?{parsed.query}"
    if not proxy_path:
        return ""
    path_part, _, query_part = proxy_path.partition("?")
    suffix = ""
    for prefix in (_content_factory_live_preview_proxy_prefix(run_id), _backend_live_preview_proxy_prefix(run_id)):
        if path_part.startswith(prefix):
            suffix = path_part[len(prefix):] or "/"
            break
    if not suffix:
        suffix = path_part if path_part.startswith("/") else f"/{path_part}"
    if query_part:
        suffix = f"{suffix}?{query_part}"
    return suffix


def _browser_live_preview_url(run_id, payload):
    suffix = _proxy_suffix_from_content_factory_payload(run_id, payload)
    if not suffix:
        return ""
    base_url = _backend_public_base_url()
    return f"{base_url}{_backend_live_preview_proxy_prefix(run_id)}{suffix}"


def _rewrite_live_preview_payload_for_browser(run_id, payload):
    if not isinstance(payload, dict):
        return payload
    rewritten = dict(payload)
    internal_preview_url = (
        rewritten.get("internalPreviewUrl")
        or rewritten.get("internal_preview_url")
        or rewritten.get("previewUrl")
        or rewritten.get("preview_url")
        or ""
    )
    browser_url = _browser_live_preview_url(run_id, rewritten)
    if browser_url:
        rewritten["internalPreviewUrl"] = internal_preview_url
        rewritten["previewUrl"] = browser_url
        rewritten["proxyPath"] = _proxy_suffix_from_content_factory_payload(run_id, rewritten)
    return rewritten


def _persist_live_preview_payload(run, payload):
    if isinstance(payload, dict) and payload:
        payload = _rewrite_live_preview_payload_for_browser(run.run_id, payload)
        result = dict(run.result or {})
        result["livePreview"] = payload
        run.result = result
        run.save(update_fields=["result", "updated_at"])
    return run


def _article_preview_should_auto_prepare(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return False
    if run.status != ContentFactoryRunStatus.COMPLETED:
        return False
    if not _component_manifest_from_run(run):
        return False

    live_preview = _live_preview_from_run(run)
    if live_preview.get("available") and live_preview.get("previewUrl"):
        return False
    preview_status = str(live_preview.get("status") or "").strip().lower()
    if preview_status in {"running", "starting"}:
        return False
    if preview_status in {"failed", "blocked"} or live_preview.get("error"):
        return False
    return True


def _article_preview_should_refresh(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return False
    if run.status != ContentFactoryRunStatus.COMPLETED:
        return False
    if not _component_manifest_from_run(run):
        return False
    live_preview = _live_preview_from_run(run)
    if live_preview.get("available") and live_preview.get("previewUrl"):
        return False
    if live_preview.get("error"):
        return False
    preview_status = str(live_preview.get("status") or "").strip().lower()
    return preview_status in {"running", "starting"}


def _live_preview_github_token_payload(run):
    domain = normalize_company_domain(getattr(run, "domain", "") or "")
    github_repo = str(getattr(run, "github_repo", "") or "").strip()
    if not domain or not github_repo:
        return {}
    try:
        github_token = ensure_valid_org_token(domain)
    except (ArticleGenerationError, TokenRefreshError) as exc:
        logger.warning(
            "content_factory_live_preview_token_unavailable run_id=%s domain=%s github_repo=%s error=%s",
            getattr(run, "run_id", ""),
            domain,
            github_repo,
            exc,
        )
        return {}
    github_token = str(github_token or "").strip()
    return {"github_token": github_token} if github_token else {}


def _ensure_article_live_preview(run):
    if _article_preview_should_refresh(run):
        payload = _call_content_factory_live_preview(run_id=run.run_id, method="GET")
        return _persist_live_preview_payload(run, payload)
    if not _article_preview_should_auto_prepare(run):
        return run
    logger.info(
        "content_factory_live_preview_auto_start run_id=%s workflow=%s",
        run.run_id,
        run.workflow,
    )
    payload = _call_content_factory_live_preview(
        run_id=run.run_id,
        method="POST",
        payload={"force": False, **_live_preview_github_token_payload(run)},
    )
    if isinstance(payload, dict) and payload.get("error"):
        logger.warning(
            "content_factory_live_preview_auto_start_failed run_id=%s workflow=%s error=%s",
            run.run_id,
            run.workflow,
            payload.get("error"),
        )
    return _persist_live_preview_payload(run, payload)


def _serialize_component_comment(comment):
    return {
        "id": str(comment.id),
        "componentId": comment.component_id,
        "componentType": comment.component_type,
        "componentLabel": comment.component_label,
        "sourceSectionId": comment.source_section_id,
        "selector": comment.selector,
        "anchor": comment.anchor or None,
        "body": comment.body,
        "status": comment.status,
        "batchId": comment.batch_id or None,
        "createdAt": comment.created_at.isoformat() if comment.created_at else None,
        "updatedAt": comment.updated_at.isoformat() if comment.updated_at else None,
    }


def _component_feedback_from_run(run):
    run_request = run.run_request if isinstance(run.run_request, dict) else {}
    result = run.result or {}
    source_run_id = (
        run_request.get("source_run_id")
        or run_request.get("sourceRunId")
        or result.get("source_run_id")
        or result.get("sourceRunId")
    )
    batch_source_run = run
    if run.workflow == "article_revision" and source_run_id:
        batch_source_run = ContentFactoryRun.objects.filter(run_id=source_run_id).first() or run
    comments = list(
        VibeMarketingComponentComment.objects.filter(run=run).order_by("created_at", "id")
    )
    latest_batch = result.get("component_feedback_latest_batch")
    if not isinstance(latest_batch, dict):
        source_result = batch_source_run.result if isinstance(batch_source_run.result, dict) else {}
        latest_batch = source_result.get("component_feedback_latest_batch")
    if not isinstance(latest_batch, dict) and run.workflow == "article_revision":
        feedback_batch_id = str(result.get("feedback_batch_id") or result.get("feedbackBatchId") or "").strip()
        if feedback_batch_id:
            latest_batch = {
                "id": feedback_batch_id,
                "sourceRunId": source_run_id or batch_source_run.run_id,
                "revisionRunId": run.run_id,
                "status": "running",
            }
    if not isinstance(latest_batch, dict):
        submitted_comments = list(
            VibeMarketingComponentComment.objects.filter(run=batch_source_run)
            .exclude(batch_id="")
            .order_by("created_at", "id")
        )
        submitted = [comment for comment in submitted_comments if comment.batch_id]
        latest_comment = submitted[-1] if submitted else None
        latest_batch = (
            {
                "id": latest_comment.batch_id,
                "sourceRunId": batch_source_run.run_id,
                "revisionRunId": result.get("component_feedback_revision_run_id")
                or (run.run_id if run.workflow == "article_revision" else None),
                "status": "submitted",
            }
            if latest_comment
            else None
        )
    if run.workflow == "article_revision" and isinstance(latest_batch, dict):
        if latest_batch.get("status") == "accepted":
            batch_status = "accepted"
        elif run.status == ContentFactoryRunStatus.COMPLETED:
            batch_status = "completed"
        elif run.status == ContentFactoryRunStatus.FAILED:
            batch_status = "failed"
        else:
            batch_status = latest_batch.get("status", "running")
        latest_batch = {
            **latest_batch,
            "revisionRunId": latest_batch.get("revisionRunId") or run.run_id,
            "status": batch_status,
        }
    return {
        "comments": [_serialize_component_comment(comment) for comment in comments],
        "latestBatch": latest_batch,
    }


def _selector_for_component(component_id):
    escaped = str(component_id or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'[data-cf-component-id="{escaped}"]' if escaped else ""


def _component_revision_requested_run_id(source_run_id, batch_id):
    digest = hashlib.sha256(f"{source_run_id}:{batch_id}".encode("utf-8")).hexdigest()[:16]
    return f"component-revision-{digest}"


def _clamp_comment_anchor_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _comment_anchor_from_request(data):
    getter = data.get if hasattr(data, "get") else (lambda _key, default=None: default)
    anchor = getter("anchor")
    if isinstance(anchor, str):
        try:
            anchor = json.loads(anchor)
        except json.JSONDecodeError:
            anchor = None
    if not isinstance(anchor, dict):
        x_value = getter("anchorX") or getter("anchor_x")
        y_value = getter("anchorY") or getter("anchor_y")
        if x_value is None and y_value is None:
            return {}
        anchor = {"x": x_value, "y": y_value}
    x = _clamp_comment_anchor_number(anchor.get("x"))
    y = _clamp_comment_anchor_number(anchor.get("y"))
    if x is None or y is None:
        return {}
    created_from = str(anchor.get("createdFrom") or anchor.get("created_from") or "").strip()
    normalized = {"x": x, "y": y}
    if created_from:
        normalized["createdFrom"] = created_from[:80]
    return normalized


def _request_includes_comment_anchor(data):
    getter = data.get if hasattr(data, "get") else (lambda _key, default=None: default)
    return getter("anchor") not in (None, "") or getter("anchorX") not in (None, "") or getter("anchor_x") not in (None, "")


def _comment_payload_from_request(data):
    component_id = str(data.get("componentId") or data.get("component_id") or "").strip()
    body = str(data.get("body") or data.get("comment") or "").strip()
    return {
        "component_id": component_id,
        "component_type": str(data.get("componentType") or data.get("component_type") or "").strip(),
        "component_label": str(data.get("componentLabel") or data.get("component_label") or "").strip(),
        "source_section_id": str(data.get("sourceSectionId") or data.get("source_section_id") or "").strip(),
        "selector": str(data.get("selector") or "").strip() or _selector_for_component(component_id),
        "anchor": _comment_anchor_from_request(data),
        "body": body,
    }


def _remote_comment_payload(comment):
    return {
        "comment_id": str(comment.id),
        "component_id": comment.component_id,
        "component_type": comment.component_type,
        "component_label": comment.component_label,
        "source_section_id": comment.source_section_id,
        "selector": comment.selector or _selector_for_component(comment.component_id),
        "anchor": comment.anchor or {},
        "body": comment.body,
    }


def _normalized_component_feedback_rule(comment):
    label = comment.component_label or comment.component_id
    component_type = comment.component_type or "component"
    body = " ".join(str(comment.body or "").split())
    if not body:
        return ""
    return f"For {component_type} components like {label}, apply this reviewer guidance: {body}"


def _feedback_family_key(*, domain, github_repo, comment):
    seed = json.dumps(
        {
            "domain": normalize_company_domain(domain),
            "repo": github_repo or "",
            "component_type": comment.component_type or "",
            "component_id": comment.component_id or "",
            "body": " ".join(str(comment.body or "").lower().split()),
        },
        sort_keys=True,
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _create_editorial_feedback_candidates(*, organization, run, comments, batch_id):
    for comment in comments:
        rule = _normalized_component_feedback_rule(comment)
        if not rule:
            continue
        family_key = _feedback_family_key(
            domain=run.domain,
            github_repo=run.github_repo,
            comment=comment,
        )
        exact_signature = hashlib.sha256(str(comment.body or "").encode("utf-8")).hexdigest()[:32]
        summary = f"{comment.component_label or comment.component_id}: {comment.body[:180]}"
        ContentFactoryHealingRecord.objects.update_or_create(
            domain=normalize_company_domain(run.domain),
            github_repo=run.github_repo or "",
            failure_kind="article_component_feedback",
            failure_family_key=family_key,
            defaults={
                "organization": organization,
                "exact_signature": exact_signature,
                "summary": summary[:500],
                "normalized_failure": {
                    "feedback_batch_id": batch_id,
                    "comment_id": str(comment.id),
                    "source_run_id": run.run_id,
                    "component_id": comment.component_id,
                    "component_type": comment.component_type,
                    "component_label": comment.component_label,
                    "comment": comment.body,
                    "normalized_rule": rule,
                },
                "changed_files": [],
                "patch_manifest": {},
                "validation_results": {},
                "evidence_artifacts": {
                    "feedback_batch_id": batch_id,
                    "source_run_id": run.run_id,
                    "comment_id": str(comment.id),
                },
                "snippet_or_rule": rule,
                "applies_to": [
                    item
                    for item in [
                        f"component:{comment.component_id}" if comment.component_id else "",
                        f"component_type:{comment.component_type}" if comment.component_type else "",
                        "article_copy",
                    ]
                    if item
                ],
                "promoted_payload": {
                    "feedback_batch_id": batch_id,
                    "source_run_id": run.run_id,
                    "comment_id": str(comment.id),
                    "article_component_feedback_hint": {
                        "component_id": comment.component_id,
                        "component_type": comment.component_type,
                        "component_label": comment.component_label,
                        "rule": rule,
                    },
                },
                "promotion_state": ContentFactoryHealingPromotionState.CANDIDATE,
                "latest_run_id": run.run_id,
            },
        )


def _promote_editorial_feedback_batch(*, run, batch_id, revision_run_id=""):
    if not batch_id:
        return 0
    records = ContentFactoryHealingRecord.objects.filter(
        domain=normalize_company_domain(run.domain),
        github_repo=run.github_repo or "",
        failure_kind="article_component_feedback",
    )
    promoted_count = 0
    now_iso = timezone.now().isoformat()
    for record in records:
        evidence = record.evidence_artifacts if isinstance(record.evidence_artifacts, dict) else {}
        promoted_payload = record.promoted_payload if isinstance(record.promoted_payload, dict) else {}
        if evidence.get("feedback_batch_id") != batch_id and promoted_payload.get("feedback_batch_id") != batch_id:
            continue
        promoted_payload = {
            **promoted_payload,
            "accepted_at": now_iso,
            "revision_run_id": revision_run_id,
        }
        record.promoted_payload = promoted_payload
        record.promotion_state = ContentFactoryHealingPromotionState.PROMOTED
        record.latest_run_id = revision_run_id or run.run_id
        record.save(update_fields=["promoted_payload", "promotion_state", "latest_run_id", "updated_at"])
        promoted_count += 1
    return promoted_count


def _publish_evidence_from_run(run):
    if not run:
        return {}
    result = run.result or {}
    diagnostics = result.get("diagnostics") or run.verification_summary or {}
    return {
        "runId": run.run_id,
        "status": run.status,
        "approvalState": run.approval_state,
        "previewUrl": result.get("preview_url") or result.get("article_url") or result.get("url"),
        "prUrl": result.get("pr_url") or result.get("pull_request_url"),
        "routePath": result.get("route_path") or result.get("path"),
        "screenshots": result.get("screenshots") or diagnostics.get("screenshots") or [],
        "changedFiles": result.get("changed_files") or result.get("files") or diagnostics.get("changed_files") or [],
        "warnings": result.get("warnings") or run.acceptance_summary.get("warnings") or [],
        "diagnostics": diagnostics,
        "contentPackage": _content_package_from_run(run),
    }


def _run_has_external_publish_evidence(run):
    result = _run_mapping(run.result)
    evidence = _publish_evidence_from_run(run)
    return bool(
        evidence.get("prUrl")
        or result.get("draft_pr_url")
        or result.get("draftPrUrl")
        or result.get("draft_pr_number")
        or result.get("pull_request_url")
        or result.get("pullRequestUrl")
    )


def _article_keyword_from_run(run):
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    package = _content_package_from_run(run) or {}
    return str(
        run_request.get("target_keyword")
        or run_request.get("targetKeyword")
        or result.get("target_keyword")
        or result.get("targetKeyword")
        or package.get("targetKeyword")
        or ""
    ).strip()


def _release_cancelled_article_keyword(organization, run):
    keyword_text = _article_keyword_from_run(run)
    keyword_key = _normalize_keyword_memory(keyword_text)
    if not keyword_key:
        return None
    keyword = ResearchedKeyword.objects.filter(
        organization=organization,
        keyword_normalized=keyword_key,
        status=KeywordStatus.IN_PROGRESS,
    ).first()
    if not keyword or keyword.written_article_id:
        return None
    keyword.status = KeywordStatus.PENDING
    keyword.status_changed_at = timezone.now()
    keyword.save(update_fields=["status", "status_changed_at"])
    return keyword


def _cancel_local_article_run(*, run, organization, remote_data=None):
    remote_data = remote_data if isinstance(remote_data, dict) else {}
    now = timezone.now()
    warnings = []
    if remote_data.get("error") and remote_data.get("retryable"):
        warnings.append(str(remote_data.get("error")))
    cleanup = remote_data.get("cleanup") if isinstance(remote_data.get("cleanup"), dict) else {}

    _release_cancelled_article_keyword(organization, run)

    with transaction.atomic():
        locked_run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        VibeMarketingComponentComment.objects.filter(run=locked_run).delete()
        locked_run.steps.update(
            status=ContentFactoryStepStatus.CANCELLED,
            message="Cancelled by user.",
            error="",
            artifacts=[],
        )
        locked_run.status = ContentFactoryRunStatus.CANCELLED
        locked_run.current_step = "cancelled"
        locked_run.approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        locked_run.resume_available = False
        locked_run.acceptance_summary = {}
        locked_run.verification_summary = {}
        locked_run.error = ""
        locked_run.result = {
            "status": ContentFactoryRunStatus.CANCELLED,
            "cancelled": True,
            "cancelled_at": now.isoformat(),
            "cleanup": cleanup,
            "warnings": warnings,
        }
        locked_run.save(
            update_fields=[
                "status",
                "current_step",
                "approval_state",
                "resume_available",
                "acceptance_summary",
                "verification_summary",
                "error",
                "result",
                "updated_at",
            ]
        )
        return locked_run


def _latest_baseline_snapshot(organization):
    return WebsiteBaselineSnapshot.objects.filter(organization=organization).order_by("-collected_at", "-created_at").first()


def _parse_baseline_collected_at(value):
    parsed = _parse_remote_datetime(value)
    return parsed or timezone.now()


def _baseline_payload_from_result(result):
    if not isinstance(result, dict):
        return {}
    baseline = result.get("baseline")
    return baseline if isinstance(baseline, dict) else {}


def _persist_baseline_snapshot_from_payload(*, organization, run=None, baseline=None, status_value="completed"):
    baseline = baseline if isinstance(baseline, dict) else _baseline_payload_from_result((run.result if run else {}) or {})
    if not baseline:
        return None
    collected_at = _parse_baseline_collected_at(baseline.get("collectedAt") or baseline.get("collected_at"))
    source_status = baseline.get("sourceStatus") or baseline.get("source_status") or {}
    metrics = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}
    recommendations = baseline.get("recommendations") if isinstance(baseline.get("recommendations"), list) else []
    summary_text = baseline.get("summary") or ""
    summary = summary_text if isinstance(summary_text, dict) else {"text": str(summary_text or "")}
    raw_score = baseline.get("overallScore") if baseline.get("overallScore") is not None else baseline.get("overall_score")
    try:
        overall_score = int(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        overall_score = None
    snapshot, _created = WebsiteBaselineSnapshot.objects.update_or_create(
        organization=organization,
        run_id=str((run.run_id if run else baseline.get("runId") or baseline.get("run_id")) or ""),
        defaults={
            "domain": normalize_company_domain(baseline.get("domain") or organization.domain),
            "status": status_value,
            "collected_at": collected_at,
            "overall_score": overall_score,
            "summary": summary,
            "metrics": metrics,
            "source_status": source_status if isinstance(source_status, dict) else {},
            "recommendations": recommendations,
            "raw_payload": baseline,
        },
    )
    return snapshot


def _baseline_is_fresh(snapshot) -> bool:
    if not snapshot:
        return False
    return snapshot.collected_at >= timezone.now() - timedelta(days=BASELINE_FRESH_DAYS)


def _baseline_requirement_satisfied(config, snapshot) -> bool:
    return bool(config.baseline_skipped_at or _baseline_is_fresh(snapshot))


def _serialize_baseline_snapshot(snapshot, config=None):
    if not snapshot:
        return {
            "status": "missing",
            "passed": bool(config and config.baseline_skipped_at),
            "skipped": bool(config and config.baseline_skipped_at),
            "skippedAt": config.baseline_skipped_at.isoformat() if config and config.baseline_skipped_at else None,
            "skipReason": config.baseline_skip_reason if config else "",
        }
    return {
        "id": snapshot.id,
        "runId": snapshot.run_id,
        "domain": snapshot.domain,
        "status": snapshot.status,
        "passed": _baseline_requirement_satisfied(config, snapshot) if config else _baseline_is_fresh(snapshot),
        "stale": not _baseline_is_fresh(snapshot),
        "collectedAt": snapshot.collected_at.isoformat(),
        "overallScore": snapshot.overall_score,
        "summary": snapshot.summary,
        "metrics": snapshot.metrics,
        "sourceStatus": snapshot.source_status,
        "recommendations": snapshot.recommendations,
        "skipped": bool(config and config.baseline_skipped_at),
        "skippedAt": config.baseline_skipped_at.isoformat() if config and config.baseline_skipped_at else None,
        "skipReason": config.baseline_skip_reason if config else "",
    }


def _merge_google_metrics_into_baseline(snapshot, google_metrics):
    if not snapshot:
        return None
    payload = dict(snapshot.raw_payload or {})
    metrics = dict(payload.get("metrics") or snapshot.metrics or {})
    source_status = dict(payload.get("sourceStatus") or snapshot.source_status or {})
    traffic = (google_metrics or {}).get("traffic") or {}
    metrics["traffic"] = traffic
    source_status["traffic"] = traffic.get("status", "unavailable")
    for key, value in ((google_metrics or {}).get("sourceStatus") or {}).items():
        source_status[key] = value
    payload["metrics"] = metrics
    payload["sourceStatus"] = source_status
    payload["googleEnrichedAt"] = timezone.now().isoformat()
    snapshot.metrics = metrics
    snapshot.source_status = source_status
    snapshot.raw_payload = payload
    snapshot.save(update_fields=["metrics", "source_status", "raw_payload", "updated_at"])
    return snapshot


def _google_baseline_connect_url(request):
    if request is None:
        return ""
    for setting_name in ("FOUNDER_TOOLS_URL", "VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            frontend_base_url = value.rstrip("/")
            break
    else:
        frontend_base_url = "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"
    next_path = "/founder-tools/marketing/create?step=baseline&googleBaseline=refresh"
    next_url = f"{frontend_base_url}{next_path}"
    query = urlencode({"scope": "website_baseline", "next": next_url})
    return request.build_absolute_uri(f"/integrations/connect/google?{query}")


def _serialize_startup_profile(organization):
    try:
        profile = organization.startup_profile
    except Exception:
        return {
            "founderNames": [],
            "stage": "",
            "organizationKind": "",
            "notes": "",
            "companyAliases": [],
            "domainAliases": [],
        }
    return {
        "founderNames": list(profile.founder_names or []),
        "stage": profile.stage,
        "organizationKind": getattr(profile, "organization_kind", ""),
        "notes": profile.notes,
        "companyAliases": list(profile.company_aliases or []),
        "domainAliases": list(profile.domain_aliases or []),
        "competitorDomains": list(profile.competitor_domains or []),
        "positiveKeywords": list(profile.positive_keywords or []),
    }


def _run_mapping(value):
    return value if isinstance(value, dict) else {}


def _run_source_run_id(run):
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    return str(
        run_request.get("source_run_id")
        or run_request.get("sourceRunId")
        or result.get("source_run_id")
        or result.get("sourceRunId")
        or ""
    ).strip()


def _run_delivery_mode(run):
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    return str(
        run_request.get("resolved_delivery_mode")
        or run_request.get("delivery_mode")
        or result.get("resolved_delivery_mode")
        or result.get("delivery_mode")
        or ""
    ).strip()


def _workflow_step_action(label, *, href=None, intent=None, variant="primary"):
    payload = {"label": label, "variant": variant}
    if href:
        payload["href"] = href
    if intent:
        payload["intent"] = intent
    return payload


def _run_url(run):
    return f"/founder-tools/marketing/runs/{run.run_id}" if run else ""


def _latest_publish_child_run(latest_runs, article_run):
    if not article_run:
        return None
    explicit_child_run_id = str(_run_mapping(article_run.result).get("publish_child_run_id") or "").strip()
    if explicit_child_run_id:
        for candidate in latest_runs or []:
            if candidate.run_id == explicit_child_run_id:
                return candidate
    candidates = []
    for candidate in latest_runs or []:
        if candidate.run_id == article_run.run_id or candidate.workflow not in ARTICLE_WORKFLOWS:
            continue
        if _run_source_run_id(candidate) != article_run.run_id:
            continue
        if candidate.workflow == "article_revision":
            continue
        delivery_mode = _run_delivery_mode(candidate)
        if delivery_mode and delivery_mode not in {"publish_code", "publish_webflow"}:
            continue
        candidates.append(candidate)
    return candidates[0] if candidates else None


def _run_can_promote_package(run, config=None):
    if not run:
        return False
    result = _run_mapping(run.result)
    if result.get("promote_bundle_url") or result.get("publish_pr_url"):
        return True
    delivery_mode = _run_delivery_mode(run)
    if delivery_mode in {"content_only", "review_draft"} or run.workflow == "article_revision":
        return bool(config and config.github_connection_state == "connected" and config.github_repo)
    return False


def _workflow_progress_context(*, context=None, run=None, latest_runs=None, checks=None):
    organization = context.organization if context is not None else None
    if organization is None and run is not None and run.domain:
        organization = Organization.objects.filter(domain__iexact=normalize_company_domain(run.domain)).first()
    if organization is not None and context is not None:
        config = _get_config(organization)
    elif organization is not None:
        config = OrganizationContentConfig.objects.filter(organization=organization).first()
    else:
        config = None
    if latest_runs is None:
        latest_runs = _latest_runs_for_org(organization, limit=12) if organization is not None else []
    latest_runs = list(latest_runs or [])
    if run is not None and not any(candidate.pk == run.pk for candidate in latest_runs):
        latest_runs.insert(0, run)
    source_run_id = _run_source_run_id(run) if run is not None else ""
    if source_run_id and not any(candidate.run_id == source_run_id for candidate in latest_runs):
        source_run = ContentFactoryRun.objects.filter(run_id=source_run_id).prefetch_related("steps").first()
        if source_run is not None:
            latest_runs.append(source_run)
    if checks is None and organization is not None and config is not None:
        checks = _profile_checks(organization, config, latest_runs, _latest_baseline_snapshot(organization))
    return organization, config, latest_runs, checks or {}


def _workflow_progress(*, context=None, run=None, latest_runs=None, checks=None, topic_candidates=None):
    organization, config, latest_runs, checks = _workflow_progress_context(
        context=context,
        run=run,
        latest_runs=latest_runs,
        checks=checks,
    )
    scan_run = run if run and run.workflow in SCAN_WORKFLOWS else _latest_run_matching(latest_runs, SCAN_WORKFLOWS)
    discovery_run = run if run and run.workflow in DISCOVERY_WORKFLOWS else _latest_run_matching(latest_runs, DISCOVERY_WORKFLOWS)
    article_run = run if run and run.workflow in ARTICLE_WORKFLOWS else _latest_run_matching(latest_runs, ARTICLE_WORKFLOWS)
    active_run_source_id = _run_source_run_id(run) if run else ""
    active_run_delivery_mode = _run_delivery_mode(run) if run else ""
    active_run_is_publish_child = bool(
        run
        and run.workflow in ARTICLE_WORKFLOWS
        and run.workflow != "article_revision"
        and active_run_source_id
        and (not active_run_delivery_mode or active_run_delivery_mode in {"publish_code", "publish_webflow"})
    )
    if active_run_is_publish_child:
        source_run = next((candidate for candidate in latest_runs if candidate.run_id == active_run_source_id), None)
        article_run = source_run or article_run
        publish_child_run = run
    else:
        publish_child_run = _latest_publish_child_run(latest_runs, article_run)
    publish_evidence_run = publish_child_run or article_run
    publish_evidence = _publish_evidence_from_run(publish_evidence_run)
    content_package = _content_package_from_run(article_run) if article_run else None
    content_package_ready = bool(content_package and content_package.get("contentPackaged"))
    component_feedback = _component_feedback_from_run(article_run) if article_run else {"comments": [], "latestBatch": None}
    comments = component_feedback.get("comments") or []
    latest_batch = component_feedback.get("latestBatch") or {}
    draft_comment_count = len([comment for comment in comments if comment.get("status") == "draft" and str(comment.get("body") or "").strip()])
    submitted_revision_pending = bool(
        latest_batch
        and latest_batch.get("status") in {"submitted", "running", "failed"}
        and not latest_batch.get("revisionRunId")
    )
    revision_running = bool(
        article_run
        and article_run.workflow == "article_revision"
        and article_run.status in RUNNING_RUN_STATUSES
    )
    revision_needs_acceptance = bool(
        article_run
        and article_run.workflow == "article_revision"
        and article_run.status == ContentFactoryRunStatus.COMPLETED
        and latest_batch.get("status") != "accepted"
    )
    package_can_promote = _run_can_promote_package(article_run, config=config)
    publish_complete = bool(publish_evidence.get("previewUrl") or publish_evidence.get("prUrl"))
    publish_running = bool(publish_child_run and publish_child_run.status in RUNNING_RUN_STATUSES)
    review_surface_ready = bool(content_package_ready and article_run and _component_manifest_from_run(article_run))
    review_is_finished = bool(
        publish_complete
        or publish_running
        or draft_comment_count
        or submitted_revision_pending
        or revision_running
        or revision_needs_acceptance
        or latest_batch.get("status") == "accepted"
    )

    status_by_id = {}
    action_by_id = {}
    href_by_id = {}
    run_by_id = {}
    summary_by_id = {}

    for step_def in WORKFLOW_STEP_DEFS:
        step_id = step_def["id"]
        check_key = step_def.get("check")
        status_by_id[step_id] = "complete" if check_key and checks.get(check_key, {}).get("passed") else "locked"
        href_by_id[step_id] = step_def["href"]
        summary_by_id[step_id] = step_def["summary"]

    if not checks.get("websiteProfile", {}).get("passed"):
        status_by_id["profile"] = "needs_action"
        action_by_id["profile"] = _workflow_step_action("Save startup profile", href=href_by_id["profile"])
    if checks.get("websiteProfile", {}).get("passed") and not checks.get("baseline", {}).get("passed"):
        status_by_id["baseline"] = "ready"
        action_by_id["baseline"] = _workflow_step_action("Run or skip baseline", href=href_by_id["baseline"])
    if checks.get("baseline", {}).get("passed") and not checks.get("github", {}).get("passed"):
        status_by_id["repo"] = "ready"
        action_by_id["repo"] = _workflow_step_action("Connect GitHub", href=href_by_id["repo"])

    if scan_run and not checks.get("scaffold", {}).get("passed"):
        run_by_id["article_system"] = scan_run.run_id
        href_by_id["article_system"] = _run_url(scan_run)
        if scan_run.status in RUNNING_RUN_STATUSES:
            status_by_id["article_system"] = "running"
            summary_by_id["article_system"] = "Repository scan is running."
        elif scan_run.status in FAILED_RUN_STATUSES:
            status_by_id["article_system"] = "blocked"
            action_by_id["article_system"] = _workflow_step_action("Open scan run", href=_run_url(scan_run))
        elif scan_run.status in {ContentFactoryRunStatus.AWAITING_CONFIRMATION, ContentFactoryRunStatus.AWAITING_APPROVAL, ContentFactoryRunStatus.APPROVAL_REQUIRED}:
            status_by_id["article_system"] = "needs_action"
            action_by_id["article_system"] = _workflow_step_action("Review scaffold approval", href=_run_url(scan_run))
    if checks.get("github", {}).get("passed") and not checks.get("scaffold", {}).get("passed") and status_by_id["article_system"] == "locked":
        status_by_id["article_system"] = "ready"
        action_by_id["article_system"] = _workflow_step_action("Run repository scan", href=href_by_id["article_system"])

    if discovery_run and not checks.get("research", {}).get("passed"):
        run_by_id["research"] = discovery_run.run_id
        href_by_id["research"] = _run_url(discovery_run)
        if discovery_run.status in RUNNING_RUN_STATUSES:
            status_by_id["research"] = "running"
            summary_by_id["research"] = "Topic discovery is running."
        elif discovery_run.status in FAILED_RUN_STATUSES:
            status_by_id["research"] = "blocked"
            action_by_id["research"] = _workflow_step_action("Open research run", href=_run_url(discovery_run))
    if checks.get("scaffold", {}).get("passed") and not checks.get("research", {}).get("passed") and status_by_id["research"] == "locked":
        status_by_id["research"] = "ready"
        action_by_id["research"] = _workflow_step_action("Start topic research", href=href_by_id["research"])

    if topic_candidates is None:
        topic_candidates = _topic_candidates_from_runs(latest_runs, organization=organization)
    if article_run:
        status_by_id["choose_topic"] = "complete"
        run_by_id["choose_topic"] = article_run.run_id
    elif checks.get("research", {}).get("passed"):
        status_by_id["choose_topic"] = "needs_action" if topic_candidates else "ready"
        action_by_id["choose_topic"] = _workflow_step_action("Choose article topic", href=href_by_id["choose_topic"])

    if article_run:
        run_by_id["generate"] = article_run.run_id
        href_by_id["generate"] = _run_url(article_run)
        if article_run.status in RUNNING_RUN_STATUSES:
            status_by_id["generate"] = "running"
            summary_by_id["generate"] = "Article generation is running."
        elif article_run.status in FAILED_RUN_STATUSES:
            status_by_id["generate"] = "blocked"
            action_by_id["generate"] = _workflow_step_action("Open failed article run", href=_run_url(article_run))
        elif checks.get("write", {}).get("passed") or content_package_ready:
            status_by_id["generate"] = "complete"
    elif status_by_id["choose_topic"] in {"ready", "needs_action"}:
        status_by_id["generate"] = "locked"

    if content_package_ready or (article_run and article_run.component_comments.exists()):
        run_by_id["review"] = article_run.run_id
        href_by_id["review"] = _run_url(article_run)
    if content_package_ready:
        status_by_id["review"] = "complete" if not review_surface_ready or review_is_finished else "ready"
        action_by_id["review"] = _workflow_step_action("Open live preview", href=_run_url(article_run))
    elif article_run and article_run.status in RUNNING_RUN_STATUSES:
        status_by_id["review"] = "locked"

    if draft_comment_count:
        status_by_id["revise"] = "needs_action"
        href_by_id["revise"] = _run_url(article_run)
        run_by_id["revise"] = article_run.run_id
        action_by_id["revise"] = _workflow_step_action("Send comments for AI revision", href=_run_url(article_run), intent="submit-component-comments")
        summary_by_id["revise"] = f"{draft_comment_count} draft comment{'s' if draft_comment_count != 1 else ''} ready."
    elif submitted_revision_pending or revision_running:
        status_by_id["revise"] = "running"
        href_by_id["revise"] = _run_url(article_run)
        run_by_id["revise"] = article_run.run_id
        summary_by_id["revise"] = "AI revision is running or ready to retry."
    elif revision_needs_acceptance:
        status_by_id["revise"] = "needs_action"
        href_by_id["revise"] = _run_url(article_run)
        run_by_id["revise"] = article_run.run_id
        action_by_id["revise"] = _workflow_step_action("Accept revised article", href=_run_url(article_run), intent="accept-component-revision")
    elif latest_batch.get("status") == "accepted":
        status_by_id["revise"] = "complete"
        href_by_id["revise"] = _run_url(article_run)
        run_by_id["revise"] = article_run.run_id

    if content_package_ready:
        status_by_id["package"] = "complete"
        href_by_id["package"] = _run_url(article_run)
        run_by_id["package"] = article_run.run_id
        title = content_package.get("title") or content_package.get("slug")
        summary_by_id["package"] = f"Article package ready{': ' + title if title else '.'}"
        action_by_id["package"] = _workflow_step_action("Review package", href=_run_url(article_run))

    if publish_complete:
        status_by_id["publish"] = "complete"
        href_by_id["publish"] = _run_url(publish_evidence_run)
        run_by_id["publish"] = publish_evidence_run.run_id
        action_by_id["publish"] = _workflow_step_action(
            "Open published evidence",
            href=publish_evidence.get("previewUrl") or publish_evidence.get("prUrl") or _run_url(publish_evidence_run),
        )
        summary_by_id["publish"] = "PR or preview evidence is ready."
    elif publish_running:
        status_by_id["publish"] = "running"
        href_by_id["publish"] = _run_url(publish_child_run)
        run_by_id["publish"] = publish_child_run.run_id
        summary_by_id["publish"] = "Publishing child run is in progress."
    elif content_package_ready and package_can_promote:
        status_by_id["publish"] = "ready"
        href_by_id["publish"] = _run_url(article_run)
        run_by_id["publish"] = article_run.run_id
        action_by_id["publish"] = _workflow_step_action("Publish to website", href=_run_url(article_run), intent="promote-bundle")
        summary_by_id["publish"] = "Promote the content package into a publish run."
    elif content_package_ready:
        status_by_id["publish"] = "blocked"
        href_by_id["publish"] = "/founder-tools/marketing/settings"
        action_by_id["publish"] = _workflow_step_action("Configure publishing", href="/founder-tools/marketing/settings")
        summary_by_id["publish"] = "Package is ready, but no publish target is connected."

    if checks.get("dailyAutomation", {}).get("passed"):
        status_by_id["automation"] = "complete"
    elif status_by_id["publish"] == "complete":
        status_by_id["automation"] = "ready"
        action_by_id["automation"] = _workflow_step_action("Enable daily automation", href=href_by_id["automation"])

    steps = []
    for index, step_def in enumerate(WORKFLOW_STEP_DEFS):
        step_id = step_def["id"]
        steps.append(
            {
                "id": step_id,
                "label": step_def["label"],
                "phase": step_def["phase"],
                "status": status_by_id.get(step_id, "locked"),
                "href": href_by_id.get(step_id) or step_def["href"],
                "runId": run_by_id.get(step_id),
                "summary": summary_by_id.get(step_id) or step_def["summary"],
                "primaryAction": action_by_id.get(step_id),
                "order": index + 1,
            }
        )

    active_statuses = {"blocked", "needs_action", "running", "ready"}
    current_step = next((step for step in steps if step["status"] in active_statuses), steps[-1])
    current_index = WORKFLOW_STEP_IDS.index(current_step["id"])
    next_step = next((step for step in steps[current_index + 1 :] if step["status"] != "locked"), None)
    return {
        "currentStepId": current_step["id"],
        "nextStepId": next_step["id"] if next_step else None,
        "steps": steps,
    }


def _serialize_run(run, *, context=None, latest_runs=None, checks=None):
    step_states = []
    for step in run.steps.order_by("display_order", "id"):
        step_states.append(
            {
                "key": step.step_key,
                "name": step.step_key.replace("_", " ").title(),
                "required": step.required,
                "status": step.status,
                "attempts": step.attempts,
                "message": step.message or "",
                "error": step.error or "",
                "artifacts": step.artifacts or [],
                "startedAt": step.started_at.isoformat() if step.started_at else None,
                "completedAt": step.completed_at.isoformat() if step.completed_at else None,
            }
        )

    result = run.result or {}
    preview_url = result.get("preview_url") or result.get("article_url") or result.get("url")
    pr_url = result.get("pr_url") or result.get("pull_request_url")
    content_package = _content_package_from_run(run)
    component_manifest = _component_manifest_from_run(run)
    live_preview = _live_preview_from_run(run)
    return {
        "runId": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "githubRepo": run.github_repo,
        "status": run.status,
        "currentStep": run.current_step,
        "approvalState": run.approval_state,
        "resumeAvailable": run.resume_available,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
        "stepOrder": run.step_order or [],
        "steps": step_states,
        "warnings": result.get("warnings") or run.acceptance_summary.get("warnings") or [],
        "errors": [run.error] if run.error else result.get("errors") or [],
        "artifacts": result.get("artifacts") or [],
        "previewUrl": preview_url,
        "prUrl": pr_url,
        "routePath": result.get("route_path") or result.get("path"),
        "diagnostics": result.get("diagnostics") or run.verification_summary or {},
        "contentPackage": content_package,
        "componentManifest": component_manifest,
        "livePreview": live_preview,
        "componentFeedback": _component_feedback_from_run(run),
        "workflowProgress": _workflow_progress(context=context, run=run, latest_runs=latest_runs, checks=checks),
        "result": result,
    }


def _profile_checks(organization, config, latest_runs=None, baseline_snapshot=None):
    latest_runs = latest_runs or []
    domain_ok = bool(normalize_company_domain(organization.domain))
    context_ok = bool(str(config.company_context or "").strip()) or bool(str(config.brand_name or "").strip())
    keywords_ok = bool(organization.competitors or organization.seed_keywords)
    baseline_ready = _baseline_requirement_satisfied(config, baseline_snapshot)
    github_ready = config.github_connection_state == "connected" and bool(config.github_repo)
    article_system = resolve_article_system(config)
    article_ready = article_system.get("state") in {
        "ready",
        "detected",
        "registry_driven_seo_ready",
        "article_system_ready",
    } or bool(config.publish_targets)
    scan_ready = bool(config.last_scanned_at or config.scan_summary or config.article_system or config.publish_targets)
    discovery_run = _latest_run_matching(latest_runs, DISCOVERY_WORKFLOWS)
    article_run = _latest_run_matching(latest_runs, ARTICLE_WORKFLOWS)
    topic_candidates = _topic_candidates_from_runs(latest_runs, organization=organization)
    research_ready = bool(topic_candidates) or bool(
        discovery_run and discovery_run.status in {
            ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            ContentFactoryRunStatus.COMPLETED,
        }
    )
    article_result = (article_run.result if article_run else {}) or {}
    article_acceptance_summary = (article_run.acceptance_summary if article_run else {}) or {}
    article_content_packaged = bool(article_acceptance_summary.get("content_packaged"))
    write_ready = bool(
        article_run
        and (
            article_run.status
            in {
                ContentFactoryRunStatus.COMPLETED,
                ContentFactoryRunStatus.AWAITING_APPROVAL,
                ContentFactoryRunStatus.APPROVAL_REQUIRED,
            }
            or article_result.get("article")
            or article_result.get("content")
            or article_result.get("markdown")
            or article_result.get("preview_url")
            or article_result.get("pr_url")
            or article_content_packaged
        )
    )
    publish_evidence = _publish_evidence_from_run(article_run)
    content_package = publish_evidence.get("contentPackage") or {}
    content_package_ready = bool(content_package.get("contentPackaged") or article_content_packaged)
    publish_ready = bool(
        article_run
        and (
            article_run.approval_state == ContentFactoryApprovalState.APPROVED
            or article_run.status == ContentFactoryRunStatus.COMPLETED
        )
        and (publish_evidence.get("previewUrl") or publish_evidence.get("prUrl"))
    )
    delivery_mode = _effective_article_delivery_mode(config, github_ready=github_ready, article_ready=article_ready)
    daily_ready = (
        domain_ok
        and context_ok
        and keywords_ok
        and baseline_ready
        and scan_ready
        and article_ready
        and bool(config.connected_slack_user_id)
        and (delivery_mode != "publish_code" or github_ready)
    )
    return {
        "account": {"passed": True},
        "websiteProfile": {
            "passed": domain_ok and context_ok and keywords_ok,
            "checks": {
                "domain": domain_ok,
                "brandOrContext": context_ok,
                "competitorsOrSeedKeywords": keywords_ok,
            },
        },
        "github": {
            "passed": github_ready,
            "connectionState": config.github_connection_state,
            "repoSet": bool(config.github_repo),
        },
        "baseline": {
            "passed": baseline_ready,
            "runId": baseline_snapshot.run_id if baseline_snapshot else None,
            "collectedAt": baseline_snapshot.collected_at.isoformat() if baseline_snapshot else None,
            "overallScore": baseline_snapshot.overall_score if baseline_snapshot else None,
            "stale": bool(baseline_snapshot and not _baseline_is_fresh(baseline_snapshot)),
            "skipped": bool(config.baseline_skipped_at),
        },
        "scan": {"passed": scan_ready},
        "scaffold": {"passed": article_ready, "articleSystem": article_system},
        "research": {"passed": research_ready, "runId": discovery_run.run_id if discovery_run else None},
        "write": {"passed": write_ready, "runId": article_run.run_id if article_run else None},
        "contentPackage": {"passed": content_package_ready, "runId": article_run.run_id if article_run else None},
        "publish": {"passed": publish_ready, "runId": article_run.run_id if article_run else None},
        "dailyAutomation": {"passed": daily_ready},
    }


def _serialize_bootstrap(context, request=None):
    config = _get_config(context.organization)
    latest_runs = _latest_runs_for_org(context.organization)
    declined_topic_feedback = list_topic_feedback(context.organization, feedback_type="declined", limit=100)
    declined_keyword_keys = {
        normalize_topic_feedback_keyword(item.keyword)
        for item in declined_topic_feedback
        if normalize_topic_feedback_keyword(item.keyword)
    }
    coverage_memory = build_topic_coverage_memory(context.organization)
    written_memory = _written_topic_memory(context.organization)
    topic_candidates = _topic_candidates_from_runs(
        latest_runs,
        organization=context.organization,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        written_memory=written_memory,
    )
    hidden_topic_candidates = _topic_candidates_from_runs(
        latest_runs,
        organization=context.organization,
        include_written=True,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        written_memory=written_memory,
    )
    baseline_snapshot = _latest_baseline_snapshot(context.organization)
    checks = _profile_checks(context.organization, config, latest_runs, baseline_snapshot)
    guided_steps, current_guided_step = _guided_steps(checks)
    latest_runs_by_workflow = {}
    for run in latest_runs:
        latest_runs_by_workflow.setdefault(
            run.workflow,
            _serialize_run(run, context=context, latest_runs=latest_runs, checks=checks),
        )
    latest_article_run = _latest_run_matching(latest_runs, ARTICLE_WORKFLOWS)
    google_status = google_baseline_connection_status(context.profile.user)
    google_status["connectUrl"] = _google_baseline_connect_url(request)
    has_completed_article_flow = _has_completed_article_flow(context.organization, latest_runs)
    return {
        "company": {
            "id": str(context.company.id),
            "name": context.company.name,
            "domain": context.company.domain,
            "location": context.company.location,
            "abn": context.company.abn,
            "organizationId": context.organization.id,
            "companyLinkedInUrl": context.organization.company_linkedin_url,
        },
        "organization": {
            "id": context.organization.id,
            "name": context.organization.name,
            "domain": context.organization.domain,
            "companyLinkedInUrl": context.organization.company_linkedin_url,
            "competitors": context.organization.competitors,
            "seedKeywords": context.organization.seed_keywords,
        },
        "settings": {
            "brandName": config.brand_name,
            "companyContext": config.company_context,
            "articleDeliveryMode": _stored_article_delivery_mode(config) or "content_only",
            "articleDeliveryModeEffective": _effective_article_delivery_mode(config),
            "githubRepo": config.github_repo,
            "dailyDiscoveryEnabled": config.daily_discovery_enabled,
            "dailyDiscoveryPriority": config.daily_discovery_priority,
            "defaultTimezone": config.default_timezone,
            "githubConnectionState": config.github_connection_state,
        },
        "startupProfile": _serialize_startup_profile(context.organization),
        "websiteBaseline": _serialize_baseline_snapshot(baseline_snapshot, config),
        "googleBaselineConnection": google_status,
        "checks": checks,
        "latestRuns": [_serialize_run(run, context=context, latest_runs=latest_runs, checks=checks) for run in latest_runs],
        "latestRunsByWorkflow": latest_runs_by_workflow,
        "topicCandidates": topic_candidates,
        "hiddenTopicCandidates": hidden_topic_candidates,
        "declinedTopicFeedback": [serialize_topic_feedback(item) for item in declined_topic_feedback],
        "writtenTopics": _recent_written_topics(context.organization),
        "publishEvidence": _publish_evidence_from_run(latest_article_run),
        "guidedSteps": guided_steps,
        "currentGuidedStep": current_guided_step,
        "recommendedNextAction": _recommended_next_action(checks),
        "workflowProgress": _workflow_progress(
            context=context,
            latest_runs=latest_runs,
            checks=checks,
            topic_candidates=topic_candidates,
        ),
        "hasCompletedArticleFlow": has_completed_article_flow,
        "startPageMode": "topic_picker" if has_completed_article_flow else "first_article_setup",
    }


def _serialize_bootstrap_without_domain(company):
    checks = {
        "account": {"passed": True},
        "websiteProfile": {
            "passed": False,
            "checks": {"domain": False, "brandOrContext": False, "competitorsOrSeedKeywords": False},
        },
        "github": {"passed": False, "connectionState": "missing_domain", "repoSet": False},
        "baseline": {"passed": False, "runId": None, "collectedAt": None, "overallScore": None, "stale": False, "skipped": False},
        "scan": {"passed": False},
        "scaffold": {"passed": False},
        "research": {"passed": False},
        "write": {"passed": False},
        "contentPackage": {"passed": False},
        "publish": {"passed": False},
        "dailyAutomation": {"passed": False},
    }
    guided_steps, current_guided_step = _guided_steps(checks)
    return {
        "company": {
            "id": str(company.id),
            "name": company.name,
            "domain": company.domain,
            "location": company.location,
            "abn": company.abn,
            "organizationId": None,
            "companyLinkedInUrl": "",
        },
        "organization": {
            "id": None,
            "name": company.name,
            "domain": company.domain or "",
            "companyLinkedInUrl": "",
            "competitors": [],
            "seedKeywords": [],
        },
        "settings": {
            "brandName": company.name,
            "companyContext": "",
            "articleDeliveryMode": getattr(settings, "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE", "content_only"),
            "articleDeliveryModeEffective": "content_only",
            "githubRepo": "",
            "dailyDiscoveryEnabled": False,
            "dailyDiscoveryPriority": 0,
            "defaultTimezone": "",
            "githubConnectionState": "missing_domain",
        },
        "checks": checks,
        "startupProfile": {
            "founderNames": [],
            "stage": "",
            "organizationKind": "",
            "notes": "",
            "companyAliases": [company.name] if company.name else [],
            "domainAliases": [],
        },
        "websiteBaseline": {"status": "missing", "passed": False, "skipped": False},
        "googleBaselineConnection": {"connected": False, "hasBaselineScopes": False, "status": "needs_connection", "connectUrl": ""},
        "latestRuns": [],
        "latestRunsByWorkflow": {},
        "topicCandidates": [],
        "hiddenTopicCandidates": [],
        "declinedTopicFeedback": [],
        "writtenTopics": [],
        "publishEvidence": {},
        "guidedSteps": guided_steps,
        "currentGuidedStep": current_guided_step,
        "recommendedNextAction": {"key": "websiteProfile", "label": "Save website profile"},
        "workflowProgress": _workflow_progress(checks=checks),
        "hasCompletedArticleFlow": False,
        "startPageMode": "first_article_setup",
    }


def _recommended_next_action(checks):
    order = [
        ("websiteProfile", "Save website profile"),
        ("baseline", "Run website baseline"),
        ("github", "Connect GitHub"),
        ("scan", "Scan repository"),
        ("scaffold", "Verify publishing path"),
        ("research", "Run topic discovery"),
        ("write", "Generate article"),
        ("publish", "Review publish evidence"),
        ("dailyAutomation", "Enable daily generation"),
    ]
    for key, label in order:
        if not checks.get(key, {}).get("passed"):
            return {"key": key, "label": label}
    return {"key": "ready", "label": "Daily generation is ready"}


def _guided_steps(checks):
    steps = [
        ("startupDetails", "Startup details", "websiteProfile"),
        ("baseline", "Website baseline", "baseline"),
        ("github", "Connect GitHub", "github"),
        ("scan", "Scan repository", "scan"),
        ("articleSystem", "Prepare article system", "scaffold"),
        ("research", "Research topics", "research"),
        ("chooseArticle", "Choose article", "research"),
        ("writeCheck", "Write + check", "write"),
        ("editArticle", "Edit article", "write"),
        ("reviewPublish", "Review publish", "publish"),
        ("dailyAutomation", "Daily automation", "dailyAutomation"),
    ]
    first_incomplete = None
    payload = []
    for key, label, check_key in steps:
        passed = bool(checks.get(check_key, {}).get("passed"))
        status_label = "complete" if passed else "pending"
        if not passed and first_incomplete is None:
            first_incomplete = key
            status_label = "active"
        payload.append(
            {
                "key": key,
                "label": label,
                "status": status_label,
                "passed": passed,
                "href": f"/founder-tools/marketing/create?step={key}",
            }
        )
    return payload, first_incomplete or "dailyAutomation"


def _content_factory_headers():
    headers = {"Content-Type": "application/json"}
    api_key = getattr(settings, "CONTENT_FACTORY_API_KEY", None)
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


def _content_factory_remote_config():
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip().rstrip("/")
    api_key_configured = bool(getattr(settings, "CONTENT_FACTORY_API_KEY", None))
    is_local_env = bool(getattr(settings, "IS_LOCAL_ENV", False))
    enabled = bool(base_url and (api_key_configured or not is_local_env))
    return {
        "base_url": base_url,
        "api_key_configured": api_key_configured,
        "is_local_env": is_local_env,
        "enabled": enabled,
    }


def _remote_required_for_workflow(workflow):
    return str(workflow or "").strip() in REMOTE_REQUIRED_WORKFLOWS


def _content_factory_unavailable_message(config):
    if not config["base_url"]:
        return "CONTENT_FACTORY_URL is not configured."
    if config["is_local_env"] and not config["api_key_configured"]:
        return "CONTENT_FACTORY_API_KEY is not configured for this local environment."
    return "Content Factory remote calls are not enabled."


def _content_factory_diagnostics(config, **extra):
    diagnostics = {
        "content_factory_url_configured": bool(config["base_url"]),
        "content_factory_api_key_configured": bool(config["api_key_configured"]),
        "content_factory_remote_enabled": bool(config["enabled"]),
        "is_local_env": bool(config["is_local_env"]),
    }
    diagnostics.update({key: value for key, value in extra.items() if value is not None})
    return diagnostics


def _normalize_remote_run_status(value):
    normalized = str(value or "").strip().lower()
    mapping = {
        "processing": ContentFactoryRunStatus.RUNNING,
        "in_progress": ContentFactoryRunStatus.RUNNING,
        "blocked_verification": ContentFactoryRunStatus.BLOCKED,
        "precondition_failed": ContentFactoryRunStatus.BLOCKED,
        "error": ContentFactoryRunStatus.FAILED,
    }
    normalized = mapping.get(normalized, normalized)
    allowed = {choice[0] for choice in ContentFactoryRunStatus.choices}
    return normalized if normalized in allowed else ContentFactoryRunStatus.QUEUED


def _normalize_remote_step_status(value):
    normalized = str(value or "").strip().lower()
    mapping = {
        "processing": ContentFactoryStepStatus.RUNNING,
        "in_progress": ContentFactoryStepStatus.RUNNING,
        "blocked_verification": ContentFactoryStepStatus.BLOCKED,
        "error": ContentFactoryStepStatus.FAILED,
    }
    normalized = mapping.get(normalized, normalized)
    allowed = {choice[0] for choice in ContentFactoryStepStatus.choices}
    return normalized if normalized in allowed else ContentFactoryStepStatus.PENDING


def _is_retryable_sqlite_lock(exc):
    return connection.vendor == "sqlite" and "database is locked" in str(exc).lower()


def _friendly_content_factory_error(*, workflow, detail="", unavailable=False):
    if workflow == "startup_autofill" and unavailable:
        return "AI fill is unavailable. Check the Content Factory backend and try again."
    if detail:
        return str(detail)
    return "Content Factory worker is unavailable. Please try again."


def _blocked_worker_payload(*, workflow, detail="", technical_error="", status_code=None, response_payload=None, retryable=True, diagnostics=None):
    friendly = _friendly_content_factory_error(workflow=workflow, detail=detail, unavailable=True)
    payload_diagnostics = {
        "technical_error": str(technical_error or detail or ""),
        "retryable": retryable,
    }
    if isinstance(diagnostics, dict):
        payload_diagnostics.update(diagnostics)
    if status_code is not None:
        payload_diagnostics["content_factory_status_code"] = status_code
    if response_payload is not None:
        payload_diagnostics["content_factory_response"] = response_payload
    return {
        "status": ContentFactoryRunStatus.BLOCKED,
        "error": friendly,
        "errors": [friendly],
        "message": friendly,
        "diagnostics": payload_diagnostics,
        "retryable": retryable,
    }


def _parse_remote_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _run_result_from_remote(remote_data):
    if not isinstance(remote_data, dict):
        return {}
    result = remote_data.get("result")
    if isinstance(result, dict):
        merged = dict(result)
    else:
        merged = {}
    for key in (
        "warnings",
        "errors",
        "error",
        "message",
        "diagnostics",
        "retryable",
        "preview_url",
        "pr_url",
        "route_path",
        "livePreview",
        "live_preview",
        "componentManifest",
        "component_manifest",
    ):
        if remote_data.get(key) is not None and merged.get(key) is None:
            merged[key] = remote_data.get(key)
    if not merged and remote_data:
        merged = dict(remote_data)
    return merged


def _preview_payload_from_result(result):
    if not isinstance(result, dict):
        return {}
    payload = result.get("livePreview") or result.get("live_preview") or {}
    return payload if isinstance(payload, dict) else {}


def _empty_preview_payload(payload):
    if not isinstance(payload, dict) or not payload:
        return True
    status_value = str(payload.get("status") or "").strip().lower()
    return (
        status_value in {"", "not_started"}
        and not payload.get("available")
        and not (payload.get("previewUrl") or payload.get("preview_url"))
        and not payload.get("error")
    )


def _merge_preserved_live_preview(local_result, remote_result):
    if not isinstance(remote_result, dict):
        return remote_result
    local_preview = _preview_payload_from_result(local_result)
    if _empty_preview_payload(local_preview):
        return remote_result
    remote_preview = _preview_payload_from_result(remote_result)
    if not _empty_preview_payload(remote_preview):
        return remote_result
    merged = dict(remote_result)
    merged["livePreview"] = local_preview
    merged.pop("live_preview", None)
    return merged


def _create_local_run(*, workflow, domain, github_repo="", actor_id="", payload=None, remote_data=None):
    remote_data = remote_data or {}
    run_id = str(remote_data.get("run_id") or remote_data.get("job_id") or remote_data.get("task_id") or "")
    if not run_id:
        run_id = f"vibe-marketing-{workflow}-{uuid.uuid4().hex[:12]}"
    run, _created = ContentFactoryRun.objects.get_or_create(
        run_id=run_id,
        defaults={
            "workflow": workflow,
            "domain": domain,
            "github_repo": github_repo or "",
            "slack_user_id": actor_id,
            "status": _normalize_remote_run_status(remote_data.get("status")),
            "current_step": remote_data.get("current_step") or remote_data.get("step") or "queued",
            "run_request": payload or {},
            "result": _run_result_from_remote(remote_data),
            "error": str(remote_data.get("error") or ""),
        },
    )
    if not _created:
        run.workflow = run.workflow or workflow
        run.domain = run.domain or domain
        run.github_repo = run.github_repo or github_repo or ""
        run.slack_user_id = run.slack_user_id or actor_id
        run.run_request = run.run_request or payload or {}
        update_fields = ["workflow", "domain", "github_repo", "slack_user_id", "run_request", "updated_at"]
        if remote_data:
            run.status = _normalize_remote_run_status(remote_data.get("status") or run.status)
            run.current_step = remote_data.get("current_step") or remote_data.get("step") or run.current_step or "queued"
            remote_result = _run_result_from_remote(remote_data)
            if remote_result:
                run.result = remote_result
            run.error = str(remote_data.get("error") or "")
            update_fields.extend(["status", "current_step", "result", "error"])
        run.save(update_fields=list(dict.fromkeys(update_fields)))
    return run


def _call_content_factory_run_status(run_id, *, workflow=""):
    remote_config = _content_factory_remote_config()
    if not remote_config["enabled"]:
        if workflow == "startup_autofill" or _remote_required_for_workflow(workflow):
            technical_error = _content_factory_unavailable_message(remote_config)
            logger.warning(
                "content_factory_status_poll_blocked run_id=%s workflow=%s reason=%s url_configured=%s api_key_configured=%s is_local_env=%s",
                run_id,
                workflow,
                technical_error,
                bool(remote_config["base_url"]),
                bool(remote_config["api_key_configured"]),
                bool(remote_config["is_local_env"]),
            )
            return _blocked_worker_payload(
                workflow=workflow,
                technical_error=technical_error,
                diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
                retryable=True,
            )
        return {}

    try:
        response = http_client.get(
            f"{remote_config['base_url']}/api/runs/{run_id}",
            headers=_content_factory_headers(),
            timeout=(3, 15),
        )
    except http_client.RequestException as exc:
        if workflow == "startup_autofill" or _remote_required_for_workflow(workflow):
            logger.warning(
                "content_factory_status_poll_blocked run_id=%s workflow=%s reason=request_exception error=%s",
                run_id,
                workflow,
                exc,
            )
            return _blocked_worker_payload(
                workflow=workflow,
                technical_error=str(exc),
                diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
                retryable=True,
            )
        return {"error": str(exc), "errors": [str(exc)], "retryable": True}

    if response.status_code == 200:
        return response.json() if response.content else {}
    if response.status_code == 404:
        if workflow == "startup_autofill" or _remote_required_for_workflow(workflow):
            detail = f"Content Factory run {run_id} was not found."
            logger.warning(
                "content_factory_status_poll_blocked run_id=%s workflow=%s status_code=404",
                run_id,
                workflow,
            )
            return _blocked_worker_payload(
                workflow=workflow,
                detail=detail,
                technical_error=detail,
                status_code=response.status_code,
                diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
                retryable=False,
            )
        return {}

    try:
        response_payload = response.json()
    except Exception:
        response_payload = {}
    detail = response_payload.get("detail") or response_payload.get("error") or response.text
    if workflow == "startup_autofill" or _remote_required_for_workflow(workflow):
        logger.warning(
            "content_factory_status_poll_blocked run_id=%s workflow=%s status_code=%s",
            run_id,
            workflow,
            response.status_code,
        )
        return _blocked_worker_payload(
            workflow=workflow,
            detail=str(detail or f"Content Factory returned {response.status_code}."),
            technical_error=str(detail or f"Content Factory returned {response.status_code}."),
            status_code=response.status_code,
            response_payload=response_payload,
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
            retryable=response.status_code >= 500,
        )
    return {
        "error": str(detail or f"Content Factory returned {response.status_code}."),
        "errors": [str(detail or f"Content Factory returned {response.status_code}.")],
        "content_factory_status_code": response.status_code,
        "content_factory_response": response_payload,
        "retryable": response.status_code >= 500,
    }


def _sync_steps_from_remote(run, remote_data):
    raw_steps = remote_data.get("steps") or remote_data.get("step_states")
    if not raw_steps:
        return

    if isinstance(raw_steps, dict):
        items = list(raw_steps.items())
    elif isinstance(raw_steps, list):
        items = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                continue
            key = str(raw_step.get("key") or raw_step.get("step_key") or raw_step.get("name") or f"step_{index}").strip()
            if key:
                items.append((key, raw_step))
    else:
        return

    seen = []
    for index, (step_key, raw_step) in enumerate(items):
        if not isinstance(raw_step, dict):
            continue
        step_key = str(step_key or "").strip()
        if not step_key:
            continue
        seen.append(step_key)
        artifacts = raw_step.get("artifacts") or []
        if not isinstance(artifacts, list):
            artifacts = []
        ContentFactoryRunStep.objects.update_or_create(
            run=run,
            step_key=step_key,
            defaults={
                "display_order": index,
                "required": bool(raw_step.get("required", True)),
                "status": _normalize_remote_step_status(raw_step.get("status")),
                "attempts": int(raw_step.get("attempts") or len(raw_step.get("attempt_history") or []) or 0),
                "message": str(raw_step.get("message") or ""),
                "error": str(raw_step.get("error") or ""),
                "started_at": _parse_remote_datetime(raw_step.get("started_at") or raw_step.get("startedAt")),
                "completed_at": _parse_remote_datetime(raw_step.get("completed_at") or raw_step.get("completedAt")),
                "latest_attempt_path": str(raw_step.get("latest_attempt_path") or ""),
                "artifacts": artifacts,
            },
        )
    if seen and run.step_order != seen:
        run.step_order = seen


def _sync_local_run_from_remote(run, remote_data):
    if not isinstance(remote_data, dict) or not remote_data:
        return run

    result = _run_result_from_remote(remote_data)
    remote_status = _normalize_remote_run_status(remote_data.get("status") or result.get("status") or run.status)
    if run.status == ContentFactoryRunStatus.CANCELLED and remote_status != ContentFactoryRunStatus.CANCELLED:
        return run
    if run.status in FAILED_RUN_STATUSES and remote_status in RUNNING_RUN_STATUSES:
        return run

    run.status = remote_status
    run.current_step = str(remote_data.get("current_step") or remote_data.get("step") or run.current_step or "")
    run.artifact_root = str(remote_data.get("artifact_root") or run.artifact_root or "")
    if isinstance(remote_data.get("acceptance_summary"), dict):
        run.acceptance_summary = remote_data["acceptance_summary"]
    if isinstance(remote_data.get("verification"), dict):
        run.verification_summary = remote_data["verification"]
    if remote_data.get("approval_state"):
        run.approval_state = str(remote_data.get("approval_state"))
    if remote_data.get("resume_available") is not None:
        run.resume_available = bool(remote_data.get("resume_available"))
    if remote_data.get("workflow") and not run.workflow:
        run.workflow = str(remote_data.get("workflow"))
    if remote_data.get("error"):
        run.error = str(remote_data.get("error"))
    elif run.status not in {ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.BLOCKED}:
        run.error = ""
    if result:
        result = _merge_preserved_live_preview(run.result or {}, result)
        run.result = result
    _sync_steps_from_remote(run, remote_data)
    run.save(
        update_fields=[
            "workflow",
            "status",
            "current_step",
            "artifact_root",
            "step_order",
            "acceptance_summary",
            "verification_summary",
            "approval_state",
            "resume_available",
            "result",
            "error",
            "updated_at",
        ]
    )
    _persist_completed_article_memory_if_possible(run)
    return run


def _queue_content_factory_run(*, endpoint, workflow, context, config, payload):
    actor_id = founder_actor_id_for_user(context.profile.user)
    remote_config = _content_factory_remote_config()
    remote_data = {}
    requires_remote = workflow == "startup_autofill" or _remote_required_for_workflow(workflow)
    if requires_remote and not remote_config["enabled"]:
        technical_error = _content_factory_unavailable_message(remote_config)
        logger.warning(
            "content_factory_dispatch_blocked workflow=%s endpoint=%s reason=%s url_configured=%s api_key_configured=%s is_local_env=%s",
            workflow,
            endpoint,
            technical_error,
            bool(remote_config["base_url"]),
            bool(remote_config["api_key_configured"]),
            bool(remote_config["is_local_env"]),
        )
        remote_data = _blocked_worker_payload(
            workflow=workflow,
            technical_error=technical_error,
            diagnostics=_content_factory_diagnostics(remote_config, workflow=workflow, endpoint=endpoint),
            retryable=True,
        )
    elif remote_config["enabled"]:
        url = f"{remote_config['base_url']}/api/runs/{endpoint}"
        logger.info(
            "content_factory_dispatch_start workflow=%s endpoint=%s url_configured=%s api_key_configured=%s",
            workflow,
            endpoint,
            bool(remote_config["base_url"]),
            bool(remote_config["api_key_configured"]),
        )
        try:
            response = http_client.post(url, json=payload, headers=_content_factory_headers(), timeout=(3, 10))
            if response.status_code in (200, 202):
                remote_data = response.json() if response.content else {}
                run_id = str(remote_data.get("run_id") or remote_data.get("job_id") or remote_data.get("task_id") or "").strip()
                logger.info(
                    "content_factory_dispatch_result workflow=%s endpoint=%s status_code=%s run_id=%s",
                    workflow,
                    endpoint,
                    response.status_code,
                    run_id,
                )
                if requires_remote and not run_id:
                    remote_data = _blocked_worker_payload(
                        workflow=workflow,
                        detail="Content Factory did not return a run id.",
                        technical_error="Content Factory queue response did not include run_id, job_id, or task_id.",
                        status_code=response.status_code,
                        response_payload=remote_data,
                        diagnostics=_content_factory_diagnostics(remote_config, workflow=workflow, endpoint=endpoint),
                        retryable=True,
                    )
            else:
                try:
                    response_payload = response.json()
                except Exception:
                    response_payload = {}
                detail = (
                    response_payload.get("detail")
                    or response_payload.get("error")
                    or response_payload.get("message")
                    or response.text
                    or f"Content Factory returned {response.status_code}."
                )
                logger.warning(
                    "content_factory_dispatch_blocked workflow=%s endpoint=%s status_code=%s",
                    workflow,
                    endpoint,
                    response.status_code,
                )
                remote_data = _blocked_worker_payload(
                    workflow=workflow,
                    detail=str(detail),
                    technical_error=str(detail),
                    status_code=response.status_code,
                    response_payload=response_payload,
                    diagnostics=_content_factory_diagnostics(remote_config, workflow=workflow, endpoint=endpoint),
                    retryable=response.status_code >= 500,
                )
        except http_client.RequestException as exc:
            logger.warning(
                "content_factory_dispatch_blocked workflow=%s endpoint=%s reason=request_exception error=%s",
                workflow,
                endpoint,
                exc,
            )
            remote_data = _blocked_worker_payload(
                workflow=workflow,
                technical_error=str(exc),
                diagnostics=_content_factory_diagnostics(remote_config, workflow=workflow, endpoint=endpoint),
                retryable=True,
            )

    return _create_local_run(
        workflow=workflow,
        domain=context.organization.domain,
        github_repo=config.github_repo or payload.get("github_repo") or "",
        actor_id=actor_id,
        payload=payload,
        remote_data=remote_data,
    )


def _call_content_factory_run_action(*, run_id, action, payload, workflow="article_generation"):
    remote_config = _content_factory_remote_config()
    if not remote_config["enabled"]:
        technical_error = _content_factory_unavailable_message(remote_config)
        logger.warning(
            "content_factory_action_blocked run_id=%s workflow=%s action=%s reason=%s",
            run_id,
            workflow,
            action,
            technical_error,
        )
        return _blocked_worker_payload(
            workflow=workflow,
            technical_error=technical_error,
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow, action=action),
            retryable=True,
        )

    try:
        response = http_client.post(
            f"{remote_config['base_url']}/api/runs/{run_id}/{action}",
            json=payload or {},
            headers=_content_factory_headers(),
            timeout=(3, 15),
        )
    except http_client.RequestException as exc:
        return _blocked_worker_payload(
            workflow=workflow,
            technical_error=str(exc),
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow, action=action),
            retryable=True,
        )

    if response.status_code in (200, 202):
        return response.json() if response.content else {}

    try:
        response_payload = response.json()
    except Exception:
        response_payload = {}
    detail = response_payload.get("detail") or response_payload.get("error") or response.text
    return {
        "error": str(detail or f"Content Factory returned {response.status_code}."),
        "errors": [str(detail or f"Content Factory returned {response.status_code}.")],
        "content_factory_status_code": response.status_code,
        "content_factory_response": response_payload,
        "retryable": response.status_code >= 500,
    }


def _call_content_factory_component_revision(*, run_id, payload):
    remote_config = _content_factory_remote_config()
    if not remote_config["enabled"]:
        technical_error = _content_factory_unavailable_message(remote_config)
        logger.warning("content_factory_component_revision_blocked run_id=%s reason=%s", run_id, technical_error)
        return _blocked_worker_payload(
            workflow="article_revision",
            technical_error=technical_error,
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow="article_revision"),
            retryable=True,
        )

    try:
        response = http_client.post(
            f"{remote_config['base_url']}/api/runs/{run_id}/component-revisions",
            json=payload or {},
            headers=_content_factory_headers(),
            timeout=(5, 90),
        )
    except http_client.RequestException as exc:
        return _blocked_worker_payload(
            workflow="article_revision",
            technical_error=str(exc),
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow="article_revision"),
            retryable=True,
        )

    if response.status_code in (200, 202):
        return response.json() if response.content else {}

    try:
        response_payload = response.json()
    except Exception:
        response_payload = {}
    detail = response_payload.get("detail") or response_payload.get("error") or response.text
    return {
        "error": str(detail or f"Content Factory returned {response.status_code}."),
        "errors": [str(detail or f"Content Factory returned {response.status_code}.")],
        "content_factory_status_code": response.status_code,
        "content_factory_response": response_payload,
        "retryable": response.status_code >= 500,
    }


def _call_content_factory_live_preview(*, run_id, method="GET", payload=None):
    remote_config = _content_factory_remote_config()
    if not remote_config["enabled"]:
        technical_error = _content_factory_unavailable_message(remote_config)
        logger.warning("content_factory_live_preview_blocked run_id=%s method=%s reason=%s", run_id, method, technical_error)
        return {
            **_blocked_worker_payload(
                workflow="article_generation",
                technical_error=technical_error,
                diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow="article_generation", method=method),
                retryable=True,
            ),
            "available": False,
        }

    url = f"{remote_config['base_url']}/api/runs/{run_id}/live-preview"
    try:
        logger.info("content_factory_live_preview_request run_id=%s method=%s url=%s", run_id, method, url)
        if method == "POST":
            response = http_client.post(
                url,
                json=payload or {},
                headers=_content_factory_headers(),
                timeout=(3, _content_factory_live_preview_start_timeout_seconds()),
            )
        elif method == "DELETE":
            response = http_client.delete(url, headers=_content_factory_headers(), timeout=(3, 15))
        else:
            response = http_client.get(url, headers=_content_factory_headers(), timeout=(3, 15))
    except http_client.RequestException as exc:
        logger.warning(
            "content_factory_live_preview_transport_error run_id=%s method=%s error=%s",
            run_id,
            method,
            exc,
        )
        if method == "POST" and _is_live_preview_start_timeout(exc):
            return {
                "available": False,
                "status": "starting",
                "previewUrl": "",
                "error": "",
                "errors": [],
                "errorCode": "preview_start_timeout",
                "error_code": "preview_start_timeout",
                "retryable": True,
            }
        return {"available": False, "status": "failed", "error": str(exc), "errors": [str(exc)], "retryable": True}

    if response.status_code in (200, 202):
        response_payload = response.json() if response.content else {}
        logger.info(
            "content_factory_live_preview_response run_id=%s method=%s status_code=%s remote_status=%s error_code=%s available=%s",
            run_id,
            method,
            response.status_code,
            response_payload.get("status"),
            response_payload.get("errorCode") or response_payload.get("error_code"),
            response_payload.get("available"),
        )
        return response_payload

    try:
        response_payload = response.json()
    except Exception:
        response_payload = {}
    detail = response_payload.get("detail") or response_payload.get("error") or response.text
    logger.warning(
        "content_factory_live_preview_response run_id=%s method=%s status_code=%s remote_status=%s error_code=%s detail=%s",
        run_id,
        method,
        response.status_code,
        response_payload.get("status"),
        response_payload.get("errorCode") or response_payload.get("error_code"),
        str(detail or "")[:300],
    )
    return {
        "available": False,
        "status": "failed",
        "error": str(detail or f"Content Factory returned {response.status_code}."),
        "errors": [str(detail or f"Content Factory returned {response.status_code}.")],
        "content_factory_status_code": response.status_code,
        "content_factory_response": response_payload,
        "retryable": response.status_code >= 500,
    }


def _content_factory_live_preview_start_timeout_seconds():
    raw_value = getattr(settings, "CONTENT_FACTORY_LIVE_PREVIEW_START_READ_TIMEOUT_SECONDS", 20)
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 20


def _is_live_preview_start_timeout(exc) -> bool:
    timeout_classes = (
        getattr(http_client.exceptions, "Timeout", ()),
        getattr(http_client.exceptions, "ReadTimeout", ()),
    )
    if isinstance(exc, timeout_classes):
        return True
    message = str(exc or "").casefold()
    return "timed out" in message or "timeout" in message


def _lookup_query(request) -> str:
    return str(request.query_params.get("q") or request.query_params.get("query") or "").strip()


def _lookup_requires_founder(request):
    profile = get_or_create_founder_profile(request.user)
    if profile.role != profile.ROLE_FOUNDER:
        return Response({"detail": "Only founders can access Vibe Marketing."}, status=status.HTTP_403_FORBIDDEN)
    return None


def _google_place_prediction_to_suggestion(prediction):
    if not isinstance(prediction, dict):
        return None
    place_id = str(prediction.get("placeId") or prediction.get("place") or "").strip()
    text = prediction.get("text") if isinstance(prediction.get("text"), dict) else {}
    structured = prediction.get("structuredFormat") if isinstance(prediction.get("structuredFormat"), dict) else {}
    main_text = structured.get("mainText") if isinstance(structured.get("mainText"), dict) else {}
    secondary_text = structured.get("secondaryText") if isinstance(structured.get("secondaryText"), dict) else {}
    label = str(text.get("text") or "").strip()
    city = str(main_text.get("text") or "").strip()
    secondary = str(secondary_text.get("text") or "").strip()
    if not label:
        label = ", ".join(part for part in [city, secondary] if part)
    if not label:
        return None
    parts = [part.strip() for part in label.split(",") if part.strip()]
    if not city and parts:
        city = parts[0]
    return {
        "id": place_id or label,
        "label": label,
        "city": city,
        "region": parts[-2] if len(parts) > 2 else "",
        "country": parts[-1] if len(parts) > 1 else "",
        "placeId": place_id,
    }


class VibeMarketingLocationLookupView(APIView):
    def get(self, request):
        error_response = _lookup_requires_founder(request)
        if error_response:
            return error_response

        query = _lookup_query(request)
        api_key = getattr(settings, "GOOGLE_PLACES_API_KEY", "")
        if len(query) < 3:
            return Response({"configured": bool(api_key), "suggestions": []}, status=status.HTTP_200_OK)
        if not api_key:
            return Response({"configured": False, "suggestions": []}, status=status.HTTP_200_OK)

        payload = {
            "input": query,
            "includedPrimaryTypes": ["(cities)"],
            "languageCode": "en",
            "regionCode": "au",
        }
        session_token = str(request.query_params.get("sessionToken") or request.query_params.get("session_token") or "").strip()
        if session_token:
            payload["sessionToken"] = session_token

        try:
            response = http_client.post(
                "https://places.googleapis.com/v1/places:autocomplete",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": ",".join(
                        [
                            "suggestions.placePrediction.place",
                            "suggestions.placePrediction.placeId",
                            "suggestions.placePrediction.text.text",
                            "suggestions.placePrediction.structuredFormat.mainText.text",
                            "suggestions.placePrediction.structuredFormat.secondaryText.text",
                        ]
                    ),
                },
                timeout=(2, 5),
            )
            if getattr(response, "status_code", 200) >= 400:
                raise http_client.RequestException(f"Google Places returned {response.status_code}.")
            data = response.json()
        except Exception:
            return Response({"configured": True, "suggestions": [], "error": "Location lookup is unavailable."}, status=status.HTTP_200_OK)

        suggestions = []
        for item in data.get("suggestions", []) if isinstance(data, dict) else []:
            prediction = item.get("placePrediction") if isinstance(item, dict) else None
            suggestion = _google_place_prediction_to_suggestion(prediction)
            if suggestion:
                suggestions.append(suggestion)
        return Response({"configured": True, "suggestions": suggestions[:5]}, status=status.HTTP_200_OK)


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(element, *names: str) -> str:
    wanted = set(names)
    for node in element.iter():
        if _local_xml_name(node.tag) in wanted and node.text:
            return node.text.strip()
    return ""


def _format_abn(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11:
        return f"{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:]}"
    return value.strip()


def _abn_records_from_xml(xml_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    candidates = [
        node
        for node in root.iter()
        if _local_xml_name(node.tag)
        in {"searchResultsRecord", "businessEntity", "businessEntity200506", "businessEntity201205", "businessEntity201408", "businessEntity202001"}
    ]
    if not candidates:
        candidates = [root]

    results = []
    seen = set()
    for node in candidates:
        abn = _format_abn(_xml_text(node, "identifierValue", "ABN", "abn"))
        if not abn or abn in seen:
            continue
        seen.add(abn)
        results.append(
            {
                "abn": abn,
                "entityName": _xml_text(node, "organisationName", "entityName", "name"),
                "businessName": _xml_text(node, "businessName", "tradingName", "mainTradingName"),
                "status": _xml_text(node, "entityStatusCode", "status"),
                "state": _xml_text(node, "stateCode", "state"),
                "postcode": _xml_text(node, "postcode"),
            }
        )
    return results


class VibeMarketingAbnLookupView(APIView):
    def get(self, request):
        error_response = _lookup_requires_founder(request)
        if error_response:
            return error_response

        query = _lookup_query(request)
        auth_guid = getattr(settings, "ABR_LOOKUP_AUTHENTICATION_GUID", "")
        if len(query) < 3:
            return Response({"configured": bool(auth_guid), "suggestions": []}, status=status.HTTP_200_OK)
        if not auth_guid:
            return Response({"configured": False, "suggestions": []}, status=status.HTTP_200_OK)

        digits = re.sub(r"\D", "", query)
        is_numeric_lookup = bool(digits) and len(digits) >= 9 and re.fullmatch(r"[\d\s]+", query)
        if is_numeric_lookup:
            endpoint = "https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/SearchByABNv202001"
            params = {"searchString": digits, "includeHistoricalDetails": "N", "authenticationGuid": auth_guid}
        else:
            endpoint = "https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/ABRSearchByNameAdvancedSimpleProtocol2017"
            params = {
                "name": query,
                "postcode": "",
                "legalName": "Y",
                "tradingName": "Y",
                "businessName": "Y",
                "activeABNsOnly": "Y",
                "NSW": "",
                "SA": "",
                "ACT": "",
                "VIC": "",
                "WA": "",
                "NT": "",
                "QLD": "",
                "TAS": "",
                "authenticationGuid": auth_guid,
                "searchWidth": "Typical",
                "minimumScore": "50",
                "maxSearchResults": "5",
            }

        try:
            response = http_client.get(endpoint, params=params, timeout=(2, 6))
            if getattr(response, "status_code", 200) >= 400:
                raise http_client.RequestException(f"ABN Lookup returned {response.status_code}.")
        except Exception:
            return Response({"configured": True, "suggestions": [], "error": "ABN lookup is unavailable."}, status=status.HTTP_200_OK)

        return Response({"configured": True, "suggestions": _abn_records_from_xml(response.text)[:5]}, status=status.HTTP_200_OK)


class VibeMarketingBootstrapView(APIView):
    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        company = resolve_active_company(profile)
        if company is None:
            return Response(
                {"detail": "Create or select a founder company first.", "redirect": "/founder-tools/company-setup"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not normalize_company_domain(company.domain):
            return Response(_serialize_bootstrap_without_domain(company), status=status.HTTP_200_OK)
        context, error_response = _resolve_context_or_response(request, require_domain=True)
        if error_response:
            return error_response
        return Response(_serialize_bootstrap(context, request=request), status=status.HTTP_200_OK)


class VibeMarketingTopicFeedbackView(APIView):
    def get(self, request):
        context, error_response = _resolve_context_or_response(request, require_domain=True)
        if error_response:
            return error_response

        feedback_type = str(request.query_params.get("feedback_type") or "declined").strip() or "declined"
        include_restored = str(request.query_params.get("include_restored") or "").strip().lower() in {"1", "true", "yes"}
        try:
            limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        feedback = list_topic_feedback(
            context.organization,
            feedback_type=feedback_type,
            include_restored=include_restored,
            limit=limit,
            offset=offset,
        )
        return Response(
            {
                "domain": context.organization.domain,
                "count": len(feedback),
                "feedback": [serialize_topic_feedback(item) for item in feedback],
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        context, error_response = _resolve_context_or_response(request, require_domain=True)
        if error_response:
            return error_response

        keyword = str(request.data.get("keyword") or "").strip()
        if not keyword:
            return Response({"detail": "keyword is required"}, status=status.HTTP_400_BAD_REQUEST)

        feedback, created = record_topic_feedback(
            context.organization,
            keyword=keyword,
            feedback_type=str(request.data.get("feedback_type") or request.data.get("feedbackType") or "declined"),
            reason_code=str(request.data.get("reason_code") or request.data.get("reasonCode") or "not_appropriate"),
            reason_text=request.data.get("reason_text") or request.data.get("reasonText") or None,
            decline_scope=str(request.data.get("decline_scope") or request.data.get("declineScope") or "similar"),
            source=str(request.data.get("source") or "homepage_topic_card"),
            session_id=request.data.get("session_id") or request.data.get("sessionId") or None,
        )
        return Response(
            {**serialize_topic_feedback(feedback), "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class VibeMarketingTopicFeedbackRestoreView(APIView):
    def post(self, request, feedback_id):
        context, error_response = _resolve_context_or_response(request, require_domain=True)
        if error_response:
            return error_response

        try:
            feedback = TopicFeedback.objects.select_related("organization").get(
                pk=feedback_id,
                organization=context.organization,
            )
        except TopicFeedback.DoesNotExist:
            return Response({"detail": "Topic feedback not found."}, status=status.HTTP_404_NOT_FOUND)

        restored = restore_topic_feedback(feedback)
        return Response({**serialize_topic_feedback(restored), "restored": True}, status=status.HTTP_200_OK)


class VibeMarketingSettingsView(APIView):
    @transaction.atomic
    def put(self, request):
        profile = get_or_create_founder_profile(request.user)
        company = resolve_active_company(profile)
        if company is None:
            return Response(
                {"detail": "Create or select a founder company first.", "redirect": "/founder-tools/company-setup"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        domain = normalize_company_domain(request.data.get("domain") or company.domain)
        company_name = str(request.data.get("company_name") or request.data.get("companyName") or company.name).strip()
        if company_name and company.name != company_name:
            company.name = company_name
        if "location" in request.data:
            company.location = str(request.data.get("location") or "").strip()
        if "abn" in request.data:
            company.abn = str(request.data.get("abn") or "").strip() or None
        if domain:
            company.domain = domain
            company.save()
            organization = ensure_company_organization(company)
        else:
            return Response({"detail": "Domain is required before saving Vibe Marketing settings."}, status=status.HTTP_400_BAD_REQUEST)

        if request.data.get("brand_name") or request.data.get("brandName"):
            organization.name = str(request.data.get("brand_name") or request.data.get("brandName")).strip()
            organization.save(update_fields=["name"])

        if "company_linkedin_url" in request.data or "companyLinkedInUrl" in request.data:
            try:
                organization.company_linkedin_url = normalize_company_linkedin_url(
                    request.data.get("company_linkedin_url", request.data.get("companyLinkedInUrl"))
                )
            except ValueError as exc:
                return Response({"detail": str(exc), "field": "companyLinkedInUrl"}, status=status.HTTP_400_BAD_REQUEST)

        if "competitors" in request.data:
            organization.competitors = _camel_list(request.data.get("competitors"))
        if "seed_keywords" in request.data or "seedKeywords" in request.data:
            organization.seed_keywords = _camel_list(request.data.get("seed_keywords", request.data.get("seedKeywords")))
        organization.save(update_fields=["competitors", "seed_keywords", "company_linkedin_url"])

        config = _get_config(organization)
        _assign_config_actor(config, request.user)
        config.brand_name = request.data.get("brand_name", request.data.get("brandName", config.brand_name))
        config.company_context = request.data.get("company_context", request.data.get("companyContext", config.company_context))
        config.github_repo = request.data.get("github_repo", request.data.get("githubRepo", config.github_repo))
        config.article_delivery_mode = request.data.get(
            "article_delivery_mode",
            request.data.get("articleDeliveryMode", config.article_delivery_mode),
        )
        daily_enabled_submitted = "daily_discovery_enabled" in request.data or "dailyDiscoveryEnabled" in request.data
        if daily_enabled_submitted:
            config.daily_discovery_enabled = _bool_from_request(
                request.data.get("daily_discovery_enabled", request.data.get("dailyDiscoveryEnabled"))
            )
        if request.data.get("default_timezone") or request.data.get("defaultTimezone"):
            config.default_timezone = request.data.get("default_timezone") or request.data.get("defaultTimezone")
        if daily_enabled_submitted and config.daily_discovery_enabled:
            checks = _profile_checks(
                organization,
                config,
                _latest_runs_for_org(organization),
                _latest_baseline_snapshot(organization),
            )
            if not checks["dailyAutomation"]["passed"]:
                return Response(
                    {"detail": "Daily generation prerequisites are not complete.", "checks": checks},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        config.save()
        apply_shared_startup_details(user=request.user, company=company, data=request.data)

        refreshed_context = get_founder_company_context(request.user, company_id=company.id)
        return Response(_serialize_bootstrap(refreshed_context, request=request), status=status.HTTP_200_OK)


class VibeMarketingAutofillView(APIView):
    @transaction.atomic
    def post(self, request):
        profile = get_or_create_founder_profile(request.user)
        if profile.role != profile.ROLE_FOUNDER:
            return Response({"detail": "Only founders can access Vibe Marketing."}, status=status.HTTP_403_FORBIDDEN)

        company_name = str(request.data.get("company_name") or request.data.get("companyName") or "").strip()
        domain = normalize_company_domain(request.data.get("domain"))
        if not company_name:
            return Response({"detail": "Company name is required for autofill."}, status=status.HTTP_400_BAD_REQUEST)
        if not domain:
            return Response({"detail": "Website domain is required for autofill."}, status=status.HTTP_400_BAD_REQUEST)

        company_id = _company_id_from_request(request)
        if company_id:
            try:
                company = profile.companies.get(pk=company_id)
            except VibeRaisingCompany.DoesNotExist:
                return Response({"detail": "Company not found."}, status=status.HTTP_404_NOT_FOUND)
        else:
            company = resolve_active_company(profile)

        if company is None:
            company = VibeRaisingCompany.objects.create(
                profile=profile,
                name=company_name,
                domain=domain,
                location=str(request.data.get("location") or "").strip(),
                abn=str(request.data.get("abn") or "").strip() or None,
            )
            profile.active_company = company
            profile.save(update_fields=["active_company", "updated_at"])
        else:
            company.name = company_name
            company.domain = domain
            if "location" in request.data:
                company.location = str(request.data.get("location") or "").strip()
            if "abn" in request.data:
                company.abn = str(request.data.get("abn") or "").strip() or None
            company.save(update_fields=["name", "domain", "location", "abn", "updated_at"])

        organization = ensure_company_organization(company)
        if organization is None:
            return Response({"detail": "Website domain is required for autofill."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            apply_shared_startup_details(user=request.user, company=company, data=request.data)
        except ValueError as exc:
            return Response({"detail": str(exc), "field": "companyLinkedInUrl"}, status=status.HTTP_400_BAD_REQUEST)

        context = get_founder_company_context(request.user, company_id=company.id)
        company = context.company
        organization = context.organization
        config = _get_config(organization)
        actor_id = founder_actor_id_for_user(request.user)
        existing_fields = {
            "brandName": config.brand_name or organization.name,
            "companyContext": config.company_context or "",
            "competitors": _camel_list(organization.competitors),
            "seedKeywords": _camel_list(organization.seed_keywords),
            "companyLinkedInUrl": organization.company_linkedin_url,
        }
        payload = {
            "domain": organization.domain,
            "company_name": company.name,
            "brand_name": config.brand_name or organization.name,
            "company_linkedin_url": organization.company_linkedin_url,
            "location": company.location,
            "abn": company.abn,
            "existing_fields": existing_fields,
            "research_depth": "deep",
            "strict_deep_research": True,
            "min_direct_competitors": 3,
            "min_seed_keywords": 20,
            "min_public_sources": 3,
            "persist": False,
            "slack_user_id": actor_id,
            "requested_by_slack_user_id": actor_id,
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        run = _queue_content_factory_run(
            endpoint="autofill",
            workflow="startup_autofill",
            context=context,
            config=config,
            payload=payload,
        )
        result = run.result or {}
        errors = result.get("errors") if isinstance(result.get("errors"), list) else ([run.error] if run.error else [])
        return Response(
            {
                "run_id": run.run_id,
                "runId": run.run_id,
                "status": run.status,
                "error": run.error or result.get("error") or "",
                "errors": errors,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VibeMarketingBaselineView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        config.baseline_skipped_at = None
        config.baseline_skip_reason = ""
        config.save(update_fields=["baseline_skipped_at", "baseline_skip_reason", "updated_at"])
        payload = {
            "domain": context.organization.domain,
            "company_name": context.company.name,
            "brand_name": config.brand_name or context.organization.name,
            "seed_keywords": list(context.organization.seed_keywords or []),
            "competitors": list(context.organization.competitors or []),
            "slack_user_id": founder_actor_id_for_user(request.user),
            "requested_by_slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        run = _queue_content_factory_run(
            endpoint="baseline",
            workflow="website_baseline",
            context=context,
            config=config,
            payload=payload,
        )
        if run.status == ContentFactoryRunStatus.COMPLETED:
            _persist_baseline_snapshot_from_payload(organization=context.organization, run=run)
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingBaselineSkipView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        config.baseline_skipped_at = timezone.now()
        config.baseline_skip_reason = str(request.data.get("reason") or request.data.get("skipReason") or "Skipped during onboarding").strip()
        config.save(update_fields=["baseline_skipped_at", "baseline_skip_reason", "updated_at"])
        return Response(_serialize_bootstrap(context, request=request), status=status.HTTP_200_OK)


class VibeMarketingBaselineGoogleRefreshView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        snapshot = _latest_baseline_snapshot(context.organization)
        if not snapshot:
            return Response({"detail": "Run a website baseline before enriching it with Google data."}, status=status.HTTP_400_BAD_REQUEST)
        google_metrics = collect_verified_google_metrics(
            user=request.user,
            domain=context.organization.domain,
            ga4_property_id=request.data.get("ga4_property_id") or request.data.get("ga4PropertyId"),
        )
        snapshot = _merge_google_metrics_into_baseline(snapshot, google_metrics)
        return Response({"websiteBaseline": _serialize_baseline_snapshot(snapshot, _get_config(context.organization))}, status=status.HTTP_200_OK)


class VibeMarketingGitHubConnectView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        actor_id = founder_actor_id_for_user(request.user)
        config_update_fields = _assign_config_actor(config, request.user)
        requested_repo = _clean_github_repo(request.data.get("github_repo") or request.data.get("githubRepo"))
        if requested_repo:
            config.github_repo = requested_repo
            config_update_fields.append("github_repo")
        config_update_fields.append("updated_at")
        config.save(update_fields=list(dict.fromkeys(config_update_fields)))

        existing_connection = _connect_with_existing_github_credentials(
            config,
            domain=context.organization.domain,
            actor_id=actor_id,
            requested_repo=requested_repo,
        )
        if existing_connection:
            return Response(existing_connection, status=status.HTTP_200_OK)

        return Response(
            {
                "status": "auth_required",
                "connection_state": "auth_required",
                "github_repo": config.github_repo,
                "auth_url": build_github_auth_url(actor_id, domain=context.organization.domain, request=request),
            },
            status=status.HTTP_200_OK,
        )


class VibeMarketingScanView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        if request.data.get("github_repo") or request.data.get("githubRepo"):
            config.github_repo = request.data.get("github_repo") or request.data.get("githubRepo")
            config.save(update_fields=["github_repo", "updated_at"])
        payload = {
            "domain": context.organization.domain,
            "github_repo": config.github_repo,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "scaffold_if_missing": True,
            "generate_components": True,
        }
        run = _queue_content_factory_run(
            endpoint="scan",
            workflow="repo_scan",
            context=context,
            config=config,
            payload=payload,
        )
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingDiscoveryView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        payload = {
            "domain": context.organization.domain,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        run = _queue_content_factory_run(
            endpoint="discovery",
            workflow="auto_discovery",
            context=context,
            config=config,
            payload=payload,
        )
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingArticleView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        selected_title = str(
            _request_value(request.data, "selected_title", "selectedTitle", "candidate_title", "candidateTitle", default="")
            or ""
        ).strip()
        custom_title = str(
            _request_value(request.data, "custom_title", "customTitle", "title", "titleAngle", default="") or ""
        ).strip()
        topic_value = str(_request_value(request.data, "topic", default="") or "").strip()
        target_keyword = str(
            _request_value(
                request.data,
                "target_keyword",
                "targetKeyword",
                "custom_keyword",
                "customKeyword",
                "keyword",
                "candidate_keyword",
                "candidateKeyword",
                default="",
            )
            or ""
        ).strip()
        topic = selected_title or custom_title or topic_value or target_keyword
        if not (selected_title or custom_title or topic_value or target_keyword):
            return Response(
                {
                    "detail": "Choose a discovered topic or enter a custom title or keyword before generating an article.",
                    "field": "topicCandidateId",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        baseline_snapshot = _latest_baseline_snapshot(context.organization)
        if not _baseline_requirement_satisfied(config, baseline_snapshot):
            return Response(
                {
                    "detail": "Run the website baseline or skip it before generating an article.",
                    "check": _serialize_baseline_snapshot(baseline_snapshot, config),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        coverage_match = match_covered_topic(
            organization=context.organization,
            keyword=target_keyword or topic,
            title=selected_title or custom_title or topic,
        )
        if coverage_match:
            written_article = coverage_match.article
            return Response(
                {
                    "detail": (
                        "This topic has already been written. Choose a pending topic or enter a new custom article."
                        if written_article
                        else "This topic is not available because it matches a previously skipped or covered topic."
                    ),
                    "field": "topicCandidateId",
                    "writtenArticle": _serialize_written_article(written_article) if written_article else None,
                    "coveredTopic": _serialize_topic_coverage_match(coverage_match),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        source_run_id = _request_value(
            request.data,
            "source_run_id",
            "sourceRunId",
            "source_discovery_run_id",
            "sourceDiscoveryRunId",
            default=None,
        )
        requested_delivery_mode = _request_value(request.data, "delivery_mode", "deliveryMode", default=None)
        delivery_mode_explicit = _bool_from_request(
            _request_value(
                request.data,
                "delivery_mode_explicit",
                "deliveryModeExplicit",
                default=False,
            )
        )
        delivery_mode = _effective_article_delivery_mode(
            config,
            requested_mode=requested_delivery_mode,
            explicit=delivery_mode_explicit,
        )
        payload = {
            "domain": context.organization.domain,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "topic": topic,
            "target_keyword": target_keyword or topic,
            "context": str(request.data.get("context") or ""),
            "github_repo": config.github_repo,
            "delivery_mode": delivery_mode,
            "delivery_mode_confirmed": _bool_from_request(
                request.data.get("delivery_mode_confirmed", request.data.get("deliveryModeConfirmed", True))
            ),
            "delivery_mode_explicit": delivery_mode_explicit,
            "source_run_id": source_run_id,
            "custom_title": selected_title or custom_title or None,
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        run = _queue_content_factory_run(
            endpoint="article",
            workflow="article_generation",
            context=context,
            config=config,
            payload=payload,
        )
        if run.status not in FAILED_RUN_STATUSES:
            _mark_keyword_in_progress(context.organization, payload["target_keyword"])
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingRunView(APIView):
    def get(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        remote_data = _call_content_factory_run_status(run.run_id, workflow=run.workflow)
        if remote_data:
            max_attempts = 3 if connection.vendor == "sqlite" else 1
            for attempt in range(max_attempts):
                try:
                    run = _sync_local_run_from_remote(run, remote_data)
                    break
                except OperationalError as exc:
                    if not _is_retryable_sqlite_lock(exc) or attempt == max_attempts - 1:
                        raise
                    time.sleep(0.15 * (attempt + 1))
            if run.workflow in BASELINE_WORKFLOWS and run.status == ContentFactoryRunStatus.COMPLETED:
                _persist_baseline_snapshot_from_payload(organization=context.organization, run=run)
            if run.workflow in ARTICLE_WORKFLOWS and run.status == ContentFactoryRunStatus.COMPLETED:
                _persist_article_memory_from_run(organization=context.organization, run=run)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
        if run.workflow in ARTICLE_WORKFLOWS:
            run = _ensure_article_live_preview(run)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)


class VibeMarketingRunArtifactsView(APIView):
    def get(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "runId": run.run_id,
                "artifactRoot": run.artifact_root,
                "artifacts": (run.result or {}).get("artifacts") or [],
                "steps": [
                    {"key": step.step_key, "artifacts": step.artifacts or []}
                    for step in run.steps.order_by("display_order", "id")
                ],
            },
            status=status.HTTP_200_OK,
        )


class VibeMarketingRunCommentsMixin:
    def _resolve_run(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return None, None, error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return None, None, Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        return context, run, None


class VibeMarketingRunCommentsView(VibeMarketingRunCommentsMixin, APIView):
    def get(self, request, run_id):
        _context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        return Response(_component_feedback_from_run(run), status=status.HTTP_200_OK)

    def post(self, request, run_id):
        _context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        payload = _comment_payload_from_request(request.data or {})
        if not payload["component_id"]:
            return Response({"detail": "Choose an article component before adding a comment."}, status=status.HTTP_400_BAD_REQUEST)
        if not payload["body"]:
            return Response({"detail": "Comment text is required."}, status=status.HTTP_400_BAD_REQUEST)
        comment = VibeMarketingComponentComment.objects.create(
            run=run,
            actor=request.user if request.user and request.user.is_authenticated else None,
            **payload,
        )
        return Response(_serialize_component_comment(comment), status=status.HTTP_201_CREATED)


class VibeMarketingRunCommentDetailView(VibeMarketingRunCommentsMixin, APIView):
    def patch(self, request, run_id, comment_id):
        _context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        comment = get_object_or_404(VibeMarketingComponentComment, id=comment_id, run=run)
        if comment.status != VibeMarketingComponentCommentStatus.DRAFT:
            return Response({"detail": "Only draft comments can be updated."}, status=status.HTTP_400_BAD_REQUEST)
        payload = _comment_payload_from_request(request.data or {})
        if not _request_includes_comment_anchor(request.data or {}):
            payload["anchor"] = comment.anchor or {}
        if not payload["body"]:
            return Response({"detail": "Comment text is required."}, status=status.HTTP_400_BAD_REQUEST)
        for key, value in payload.items():
            setattr(comment, key, value)
        comment.save(update_fields=[
            "component_id",
            "component_type",
            "component_label",
            "source_section_id",
            "selector",
            "anchor",
            "body",
            "updated_at",
        ])
        return Response(_serialize_component_comment(comment), status=status.HTTP_200_OK)

    def delete(self, request, run_id, comment_id):
        _context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        comment = get_object_or_404(VibeMarketingComponentComment, id=comment_id, run=run)
        if comment.status != VibeMarketingComponentCommentStatus.DRAFT:
            return Response({"detail": "Only draft comments can be deleted."}, status=status.HTTP_400_BAD_REQUEST)
        comment.delete()
        return Response(_component_feedback_from_run(run), status=status.HTTP_200_OK)


class VibeMarketingRunCommentsSubmitView(VibeMarketingRunCommentsMixin, APIView):
    def post(self, request, run_id):
        context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        source_run = run
        run_request = run.run_request if isinstance(run.run_request, dict) else {}
        run_result = run.result if isinstance(run.result, dict) else {}
        draft_comments = list(
            VibeMarketingComponentComment.objects.filter(
                run=source_run,
                status=VibeMarketingComponentCommentStatus.DRAFT,
            )
            .order_by("created_at", "id")
        )
        draft_comments = [comment for comment in draft_comments if str(comment.body or "").strip()]
        if run.workflow == "article_revision" and not draft_comments:
            source_run_id = str(
                run_request.get("source_run_id")
                or run_request.get("sourceRunId")
                or run_result.get("source_run_id")
                or run_result.get("sourceRunId")
                or ""
            ).strip()
            if source_run_id and run.status == ContentFactoryRunStatus.FAILED:
                source_run = ContentFactoryRun.objects.filter(run_id=source_run_id).first() or run
                draft_comments = list(
                    VibeMarketingComponentComment.objects.filter(
                        run=source_run,
                        status=VibeMarketingComponentCommentStatus.DRAFT,
                    )
                    .order_by("created_at", "id")
                )
                draft_comments = [comment for comment in draft_comments if str(comment.body or "").strip()]
        retry_existing_batch = False
        if draft_comments:
            batch_id = str(uuid.uuid4())
            with transaction.atomic():
                VibeMarketingComponentComment.objects.filter(
                    id__in=[comment.id for comment in draft_comments],
                    status=VibeMarketingComponentCommentStatus.DRAFT,
                ).update(status=VibeMarketingComponentCommentStatus.SUBMITTED, batch_id=batch_id, updated_at=timezone.now())
                draft_comments = list(VibeMarketingComponentComment.objects.filter(id__in=[comment.id for comment in draft_comments]).order_by("created_at", "id"))
                _create_editorial_feedback_candidates(
                    organization=context.organization,
                    run=source_run,
                    comments=draft_comments,
                    batch_id=batch_id,
                )
        else:
            latest_submitted = (
                VibeMarketingComponentComment.objects.filter(
                    run=source_run,
                    status=VibeMarketingComponentCommentStatus.SUBMITTED,
                )
                .exclude(batch_id="")
                .order_by("-updated_at", "-created_at", "-id")
                .first()
            )
            if not latest_submitted:
                return Response({"detail": "Add at least one draft component comment before requesting a revision."}, status=status.HTTP_400_BAD_REQUEST)
            batch_id = latest_submitted.batch_id
            draft_comments = list(
                VibeMarketingComponentComment.objects.filter(
                    run=source_run,
                    status=VibeMarketingComponentCommentStatus.SUBMITTED,
                    batch_id=batch_id,
                ).order_by("created_at", "id")
            )
            retry_existing_batch = True

        remote_payload = {
            "source_run_id": source_run.run_id,
            "feedback_batch_id": batch_id,
            "requested_run_id": _component_revision_requested_run_id(source_run.run_id, batch_id),
            "comments": [_remote_comment_payload(comment) for comment in draft_comments],
            "request_source": "founder_tools_component_feedback",
        }
        remote_data = _call_content_factory_component_revision(run_id=source_run.run_id, payload=remote_payload)
        new_run_id = str(remote_data.get("run_id") or remote_data.get("runId") or "").strip()
        if remote_data.get("error") and not new_run_id:
            result = source_run.result or {}
            retryable = bool(remote_data.get("retryable"))
            result["component_feedback_latest_batch"] = {
                "id": batch_id,
                "sourceRunId": source_run.run_id,
                "status": "submitted" if retryable else "failed",
                "error": remote_data.get("error"),
                "retryable": retryable,
            }
            source_run.result = result
            source_run.save(update_fields=["result", "updated_at"])
            if retryable:
                return Response(_serialize_run(source_run, context=context), status=status.HTTP_202_ACCEPTED)
            return Response(
                {
                    "detail": remote_data.get("error") or "Content Factory could not queue the revision.",
                    "componentFeedback": _component_feedback_from_run(source_run),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        config = _get_config(context.organization)
        revision_run = _create_local_run(
            workflow="article_revision",
            domain=context.organization.domain,
            github_repo=config.github_repo or run.github_repo or "",
            actor_id=founder_actor_id_for_user(request.user),
            payload=remote_payload,
            remote_data=remote_data,
        )
        revision_result = revision_run.result or {}
        revision_result.update(
            {
                "source_run_id": source_run.run_id,
                "feedback_batch_id": batch_id,
                "submitted_component_comments": [_serialize_component_comment(comment) for comment in draft_comments],
            }
        )
        revision_run.result = revision_result
        revision_run.save(update_fields=["result", "updated_at"])

        result = source_run.result or {}
        result["component_feedback_latest_batch"] = {
            "id": batch_id,
            "sourceRunId": source_run.run_id,
            "revisionRunId": revision_run.run_id,
            "status": "running",
            "retry": retry_existing_batch,
        }
        result["component_feedback_revision_run_id"] = revision_run.run_id
        source_run.result = result
        source_run.save(update_fields=["result", "updated_at"])
        return Response(_serialize_run(revision_run, context=context), status=status.HTTP_202_ACCEPTED)


class VibeMarketingRunCommentsAcceptRevisionView(VibeMarketingRunCommentsMixin, APIView):
    def post(self, request, run_id):
        context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        run_request = run.run_request if isinstance(run.run_request, dict) else {}
        result = run.result or {}
        source_run_id = str(
            request.data.get("sourceRunId")
            or request.data.get("source_run_id")
            or run_request.get("source_run_id")
            or result.get("source_run_id")
            or ""
        ).strip()
        source_run = ContentFactoryRun.objects.filter(run_id=source_run_id).first() if source_run_id else run
        if not source_run or not _run_belongs_to_context(source_run, context):
            return Response({"detail": "Source run not found."}, status=status.HTTP_404_NOT_FOUND)
        batch_id = str(
            request.data.get("batchId")
            or request.data.get("batch_id")
            or run_request.get("feedback_batch_id")
            or result.get("feedback_batch_id")
            or ((source_run.result or {}).get("component_feedback_latest_batch") or {}).get("id")
            or ""
        ).strip()
        if not batch_id:
            return Response({"detail": "Feedback batch id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if run.status != ContentFactoryRunStatus.COMPLETED:
            return Response({"detail": "The revised article must be completed before accepting feedback."}, status=status.HTTP_400_BAD_REQUEST)

        promoted_count = _promote_editorial_feedback_batch(run=source_run, batch_id=batch_id, revision_run_id=run.run_id)
        VibeMarketingComponentComment.objects.filter(run=source_run, batch_id=batch_id).update(
            status=VibeMarketingComponentCommentStatus.APPLIED,
            updated_at=timezone.now(),
        )
        _persist_article_memory_from_run(organization=context.organization, run=run)
        for target in [source_run, run]:
            target_result = target.result or {}
            latest_batch = target_result.get("component_feedback_latest_batch")
            if not isinstance(latest_batch, dict) or latest_batch.get("id") == batch_id:
                target_result["component_feedback_latest_batch"] = {
                    "id": batch_id,
                    "sourceRunId": source_run.run_id,
                    "revisionRunId": run.run_id,
                    "status": "accepted",
                    "promotedLearningCount": promoted_count,
                }
            target.result = target_result
            target.save(update_fields=["result", "updated_at"])
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)


class VibeMarketingRunLivePreviewView(APIView):
    def _resolve_run(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return None, None, error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return None, None, Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        return context, run, None

    def _persist_preview(self, run, payload):
        return _persist_live_preview_payload(run, payload)

    def get(self, request, run_id):
        context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        payload = _call_content_factory_live_preview(run_id=run_id, method="GET")
        if payload:
            run = self._persist_preview(run, payload)
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)

    def post(self, request, run_id):
        context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        payload = {
            "force": _bool_from_request(request.data.get("force")),
            "local_repo_path": request.data.get("local_repo_path") or request.data.get("localRepoPath") or "",
        }
        payload.update(_live_preview_github_token_payload(run))
        remote_data = _call_content_factory_live_preview(run_id=run_id, method="POST", payload=payload)
        run = self._persist_preview(run, remote_data)
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)

    def delete(self, request, run_id):
        context, run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        remote_data = _call_content_factory_live_preview(run_id=run_id, method="DELETE")
        run = self._persist_preview(run, remote_data)
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)


@method_decorator(xframe_options_exempt, name="dispatch")
class VibeMarketingRunLivePreviewProxyView(APIView):
    http_method_names = ["get", "head", "options"]
    renderer_classes = [_AnyContentRenderer]

    def _resolve_run(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return None, None, error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return None, None, Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        return context, run, None

    def _proxy(self, request, run_id, proxy_path):
        _context, _run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        remote_config = _content_factory_remote_config()
        if not remote_config["enabled"]:
            return Response({"detail": _content_factory_unavailable_message(remote_config)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        safe_path = quote(str(proxy_path or "").lstrip("/"), safe="/:@!$&'()*+,;=-._~")
        remote_url = f"{remote_config['base_url']}/api/runs/{run_id}/live-preview/proxy/{safe_path}"
        query_string = request.META.get("QUERY_STRING") or ""
        if query_string:
            remote_url = f"{remote_url}?{query_string}"
        forwarded_headers = {
            "Accept": request.headers.get("Accept", "*/*"),
            "User-Agent": request.headers.get("User-Agent", "mlai-backend-live-preview-proxy"),
        }
        try:
            response = http_client.request(
                request.method,
                remote_url,
                headers={**_content_factory_headers(), **forwarded_headers},
                timeout=(3, 45),
            )
        except http_client.RequestException as exc:
            return HttpResponse(
                f"Preview proxy failed: {exc}",
                status=502,
                content_type="text/plain; charset=utf-8",
            )

        django_response = HttpResponse(
            response.content if request.method != "HEAD" else b"",
            status=response.status_code,
            content_type=response.headers.get("Content-Type") or "application/octet-stream",
        )
        for header, value in response.headers.items():
            lowered = header.lower()
            if lowered in {
                "content-type",
                "content-length",
                "connection",
                "transfer-encoding",
                "content-encoding",
                "content-security-policy-report-only",
                "content-security-policy",
                "x-frame-options",
            }:
                continue
            django_response[header] = value
        return django_response

    def get(self, request, run_id, proxy_path=""):
        return self._proxy(request, run_id, proxy_path)

    def head(self, request, run_id, proxy_path=""):
        return self._proxy(request, run_id, proxy_path)


def _accepted_component_revision_for_publish(run, context):
    result = _run_mapping(run.result)
    latest_batch = result.get("component_feedback_latest_batch")
    if not isinstance(latest_batch, dict) or latest_batch.get("status") != "accepted":
        return None
    revision_run_id = str(
        latest_batch.get("revisionRunId")
        or latest_batch.get("revision_run_id")
        or result.get("component_feedback_revision_run_id")
        or ""
    ).strip()
    if not revision_run_id:
        return None
    revision_run = ContentFactoryRun.objects.filter(run_id=revision_run_id).first()
    if not revision_run or not _run_belongs_to_context(revision_run, context):
        return None
    if revision_run.status != ContentFactoryRunStatus.COMPLETED:
        return None
    return revision_run


class VibeMarketingRunControlView(APIView):
    def post(self, request, run_id, action):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun, run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)

        payload = dict(request.data or {})
        payload.setdefault("request_source", CONTENT_FACTORY_REQUEST_SOURCE)
        payload.setdefault("slack_user_id", founder_actor_id_for_user(request.user))

        if action == "cancel":
            if run.workflow not in ARTICLE_WORKFLOWS:
                return Response({"detail": "Only article runs can be cancelled from this action."}, status=status.HTTP_400_BAD_REQUEST)
            if _run_has_external_publish_evidence(run):
                return Response(
                    {"detail": "This article already has external publish evidence. Close or clean up the external PR manually before cancelling."},
                    status=status.HTTP_409_CONFLICT,
                )

            remote_data = _call_content_factory_run_action(run_id=run_id, action=action, payload=payload, workflow=run.workflow)
            if int(remote_data.get("content_factory_status_code") or 0) == 409:
                detail = remote_data.get("error") or "Content Factory rejected cancellation for this run."
                return Response({"detail": detail}, status=status.HTTP_409_CONFLICT)

            run = _cancel_local_article_run(run=run, organization=context.organization, remote_data=remote_data)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
            return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)

        remote_run = run
        if action in {"promote-bundle", "publish-pr"}:
            accepted_revision = _accepted_component_revision_for_publish(run, context)
            if accepted_revision is not None:
                remote_run = accepted_revision
                payload.setdefault("review_source_run_id", run.run_id)
                payload.setdefault("source_run_id", accepted_revision.run_id)
        remote_data = _call_content_factory_run_action(
            run_id=remote_run.run_id,
            action=action,
            payload=payload,
            workflow=remote_run.workflow,
        )

        if action == "revise":
            new_run_id = str(remote_data.get("run_id") or remote_data.get("runId") or "").strip()
            if new_run_id and new_run_id != run.run_id:
                config = _get_config(context.organization)
                revised_run = _create_local_run(
                    workflow="article_generation",
                    domain=context.organization.domain,
                    github_repo=config.github_repo or run.github_repo or "",
                    actor_id=founder_actor_id_for_user(request.user),
                    payload=payload,
                    remote_data=remote_data,
                )
                return Response(_serialize_run(revised_run, context=context), status=status.HTTP_202_ACCEPTED)

            result = run.result or {}
            revisions = list(result.get("revisions") or [])
            revisions.append(
                {
                    "submitted_at": timezone.now().isoformat(),
                    "instructions": payload.get("revision_instructions") or payload.get("revisionInstructions") or "",
                    "edited_content": payload.get("edited_content") or payload.get("editedContent") or "",
                    "component_id": payload.get("component_id") or payload.get("componentId") or "",
                    "component_type": payload.get("component_type") or payload.get("componentType") or "",
                    "remote": remote_data,
                }
            )
            result["revisions"] = revisions
            if remote_data:
                result["latest_revision_response"] = remote_data
            run.result = result
            if run.status in {ContentFactoryRunStatus.COMPLETED, ContentFactoryRunStatus.AWAITING_APPROVAL, ContentFactoryRunStatus.APPROVAL_REQUIRED}:
                run.status = ContentFactoryRunStatus.QUEUED
                run.current_step = "revision_requested"
            run.save(update_fields=["status", "current_step", "result", "updated_at"])
            return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)

        if action in {"promote-bundle", "publish-pr"}:
            new_run_id = str(remote_data.get("run_id") or remote_data.get("runId") or remote_data.get("job_id") or "").strip()
            if new_run_id and new_run_id != run.run_id:
                config = _get_config(context.organization)
                publish_payload = {
                    **payload,
                    "source_run_id": remote_run.run_id,
                    "delivery_mode": "publish_code",
                    "delivery_mode_confirmed": True,
                }
                if remote_run.run_id != run.run_id:
                    publish_payload["review_source_run_id"] = run.run_id
                publish_run = _create_local_run(
                    workflow="article_generation",
                    domain=context.organization.domain,
                    github_repo=config.github_repo or run.github_repo or "",
                    actor_id=founder_actor_id_for_user(request.user),
                    payload=publish_payload,
                    remote_data=remote_data,
                )
                result = run.result or {}
                result["publish_child_run_id"] = publish_run.run_id
                result["latest_control_response"] = remote_data
                result["promote_bundle_requested_at"] = timezone.now().isoformat()
                run.result = result
                run.save(update_fields=["result", "updated_at"])
                if remote_run.run_id != run.run_id:
                    remote_result = remote_run.result or {}
                    remote_result["publish_child_run_id"] = publish_run.run_id
                    remote_result["latest_control_response"] = remote_data
                    remote_result["promote_bundle_requested_at"] = result["promote_bundle_requested_at"]
                    remote_run.result = remote_result
                    remote_run.save(update_fields=["result", "updated_at"])
                return Response(_serialize_run(publish_run, context=context), status=status.HTTP_202_ACCEPTED)

        if action == "approve":
            run.approval_state = ContentFactoryApprovalState.APPROVED
            run.status = ContentFactoryRunStatus.RUNNING
        elif action == "deny":
            run.approval_state = ContentFactoryApprovalState.DENIED
            run.status = ContentFactoryRunStatus.DENIED
        elif action == "resume":
            run.resume_available = True
            if run.status in {ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.DENIED}:
                run.status = ContentFactoryRunStatus.QUEUED
        elif action == "delivery-mode":
            result = run.result or {}
            result["delivery_mode"] = request.data.get("delivery_mode") or request.data.get("deliveryMode")
            run.result = result
        elif action == "promote-bundle":
            result = run.result or {}
            result["promote_bundle_requested_at"] = timezone.now().isoformat()
            run.result = result
        else:
            return Response({"detail": "Unsupported run action."}, status=status.HTTP_400_BAD_REQUEST)

        if remote_data:
            result = run.result or {}
            result["latest_control_response"] = remote_data
            if remote_data.get("status"):
                run.status = remote_data["status"]
            if remote_data.get("current_step"):
                run.current_step = remote_data["current_step"]
            run.result = result
        run.save(update_fields=["approval_state", "status", "current_step", "resume_available", "result", "updated_at"])
        return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)


class VibeMarketingDailyReplayView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        checks = _profile_checks(
            context.organization,
            config,
            baseline_snapshot=_latest_baseline_snapshot(context.organization),
        )
        if not checks["dailyAutomation"]["passed"]:
            return Response(
                {"detail": "Daily generation prerequisites are not complete.", "checks": checks},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {
            "domain": context.organization.domain,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "trigger_source": "founder_tools_manual_replay",
        }
        run = _create_local_run(
            workflow="vibe_marketing_daily_replay",
            domain=context.organization.domain,
            github_repo=config.github_repo or "",
            actor_id=founder_actor_id_for_user(request.user),
            payload=payload,
            remote_data={"status": ContentFactoryRunStatus.QUEUED, "message": "Daily replay queued"},
        )
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)
