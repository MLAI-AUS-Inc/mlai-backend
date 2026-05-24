from __future__ import annotations

import ast
import copy
import hashlib
import ipaddress
import json
import logging
import re
import socket
import uuid
import time
from io import BytesIO
from datetime import timedelta, timezone as datetime_timezone
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
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
from rest_framework.parsers import FormParser, MultiPartParser
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
    GeneratedComponent,
    KeywordStatus,
    KeywordVelocity,
    OrganizationContentConfig,
    PAQuestion,
    ResearchedKeyword,
    SemanticCluster,
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
from integrations.services.github_app import GitHubAppTokenError, create_installation_access_token, github_app_credentials_configured
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
    "article_system_setup",
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
ARTICLE_SYSTEM_SETUP_WORKFLOWS = {"article_system_setup"}
RESTARTABLE_ARTICLE_WORKFLOWS = {"article_generation", "content_factory_article", "direct_generate", "confirmed_topic"}
BASELINE_WORKFLOWS = {"website_baseline"}
DISCOVERY_TOPIC_CANDIDATE_STATUSES = {
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.COMPLETED,
}
RECENT_DISCOVERY_TOPIC_RUN_LIMIT = 12
CONTENT_ISLAND_METADATA_KEYS = (
    "pillarSlug",
    "pillarName",
    "pillarKeyword",
    "pillarIconKey",
    "pillarColorKey",
)
MLAI_AU_FEATURED_REQUIRED_COMPONENTS = {
    "ArticleDisclaimer",
    "ArticleHeroHeader",
    "ArticleReferences",
    "ArticleResourceCTA",
    "ArticleStepList",
    "ArticleTocPlaceholder",
    "MLAITemplateResourceCTA",
}
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
        "href": "/founder-tools/marketing/create?step=articleSystem",
        "check": "github",
        "summary": "Connect the repository used for exact article previews and publishing.",
    },
    {
        "id": "article_system",
        "label": "Articles location",
        "phase": "Setup",
        "href": "/founder-tools/marketing/create?step=articleSystem",
        "check": "scaffold",
        "summary": "Detect or prepare the articles/blogs route and repo location.",
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
STARTUP_AUTOFILL_ACTIVE_REUSE_WINDOW = timedelta(minutes=30)
SCAN_LOCAL_AUTHORITATIVE_STATUSES = FAILED_RUN_STATUSES | {
    ContentFactoryRunStatus.COMPLETED,
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.AWAITING_APPROVAL,
    ContentFactoryRunStatus.APPROVAL_REQUIRED,
}
ARTICLE_SYSTEM_PUBLISHED_STATES = {
    "existing",
    "ready",
    "detected",
    "registry_driven_seo_ready",
    "article_system_ready",
}
ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES = {
    "pending",
    "pending_generation",
    "queued",
    "running",
    "processing",
    "preview_building",
    "preview_ready",
    "revision_ready",
    "awaiting_approval",
    "awaiting_confirmation",
    "approval_required",
    "await_review",
    "preview_failed",
    "failed",
    "blocked",
    "manual_merge_required",
    "manual_blocked",
    "completed",
    "merged",
    "merged_verifying",
    "verifying",
}
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


def _domain_host_variants(domain: str) -> set[str]:
    text = str(domain or "").strip().lower()
    if not text:
        return set()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text):
        text = f"https://{text}"
    parsed = urlsplit(text)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return set()
    base = host[4:] if host.startswith("www.") else host
    return {base, f"www.{base}"}


def _normalize_article_surface_route_path(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    if not path.startswith("/"):
        raise ValueError("Article/blog path must start with '/'.")
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _article_surface_hint_from_request(data, *, domain: str) -> tuple[dict, str, str]:
    mode = str(
        _request_value(data, "articleSurfaceMode", "article_surface_mode", default="not_sure") or "not_sure"
    ).strip()
    if mode not in {"existing", "none", "not_sure"}:
        raise ValueError("articleSurfaceMode must be existing, none, or not_sure.")

    raw_url = str(_request_value(data, "articleSurfaceUrl", "article_surface_url", default="") or "").strip()
    if mode == "existing" and not raw_url:
        raise ValueError("Article/blog URL or path is required when using an existing page.")
    if not raw_url:
        return {}, mode, ""

    if raw_url.startswith("/"):
        route_path = _normalize_article_surface_route_path(raw_url)
    elif re.match(r"^https?://", raw_url, flags=re.IGNORECASE):
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not parsed.scheme or parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Article/blog URL must be an http(s) URL or a site path.")
        if host not in _domain_host_variants(domain):
            raise ValueError("Article/blog URL must use the company website domain.")
        route_path = _normalize_article_surface_route_path(parsed.path or "/")
    else:
        raise ValueError("Article/blog URL must be a full same-domain URL or a path starting with '/'.")

    return {"source": "user_input", "listing_url": raw_url, "route_path": route_path}, mode, raw_url


def _pending_article_system_setup_from_config(config) -> dict:
    article_system = config.article_system if isinstance(getattr(config, "article_system", None), dict) else {}
    pending = article_system.get("pending_article_system_setup")
    return pending if isinstance(pending, dict) else {}


def _store_pending_article_system_setup(config, *, mode: str, route_path: str, source_scan_run_id: str = "", article_surface_hint=None):
    article_system = dict(config.article_system or {})
    saved_at = timezone.now().isoformat()
    pending = {
        "mode": str(mode or "not_sure"),
        "routePath": str(route_path or ""),
        "route_path": str(route_path or ""),
        "sourceScanRunId": str(source_scan_run_id or ""),
        "source_scan_run_id": str(source_scan_run_id or ""),
        "articleSurfaceHint": article_surface_hint or {},
        "article_surface_hint": article_surface_hint or {},
        "status": "pending_generation",
        "savedAt": saved_at,
        "saved_at": saved_at,
    }
    article_system["pending_article_system_setup"] = pending
    config.article_system = article_system
    config.save(update_fields=["article_system", "updated_at"])
    return pending


def _serialize_github_repo_payload(repo: dict, *, installation_id: str = "") -> dict:
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    full_name = str(repo.get("full_name") or "").strip()
    owner_name = str(owner.get("login") or (full_name.split("/", 1)[0] if "/" in full_name else "")).strip()
    name = str(repo.get("name") or (full_name.split("/", 1)[-1] if full_name else "")).strip()
    return {
        "fullName": full_name,
        "full_name": full_name,
        "owner": owner_name,
        "name": name,
        "private": bool(repo.get("private")),
        "defaultBranch": str(repo.get("default_branch") or "").strip(),
        "default_branch": str(repo.get("default_branch") or "").strip(),
        "installationId": str(installation_id or "").strip(),
        "installation_id": str(installation_id or "").strip(),
    }


def _list_github_repositories_for_token(*, token: str, installation_id: str = "") -> list[dict]:
    if installation_id:
        payload = _github_api_request(
            "GET",
            f"/user/installations/{installation_id}/repositories?per_page=100",
            token=token,
        )
        repos = payload.get("repositories") if isinstance(payload, dict) else []
    else:
        payload = _github_api_request(
            "GET",
            "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member",
            token=token,
        )
        repos = payload if isinstance(payload, list) else []
    return [
        _serialize_github_repo_payload(repo, installation_id=installation_id)
        for repo in repos
        if isinstance(repo, dict) and str(repo.get("full_name") or "").strip()
    ]


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


def _recent_discovery_topic_runs_for_org(organization, limit=RECENT_DISCOVERY_TOPIC_RUN_LIMIT):
    return list(
        ContentFactoryRun.objects.filter(
            domain=organization.domain,
            workflow__in=DISCOVERY_WORKFLOWS,
            status__in=DISCOVERY_TOPIC_CANDIDATE_STATUSES,
        ).order_by("-updated_at")[:limit]
    )


def _active_startup_autofill_run_for_domain(domain):
    cutoff = timezone.now() - STARTUP_AUTOFILL_ACTIVE_REUSE_WINDOW
    return (
        ContentFactoryRun.objects.filter(
            domain=domain,
            workflow="startup_autofill",
            status__in=RUNNING_RUN_STATUSES,
            updated_at__gte=cutoff,
        )
        .order_by("-updated_at")
        .first()
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


def _content_island_metadata_from_mapping(mapping):
    if not isinstance(mapping, dict):
        return {}
    nested = mapping.get("content_island") or mapping.get("contentIsland") or {}
    nested = nested if isinstance(nested, dict) else {}
    return {
        "pillarSlug": mapping.get("pillarSlug")
        or mapping.get("pillar_slug")
        or mapping.get("contentIslandSlug")
        or mapping.get("content_island_slug")
        or nested.get("slug"),
        "pillarName": mapping.get("pillarName")
        or mapping.get("pillar_name")
        or mapping.get("contentIslandName")
        or mapping.get("content_island_name")
        or nested.get("name"),
        "pillarKeyword": mapping.get("pillarKeyword")
        or mapping.get("pillar_keyword")
        or mapping.get("contentIslandKeyword")
        or mapping.get("content_island_keyword")
        or nested.get("keyword"),
        "pillarIconKey": mapping.get("pillarIconKey")
        or mapping.get("pillar_icon_key")
        or mapping.get("contentIslandIconKey")
        or mapping.get("content_island_icon_key")
        or nested.get("iconKey")
        or nested.get("icon_key"),
        "pillarColorKey": mapping.get("pillarColorKey")
        or mapping.get("pillar_color_key")
        or mapping.get("contentIslandColorKey")
        or mapping.get("content_island_color_key")
        or nested.get("colorKey")
        or nested.get("color_key"),
    }


def _extract_topic_candidates_from_result(result):
    if not isinstance(result, dict):
        return []
    raw_candidates = []
    inherited_island_metadata = _content_island_metadata_from_mapping(result)
    for mapping in (result, result.get("selection_data"), result.get("selection")):
        if not isinstance(mapping, dict):
            continue
        inherited_island_metadata = {
            **inherited_island_metadata,
            **{
                key: value
                for key, value in _content_island_metadata_from_mapping(mapping).items()
                if value
            },
        }
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
                    **{key: value for key, value in inherited_island_metadata.items() if value},
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
            or raw.get("display_title")
            or raw.get("displayTitle")
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
        raw_island_metadata = _content_island_metadata_from_mapping(raw)
        island_metadata = {
            key: raw_island_metadata.get(key) or inherited_island_metadata.get(key)
            for key in inherited_island_metadata.keys() | raw_island_metadata.keys()
        }
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
                **{key: value for key, value in island_metadata.items() if value},
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
    pillar_metadata = _keyword_pillar_metadata(keyword)
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
        **pillar_metadata,
    }


PILLAR_VISUALS = (
    {"iconKey": "brain", "colorKey": "green"},
    {"iconKey": "community", "colorKey": "purple"},
    {"iconKey": "rocket", "colorKey": "blue"},
    {"iconKey": "tools", "colorKey": "orange"},
)


def _pillar_visual(index):
    return PILLAR_VISUALS[index % len(PILLAR_VISUALS)]


def _pillar_slug(value, *, fallback="pillar"):
    slug = slugify(str(value or "").strip())
    return slug or fallback


def _pillar_title(value):
    value = str(value or "").strip()
    return value or "Content pillar"


def _keyword_pillar_metadata(keyword):
    try:
        memberships = list(keyword.cluster_memberships.all())
    except Exception:
        memberships = []
    membership = memberships[0] if memberships else None
    cluster = getattr(membership, "cluster", None)
    if not cluster:
        return {}
    pillar_keyword = _pillar_title(getattr(cluster, "pillar_keyword", ""))
    return {
        "pillarSlug": _pillar_slug(pillar_keyword, fallback=f"cluster-{getattr(cluster, 'cluster_id', 'pillar')}"),
        "pillarName": pillar_keyword,
        "pillarKeyword": pillar_keyword,
    }


def _pillar_strategy_entries(config):
    strategy = getattr(config, "pillar_strategy", None) or {}
    if not isinstance(strategy, dict):
        return []
    pillars = strategy.get("pillars")
    if not isinstance(pillars, list):
        return []
    return [pillar for pillar in pillars if isinstance(pillar, dict)]


def _pillar_strategy_lookup(config):
    lookup = {}
    for pillar in _pillar_strategy_entries(config):
        name = str(pillar.get("name") or pillar.get("title") or pillar.get("pillar") or "").strip()
        keyword = str(pillar.get("keyword") or pillar.get("pillar_keyword") or pillar.get("pillarKeyword") or name).strip()
        slug = _pillar_slug(pillar.get("slug") or keyword or name)
        metadata = {
            "name": name or keyword,
            "slug": slug,
            "description": str(pillar.get("description") or pillar.get("summary") or "").strip(),
            "raw": pillar,
        }
        for value in (slug, name, keyword):
            key = normalize_topic_feedback_keyword(value)
            if key:
                lookup[key] = metadata
    return lookup


def _pillar_strategy_topics(raw_pillar):
    for key in (
        "topicCandidates",
        "topic_candidates",
        "articleIdeas",
        "article_ideas",
        "topics",
        "keywords",
        "seedKeywords",
        "seed_keywords",
    ):
        value = raw_pillar.get(key)
        if isinstance(value, list):
            return value
    return []


def _derive_pillar_description(name, candidates):
    related = [
        str(candidate.get("keyword") or candidate.get("title") or "").strip()
        for candidate in candidates[:3]
        if str(candidate.get("keyword") or candidate.get("title") or "").strip()
    ]
    if related:
        if len(related) == 1:
            return f"Educational content about {related[0]} and related topics."
        return f"Educational content about {', '.join(related[:-1])}, and {related[-1]}."
    return f"Article ideas and search demand around {name}."


def _topic_candidate_from_strategy_topic(raw_topic, *, pillar_slug, pillar_name, pillar_keyword, index):
    if isinstance(raw_topic, dict):
        keyword = str(
            raw_topic.get("keyword")
            or raw_topic.get("targetKeyword")
            or raw_topic.get("target_keyword")
            or raw_topic.get("title")
            or raw_topic.get("angle")
            or ""
        ).strip()
        title = str(raw_topic.get("title") or raw_topic.get("angle") or keyword).strip() or keyword
        reason = str(raw_topic.get("reason") or raw_topic.get("summary") or "Suggested from stored pillar strategy.").strip()
        candidate = {
            "id": str(raw_topic.get("id") or f"pillar:{pillar_slug}:{index}"),
            "keyword": keyword or title,
            "title": title or keyword,
            "reason": reason,
            "source": "pillar_strategy",
            "status": "pending",
            "alreadyWritten": False,
            "volume": raw_topic.get("volume"),
            "difficulty": raw_topic.get("difficulty"),
            "opportunityScore": raw_topic.get("opportunityScore") or raw_topic.get("opportunity_score"),
        }
    else:
        keyword = str(raw_topic or "").strip()
        candidate = {
            "id": f"pillar:{pillar_slug}:{index}",
            "keyword": keyword,
            "title": keyword,
            "reason": "Suggested from stored pillar strategy.",
            "source": "pillar_strategy",
            "status": "pending",
            "alreadyWritten": False,
        }
    if not candidate["keyword"] and not candidate["title"]:
        return None
    return {
        **candidate,
        "pillarSlug": pillar_slug,
        "pillarName": pillar_name,
        "pillarKeyword": pillar_keyword,
    }


def _topic_pillars_from_strategy(config, *, compact=False):
    pillars = []
    candidate_limit = 8 if compact else 30
    for index, raw_pillar in enumerate(_pillar_strategy_entries(config)):
        name = _pillar_title(raw_pillar.get("name") or raw_pillar.get("title") or raw_pillar.get("pillar"))
        pillar_keyword = _pillar_title(raw_pillar.get("keyword") or raw_pillar.get("pillar_keyword") or raw_pillar.get("pillarKeyword") or name)
        slug = _pillar_slug(raw_pillar.get("slug") or pillar_keyword or name, fallback=f"pillar-{index + 1}")
        candidates = [
            candidate
            for candidate in (
                _topic_candidate_from_strategy_topic(
                    topic,
                    pillar_slug=slug,
                    pillar_name=name,
                    pillar_keyword=pillar_keyword,
                    index=topic_index,
                )
                for topic_index, topic in enumerate(_pillar_strategy_topics(raw_pillar), start=1)
            )
            if candidate
        ]
        visual = _pillar_visual(index)
        description = str(raw_pillar.get("description") or raw_pillar.get("summary") or "").strip()
        pillars.append(
            {
                "id": str(raw_pillar.get("id") or f"pillar-strategy:{slug}"),
                "slug": slug,
                "name": name,
                "description": description or _derive_pillar_description(name, candidates),
                "ideaCount": len(candidates),
                "iconKey": visual["iconKey"],
                "colorKey": visual["colorKey"],
                "source": "pillar_strategy",
                "topicCandidates": candidates[:candidate_limit],
            }
        )
    return pillars


def _topic_pillars_from_clusters(organization, config, *, declined_keyword_keys=None, coverage_memory=None, compact=False):
    declined_keyword_keys = declined_keyword_keys or set()
    if coverage_memory is None:
        coverage_memory = build_topic_coverage_memory(organization)
    strategy_lookup = _pillar_strategy_lookup(config)
    candidate_limit = 8 if compact else 30
    memberships_queryset = (
        ClusterMembership.objects.select_related("keyword")
        .prefetch_related(
            Prefetch("keyword__velocity_snapshots", queryset=KeywordVelocity.objects.order_by("-captured_at")),
            Prefetch("keyword__ai_saturation_snapshots", queryset=AISaturation.objects.order_by("-captured_at")),
            Prefetch("keyword__paa_questions", queryset=PAQuestion.objects.order_by("depth", "order")),
            Prefetch(
                "keyword__cluster_memberships",
                queryset=ClusterMembership.objects.select_related("cluster"),
            ),
        )
        .order_by("-is_pillar", "-keyword__opportunity_index", "-keyword__volume", "keyword__keyword")
    )
    clusters = (
        SemanticCluster.objects.filter(organization=organization)
        .prefetch_related(Prefetch("member_keywords", queryset=memberships_queryset))
        .order_by("-total_volume", "pillar_keyword")
    )
    pillars = []
    for cluster in clusters:
        pillar_keyword = _pillar_title(cluster.pillar_keyword)
        cluster_slug = _pillar_slug(pillar_keyword, fallback=f"cluster-{cluster.cluster_id}")
        strategy_metadata = (
            strategy_lookup.get(normalize_topic_feedback_keyword(cluster_slug))
            or strategy_lookup.get(normalize_topic_feedback_keyword(pillar_keyword))
            or {}
        )
        slug = strategy_metadata.get("slug") or cluster_slug
        name = strategy_metadata.get("name") or pillar_keyword
        description = strategy_metadata.get("description") or ""
        seen_keywords = set()
        candidates = []
        for membership in cluster.member_keywords.all():
            keyword = membership.keyword
            keyword_key = normalize_topic_feedback_keyword(keyword.keyword)
            if not keyword_key or keyword_key in seen_keywords or keyword_key in declined_keyword_keys:
                continue
            seen_keywords.add(keyword_key)
            coverage_match = match_covered_topic(keyword=keyword.keyword, memory=coverage_memory)
            if not _keyword_is_available_for_topic_picker(keyword, coverage_memory=coverage_memory):
                continue
            candidate = _apply_topic_coverage_to_candidate(_topic_candidate_from_keyword(keyword), coverage_match)
            candidates.append(
                {
                    **candidate,
                    "pillarSlug": slug,
                    "pillarName": name,
                    "pillarKeyword": pillar_keyword,
                }
            )
        if not candidates:
            continue
        visual = _pillar_visual(len(pillars))
        pillars.append(
            {
                "id": f"semantic-cluster:{cluster.id}",
                "slug": slug,
                "name": name,
                "description": description or _derive_pillar_description(name, candidates),
                "ideaCount": len(candidates),
                "iconKey": visual["iconKey"],
                "colorKey": visual["colorKey"],
                "source": "semantic_cluster",
                "topicCandidates": candidates[:candidate_limit],
            }
        )
    return pillars


def _topic_pillars_for_bootstrap(organization, config, *, declined_keyword_keys=None, coverage_memory=None, compact=False):
    cluster_pillars = _topic_pillars_from_clusters(
        organization,
        config,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        compact=compact,
    )
    if cluster_pillars:
        return cluster_pillars
    return _topic_pillars_from_strategy(config, compact=compact)


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


def _written_topic_memory(organization, *, keyword_limit=None):
    keyword_qs = ResearchedKeyword.objects.filter(organization=organization).select_related("written_article")
    if keyword_limit:
        keyword_qs = keyword_qs.order_by("-metrics_updated_at")[:keyword_limit]
    keywords = {keyword.keyword_normalized: keyword for keyword in keyword_qs}
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


def _topic_candidate_passes_dashboard_quality(candidate):
    difficulty = _safe_number(candidate.get("difficulty"), default=0)
    volume = _safe_number(candidate.get("volume"), default=0)
    opportunity_score = _safe_number(candidate.get("opportunityScore") or candidate.get("opportunity_score"), default=0)
    return difficulty <= 50 and (volume >= 50 or opportunity_score >= 500)


def _apply_content_island_metadata(candidate, metadata):
    if not metadata:
        return candidate
    enriched = dict(candidate)
    for key, value in metadata.items():
        if value and not enriched.get(key):
            enriched[key] = value
    return enriched


def _candidate_has_content_island_metadata(candidate):
    return any(candidate.get(key) for key in CONTENT_ISLAND_METADATA_KEYS)


def _candidate_non_empty_items(candidate):
    return {
        item_key: item_value
        for item_key, item_value in candidate.items()
        if item_value not in (None, "", [])
    }


def _prefer_numeric_metric(existing, candidate, key):
    existing_value = existing.get(key)
    candidate_value = candidate.get(key)
    if existing_value in (None, ""):
        return candidate_value
    if candidate_value in (None, ""):
        return existing_value
    return existing_value if _safe_number(existing_value) >= _safe_number(candidate_value) else candidate_value


def _merge_topic_candidate(existing, candidate):
    difficulty, difficulty_source = _prefer_topic_difficulty(existing, candidate)
    existing_has_island = _candidate_has_content_island_metadata(existing)
    candidate_has_island = _candidate_has_content_island_metadata(candidate)
    existing_is_run_candidate = bool(existing.get("sourceRunId"))
    candidate_is_run_candidate = bool(candidate.get("sourceRunId"))
    candidate_values = _candidate_non_empty_items(candidate)

    if existing_has_island and not candidate_has_island:
        merged = {**candidate_values, **existing}
    elif existing_has_island and candidate_has_island:
        merged = {**candidate_values, **existing}
    elif candidate_has_island and not existing_has_island:
        merged = {**existing, **candidate_values}
    elif existing_is_run_candidate and not candidate_is_run_candidate:
        merged = {**candidate_values, **existing}
    else:
        merged = {**existing, **candidate_values}

    merged.update(
        {
            "volume": _prefer_numeric_metric(existing, candidate, "volume"),
            "difficulty": difficulty,
            "difficultySource": difficulty_source,
            "opportunityScore": _prefer_numeric_metric(existing, candidate, "opportunityScore"),
            "alreadyWritten": bool(existing.get("alreadyWritten") or candidate.get("alreadyWritten")),
            "writtenArticle": existing.get("writtenArticle") or candidate.get("writtenArticle"),
        }
    )
    return merged


def _topic_candidate_sort_key(candidate):
    return (
        -_safe_number(candidate.get("opportunityScore")),
        -_safe_number(candidate.get("volume")),
        _safe_number(candidate.get("difficulty"), default=100),
        str(candidate.get("keyword") or ""),
    )


def _topic_candidate_island_key(candidate):
    return str(candidate.get("pillarSlug") or "").strip()


def _balanced_topic_candidate_order(candidates, island_order):
    candidates = list(candidates)
    if not island_order:
        return sorted(candidates, key=_topic_candidate_sort_key)

    island_groups = {}
    fallback_candidates = []
    for candidate in candidates:
        island_key = _topic_candidate_island_key(candidate)
        if island_key and island_key in island_order:
            island_groups.setdefault(island_key, []).append(candidate)
        else:
            fallback_candidates.append(candidate)

    for island_key in island_groups:
        island_groups[island_key] = sorted(island_groups[island_key], key=_topic_candidate_sort_key)

    ordered_island_keys = sorted(island_groups, key=lambda key: (island_order.get(key, 9999), key))
    balanced = []
    while any(island_groups[key] for key in ordered_island_keys):
        for island_key in ordered_island_keys:
            if island_groups[island_key]:
                balanced.append(island_groups[island_key].pop(0))

    return balanced + sorted(fallback_candidates, key=_topic_candidate_sort_key)


def _topic_candidates_from_runs(
    runs,
    *,
    organization=None,
    include_written=False,
    limit=None,
    declined_keyword_keys=None,
    coverage_memory=None,
    written_memory=None,
):
    run_candidates = []
    island_order = {}
    for run in runs:
        if run.workflow not in DISCOVERY_WORKFLOWS:
            continue
        if run.status not in DISCOVERY_TOPIC_CANDIDATE_STATUSES:
            continue
        candidates = _extract_topic_candidates_from_result(run.result or {})
        if candidates:
            run_island_metadata = _content_island_metadata_from_mapping(run.run_request or {})
            for candidate in candidates:
                candidate["sourceRunId"] = candidate.get("sourceRunId") or run.run_id
                candidate.update(_apply_content_island_metadata(candidate, run_island_metadata))
            candidates = [
                candidate
                for candidate in candidates
                if _topic_candidate_passes_dashboard_quality(candidate)
            ]
        if candidates:
            for candidate in candidates:
                island_key = _topic_candidate_island_key(candidate)
                if island_key and island_key not in island_order:
                    island_order[island_key] = len(island_order)
            run_candidates.extend(candidates)
    if organization is None:
        ordered_run_candidates = _balanced_topic_candidate_order(run_candidates, island_order)
        return ordered_run_candidates[:limit] if limit else ordered_run_candidates

    declined_keyword_keys = declined_keyword_keys or set()
    stored_candidates = _stored_keyword_topic_candidates(
        organization,
        include_written=include_written,
        limit=limit or 50,
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
    for candidate in [*enriched_run_candidates, *stored_candidates]:
        key = _normalize_keyword_memory(candidate.get("keyword"))
        if not key:
            continue
        existing = merged.get(key)
        if existing:
            merged[key] = _merge_topic_candidate(existing, candidate)
        else:
            merged[key] = candidate

    sorted_candidates = _balanced_topic_candidate_order(merged.values(), island_order)
    return sorted_candidates[:limit] if limit else sorted_candidates


def _recent_written_topics(organization, *, limit=8):
    return [
        _serialize_written_article(article)
        for article in WrittenArticle.objects.filter(organization=organization).order_by("-created_at")[:limit]
    ]


def _written_article_identity_keys(organization):
    keys = {"slugs": set(), "keywords": set()}
    for article in WrittenArticle.objects.filter(organization=organization).only("slug", "primary_keyword", "title")[:500]:
        slug = slugify(str(article.slug or article.title or ""))
        keyword = _normalize_keyword_memory(article.primary_keyword)
        title_slug = slugify(str(article.title or ""))
        if slug:
            keys["slugs"].add(slug)
        if title_slug:
            keys["slugs"].add(title_slug)
        if keyword:
            keys["keywords"].add(keyword)
    return keys


def _article_draft_title_keyword(run):
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    package = _content_package_from_run(run) or {}
    title = str(
        package.get("title")
        or run_request.get("custom_title")
        or run_request.get("customTitle")
        or run_request.get("selected_title")
        or run_request.get("selectedTitle")
        or run_request.get("topic")
        or result.get("title")
        or result.get("topic")
        or ""
    ).strip()
    keyword = str(
        package.get("targetKeyword")
        or run_request.get("target_keyword")
        or run_request.get("targetKeyword")
        or run_request.get("keyword")
        or result.get("target_keyword")
        or result.get("targetKeyword")
        or title
        or ""
    ).strip()
    if not title and keyword:
        title = keyword
    return title, keyword


def _article_draft_matches_written(run, written_keys):
    title, keyword = _article_draft_title_keyword(run)
    package = _content_package_from_run(run) or {}
    slugs = written_keys.get("slugs") or set()
    keywords = written_keys.get("keywords") or set()
    candidate_slugs = {
        slugify(str(package.get("slug") or "")),
        slugify(title),
    }
    candidate_keywords = {_normalize_keyword_memory(keyword), _normalize_keyword_memory(title)}
    return bool(
        any(candidate for candidate in candidate_slugs if candidate and candidate in slugs)
        or any(candidate for candidate in candidate_keywords if candidate and candidate in keywords)
    )


def _article_draft_stage_label(run):
    status_value = str(run.status or "").strip()
    current_step = str(run.current_step or "").strip().lower()
    if status_value in {ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.DENIED}:
        return "Needs attention"
    if status_value in {
        ContentFactoryRunStatus.COMPLETED,
        ContentFactoryRunStatus.AWAITING_APPROVAL,
        ContentFactoryRunStatus.APPROVAL_REQUIRED,
    }:
        return "Ready for review"
    if status_value == ContentFactoryRunStatus.AWAITING_DELIVERY_MODE:
        return "Choose delivery mode"
    if status_value == ContentFactoryRunStatus.AWAITING_CONFIRMATION:
        return "Needs confirmation"
    if current_step.startswith(("discover_", "collect_research", "research")):
        return "Researching"
    if current_step.startswith(("plan_", "draft_", "ground_", "assemble")):
        return "Writing draft"
    if "image" in current_step:
        return "Preparing assets"
    if "preview" in current_step or "verify" in current_step or "render" in current_step:
        return "Preparing preview"
    if status_value in RUNNING_RUN_STATUSES:
        return "In progress"
    return "Draft"


def _article_draft_action(run):
    status_value = str(run.status or "").strip()
    if status_value in {ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.DENIED}:
        if run.resume_available:
            return "resume", "Resume"
        if _article_restart_available(run):
            return "restart", "Restart"
        return "review", "Review issue"
    return "continue", "Continue"


def _article_restart_available(run):
    if run.workflow not in RESTARTABLE_ARTICLE_WORKFLOWS:
        return False
    if run.status not in {ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.DENIED}:
        return False
    title, keyword = _article_draft_title_keyword(run)
    return bool(title or keyword)


def _serialize_article_draft(run, *, written_keys):
    if run.workflow not in ARTICLE_WORKFLOWS or run.status == ContentFactoryRunStatus.CANCELLED:
        return None
    if run.status == ContentFactoryRunStatus.COMPLETED and _article_draft_matches_written(run, written_keys):
        return None

    title, keyword = _article_draft_title_keyword(run)
    if not title and not keyword:
        title = "Untitled article draft"
    action_kind, action_label = _article_draft_action(run)
    return {
        "runId": run.run_id,
        "sourceRunId": _run_source_run_id(run) or None,
        "workflow": run.workflow,
        "status": run.status,
        "title": title or keyword or "Untitled article draft",
        "targetKeyword": keyword or title or "",
        "stageLabel": _article_draft_stage_label(run),
        "actionKind": action_kind,
        "actionLabel": action_label,
        "resumeAvailable": bool(run.resume_available),
        "restartAvailable": _article_restart_available(run),
        "updatedAt": run.updated_at.isoformat() if run.updated_at else None,
        "createdAt": run.created_at.isoformat() if run.created_at else None,
    }


def _recent_article_drafts(organization, *, limit=8, scan_limit=50):
    written_keys = _written_article_identity_keys(organization)
    drafts = []
    seen = set()
    runs = (
        ContentFactoryRun.objects.filter(domain=organization.domain, workflow__in=ARTICLE_WORKFLOWS)
        .exclude(status=ContentFactoryRunStatus.CANCELLED)
        .prefetch_related("steps")
        .order_by("-updated_at")[:scan_limit]
    )
    for run in runs:
        draft = _serialize_article_draft(run, written_keys=written_keys)
        if not draft:
            continue
        identity = draft.get("sourceRunId") or draft.get("runId")
        if identity in seen:
            continue
        seen.add(identity)
        drafts.append(draft)
        if len(drafts) >= limit:
            break
    return drafts


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


def _normalize_live_preview_payload(payload):
    if not isinstance(payload, dict):
        return {}
    normalized = dict(payload)
    platform_status = str(normalized.get("platformStatus") or normalized.get("platform_status") or "").strip().lower()
    available = bool(normalized.get("available"))
    native_failure = normalized.get("nativePreviewFailure") or normalized.get("native_preview_failure") or {}
    if platform_status == "failed" and not available:
        normalized["status"] = "failed"
        if isinstance(native_failure, dict):
            native_error = native_failure.get("error") or ""
            native_code = native_failure.get("errorCode") or native_failure.get("error_code") or ""
            native_phase = native_failure.get("failedPhase") or native_failure.get("failed_phase") or ""
            native_command = native_failure.get("failedCommand") or native_failure.get("failed_command") or ""
            native_excerpt = native_failure.get("logExcerpt") or native_failure.get("log_excerpt") or ""
            if native_error and not normalized.get("error"):
                normalized["error"] = native_error
            if native_code and not (normalized.get("errorCode") or normalized.get("error_code")):
                normalized["errorCode"] = native_code
            if native_phase and not (normalized.get("failedPhase") or normalized.get("failed_phase")):
                normalized["failedPhase"] = native_phase
            if native_command and not (normalized.get("failedCommand") or normalized.get("failed_command")):
                normalized["failedCommand"] = native_command
            if native_excerpt and not (normalized.get("logExcerpt") or normalized.get("log_excerpt")):
                normalized["logExcerpt"] = native_excerpt
            if "retryable" in native_failure and "retryable" not in normalized:
                normalized["retryable"] = bool(native_failure.get("retryable"))
        if not (normalized.get("errorCode") or normalized.get("error_code")):
            normalized["errorCode"] = "platform_preview_failed"
        if not normalized.get("error"):
            normalized["error"] = "Hosted preview failed."
        if "retryable" not in normalized:
            normalized["retryable"] = True
    return normalized


def _live_preview_from_run(run):
    result = (run.result or {}) if run else {}
    payload = result.get("livePreview") or result.get("live_preview") or {}
    payload = _normalize_live_preview_payload(payload)
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
        "proofAcceptedWithWarnings": bool(
            payload.get("proofAcceptedWithWarnings")
            or payload.get("proof_accepted_with_warnings")
        ),
        "verificationSkippedForPreview": bool(
            payload.get("verificationSkippedForPreview")
            or payload.get("verification_skipped_for_preview")
        ),
        "previewMode": payload.get("previewMode") or payload.get("preview_mode") or "",
        "previewClientMode": payload.get("previewClientMode") or payload.get("preview_client_mode") or "",
        "clientHydrationDisabledForPreview": bool(
            payload.get("clientHydrationDisabledForPreview")
            or payload.get("client_hydration_disabled_for_preview")
        ),
        "renderMode": payload.get("renderMode") or payload.get("render_mode") or "",
        "renderConfidence": payload.get("renderConfidence") or payload.get("render_confidence") or "",
        "fallbackReason": payload.get("fallbackReason") or payload.get("fallback_reason") or "",
        "fallbackPreviewUrl": payload.get("fallbackPreviewUrl") or payload.get("fallback_preview_url") or "",
        "previewBuildMode": payload.get("previewBuildMode") or payload.get("preview_build_mode") or "",
        "fullSiteBuildSkipped": bool(payload.get("fullSiteBuildSkipped") or payload.get("full_site_build_skipped")),
        "nativePreviewFailure": payload.get("nativePreviewFailure") or payload.get("native_preview_failure") or {},
        "visualFallback": payload.get("visualFallback") or payload.get("visual_fallback") or {},
        "platformProvider": payload.get("platformProvider") or payload.get("platform_provider") or "",
        "platformStatus": payload.get("platformStatus") or payload.get("platform_status") or "",
        "deploymentId": payload.get("deploymentId") or payload.get("deployment_id") or "",
        "deploymentUrl": payload.get("deploymentUrl") or payload.get("deployment_url") or "",
        "routeUrl": payload.get("routeUrl") or payload.get("route_url") or "",
        "logsUrl": payload.get("logsUrl") or payload.get("logs_url") or "",
        "commitSha": payload.get("commitSha") or payload.get("commit_sha") or "",
        "branchName": payload.get("branchName") or payload.get("branch_name") or "",
        "builderWorkflow": payload.get("builderWorkflow") or payload.get("builder_workflow") or "",
        "builderRunUrl": payload.get("builderRunUrl") or payload.get("builder_run_url") or "",
    }


def _content_factory_live_preview_proxy_prefix(run_id):
    return f"/api/runs/{run_id}/live-preview/proxy"


def _backend_live_preview_proxy_prefix(run_id):
    return f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/proxy"


def _content_factory_live_preview_resource_prefix(run_id):
    return f"/api/runs/{run_id}/live-preview/resource"


def _backend_live_preview_resource_prefix(run_id):
    return f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/resource"


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
    preview_mode = str(rewritten.get("previewMode") or rewritten.get("preview_mode") or "").strip()
    if preview_mode == "platform_deployment":
        return rewritten
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


_LIVE_PREVIEW_PROXY_ASSET_PREFIXES = (
    "@react-router/",
    "@vite/",
    "@id/",
    "@fs/",
    "__cf-preview/",
    "__cf-resource",
    "app/",
    "node_modules/",
    "assets/",
    "public/",
    "static/",
    "src/",
)
_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN = "|".join(re.escape(prefix) for prefix in _LIVE_PREVIEW_PROXY_ASSET_PREFIXES)
_LIVE_PREVIEW_QUOTED_ASSET_RE = re.compile(
    rf"(?P<prefix>['\"])/(?P<path>(?:{_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN})[^'\"\s)]*)"
)
_LIVE_PREVIEW_CSS_URL_ASSET_RE = re.compile(
    rf"(?P<prefix>url\(\s*)(?P<quote>['\"]?)/(?P<path>(?:{_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN})[^'\"\s)]*)(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)
_LIVE_PREVIEW_CSS_EXTERNAL_URL_RE = re.compile(
    r"(?P<prefix>url\(\s*)(?P<quote>['\"]?)(?P<url>https?://[^'\"\s)]+)(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)
_LIVE_PREVIEW_CSS_IMPORT_ASSET_RE = re.compile(
    rf"(?P<prefix>@import\s+)(?P<quote>['\"])/(?P<path>(?:{_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN})[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
_LIVE_PREVIEW_CSS_EXTERNAL_IMPORT_RE = re.compile(
    r"(?P<prefix>@import\s+)(?P<quote>['\"])(?P<url>https?://[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
_LIVE_PREVIEW_UNQUOTED_ATTR_ASSET_RE = re.compile(
    rf"(?P<prefix>\b(?:src|href|action|poster)=)/(?P<path>(?:{_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN})[^\s>]+)",
    re.IGNORECASE,
)
_LIVE_PREVIEW_JS_IMPORT_ASSET_RE = re.compile(
    rf"(?P<prefix>\bimport(?:\s+[^'\"\n]+?\s+from\s+|\s*)\(?\s*['\"])/(?P<path>(?:{_LIVE_PREVIEW_PROXY_ASSET_PREFIX_PATTERN})[^'\"]+)(?P<suffix>['\"]\s*\)?)",
    re.IGNORECASE,
)
_VISUAL_ASSET_EXTENSIONS = {
    ".avif",
    ".css",
    ".gif",
    ".jpeg",
    ".jpg",
    ".js",
    ".mjs",
    ".mp4",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
}
_LIVE_PREVIEW_CLIENT_RUNTIME_MARKERS = (
    "app/entry.client",
    "@vite/client",
    "@react-refresh",
    "__x00__virtual:react-router",
    "virtual:react-router/hmr",
    "virtual:react-router/inject-hmr",
)
_LIVE_PREVIEW_CLIENT_RUNTIME_PATHS = {
    "app/entry.client.tsx",
    "@vite/client",
    "node_modules/vite/dist/client/env.mjs",
}
_LIVE_PREVIEW_CLIENT_MODULEPRELOAD_MARKERS = (
    "/app/",
    "/node_modules/",
    "/@id/",
    "/@vite/",
    "/@react-refresh",
    "/src/",
)


def _should_rewrite_live_preview_body(content_type):
    normalized = str(content_type or "").lower()
    return any(
        marker in normalized
        for marker in (
            "text/html",
            "application/xhtml+xml",
            "text/javascript",
            "application/javascript",
            "application/x-javascript",
            "text/css",
        )
    )


def _live_preview_proxy_asset_url(run_id, path):
    clean_path = str(path or "").lstrip("/")
    return f"{_backend_live_preview_proxy_prefix(run_id)}/{clean_path}"


def _live_preview_resource_url(run_id, url):
    return f"{_backend_live_preview_resource_prefix(run_id)}?{urlencode({'url': str(url or '')})}"


def _is_probable_visual_asset_url(url):
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    if any(path.endswith(extension) for extension in _VISUAL_ASSET_EXTENSIONS):
        return True
    host = str(parsed.netloc or "").lower()
    return any(marker in host for marker in ("firebasestorage.googleapis.com", "storage.googleapis.com", "cloudfront.net"))


def _is_live_preview_root_asset_path(value):
    text = str(value or "")
    path = urlsplit(text).path.lstrip("/")
    return any(path.startswith(prefix) for prefix in _LIVE_PREVIEW_PROXY_ASSET_PREFIXES)


def _rewrite_root_asset_reference(run_id, value):
    text = str(value or "")
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc or not text.startswith("/") or not _is_live_preview_root_asset_path(text):
        return text
    return _live_preview_proxy_asset_url(run_id, text.lstrip("/"))


def _rewrite_external_visual_reference(run_id, value):
    text = str(value or "")
    if _is_probable_visual_asset_url(text):
        return _live_preview_resource_url(run_id, text)
    return text


def _is_blocked_live_preview_resource_host(host):
    normalized = str(host or "").strip().lower().strip("[]")
    if not normalized or normalized in {"localhost", "0.0.0.0"} or normalized.endswith(".local"):
        return True
    try:
        addresses = [ipaddress.ip_address(normalized)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except Exception:
            return True
    return any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    )


def _is_allowed_live_preview_resource_url(url):
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    return not _is_blocked_live_preview_resource_host(parsed.hostname or "")


def _rewrite_srcset_reference(run_id, value):
    candidates = []
    for candidate in str(value or "").split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        pieces = candidate.split()
        if pieces:
            pieces[0] = _rewrite_external_visual_reference(run_id, _rewrite_root_asset_reference(run_id, pieces[0]))
        candidates.append(" ".join(pieces))
    return ", ".join(candidates)


def _rewrite_css_asset_references(run_id, text):
    def replace_css_url(match):
        quote_char = match.group("quote") or ""
        return (
            f"{match.group('prefix')}{quote_char}"
            f"{_live_preview_proxy_asset_url(run_id, match.group('path'))}"
            f"{quote_char}{match.group('suffix')}"
        )

    def replace_css_external_url(match):
        quote_char = match.group("quote") or ""
        return (
            f"{match.group('prefix')}{quote_char}"
            f"{_live_preview_resource_url(run_id, match.group('url'))}"
            f"{quote_char}{match.group('suffix')}"
        )

    def replace_css_import(match):
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_live_preview_proxy_asset_url(run_id, match.group('path'))}"
            f"{match.group('quote')}"
        )

    def replace_css_external_import(match):
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_live_preview_resource_url(run_id, match.group('url'))}"
            f"{match.group('quote')}"
        )

    rewritten = _LIVE_PREVIEW_CSS_URL_ASSET_RE.sub(replace_css_url, str(text or ""))
    rewritten = _LIVE_PREVIEW_CSS_IMPORT_ASSET_RE.sub(replace_css_import, rewritten)
    rewritten = _LIVE_PREVIEW_CSS_EXTERNAL_URL_RE.sub(replace_css_external_url, rewritten)
    rewritten = _LIVE_PREVIEW_CSS_EXTERNAL_IMPORT_RE.sub(replace_css_external_import, rewritten)
    return rewritten


def _has_live_preview_client_runtime_marker(value):
    normalized = str(value or "").lower()
    return any(marker in normalized for marker in _LIVE_PREVIEW_CLIENT_RUNTIME_MARKERS)


def _has_live_preview_client_modulepreload_marker(value):
    normalized = str(value or "").lower()
    return any(marker in normalized for marker in _LIVE_PREVIEW_CLIENT_MODULEPRELOAD_MARKERS)


def _is_live_preview_client_runtime_request_path(value):
    path = urlsplit(str(value or "")).path.lstrip("/").lower()
    if path in _LIVE_PREVIEW_CLIENT_RUNTIME_PATHS:
        return True
    return (
        path.startswith("@id/__x00__virtual:react-router/")
        or path.startswith("@react-refresh")
        or path.startswith("@vite/")
    )


def _empty_live_preview_client_runtime_response(include_body=True):
    body = b"export {};\n" if include_body else b""
    response = HttpResponse(body, status=200, content_type="text/javascript; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


def _strip_live_preview_client_runtime(text):
    def replace_script(match):
        tag = match.group(0)
        return "" if _has_live_preview_client_runtime_marker(tag) else tag

    def replace_link(match):
        tag = match.group(0)
        tag_lower = tag.lower()
        if "modulepreload" in tag_lower:
            return "" if _has_live_preview_client_modulepreload_marker(tag) else tag
        if "rel=\"preload\"" in tag_lower or "rel='preload'" in tag_lower or "rel=preload" in tag_lower:
            if "as=\"script\"" in tag_lower or "as='script'" in tag_lower or "as=script" in tag_lower:
                return "" if (
                    _has_live_preview_client_runtime_marker(tag)
                    or _has_live_preview_client_modulepreload_marker(tag)
                ) else tag
        return tag

    stripped = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", replace_script, str(text or ""))
    stripped = re.sub(r"(?is)<link\b[^>]*>", replace_link, stripped)
    return stripped


def _rewrite_live_preview_html_assets(run_id, text):
    soup = BeautifulSoup(str(text or ""), "html.parser")
    for base in soup.find_all("base"):
        base.decompose()
    for tag in soup.find_all(True):
        tag_name = str(tag.name or "").lower()
        for attr in ("src", "poster"):
            if tag.has_attr(attr):
                tag[attr] = _rewrite_external_visual_reference(run_id, _rewrite_root_asset_reference(run_id, tag.get(attr)))
        if tag.has_attr("action"):
            tag["action"] = _rewrite_root_asset_reference(run_id, tag.get("action"))
        if tag.has_attr("srcset"):
            tag["srcset"] = _rewrite_srcset_reference(run_id, tag.get("srcset"))
        if tag.has_attr("style"):
            tag["style"] = _rewrite_css_asset_references(run_id, str(tag.get("style") or ""))
        if tag.has_attr("href"):
            href = str(tag.get("href") or "")
            rel = " ".join(str(item).lower() for item in (tag.get("rel") or []))
            should_rewrite_href = (
                _is_live_preview_root_asset_path(href)
                or tag_name == "link"
                and any(token in rel for token in ("stylesheet", "preload", "icon", "apple-touch-icon"))
            )
            if should_rewrite_href:
                tag["href"] = _rewrite_external_visual_reference(run_id, _rewrite_root_asset_reference(run_id, href))
    return str(soup)


def _rewrite_live_preview_proxy_text(run_id, text, content_type=""):
    def replace_quoted(match):
        return f"{match.group('prefix')}{_live_preview_proxy_asset_url(run_id, match.group('path'))}"

    def replace_unquoted_attr(match):
        return f"{match.group('prefix')}{_live_preview_proxy_asset_url(run_id, match.group('path'))}"

    def replace_js_import(match):
        return f"{match.group('prefix')}{_live_preview_proxy_asset_url(run_id, match.group('path'))}{match.group('suffix')}"

    content_type_lower = str(content_type or "").lower()
    rewritten = _rewrite_css_asset_references(run_id, str(text or ""))
    rewritten = _LIVE_PREVIEW_QUOTED_ASSET_RE.sub(replace_quoted, rewritten)
    rewritten = _LIVE_PREVIEW_UNQUOTED_ATTR_ASSET_RE.sub(replace_unquoted_attr, rewritten)
    rewritten = _LIVE_PREVIEW_JS_IMPORT_ASSET_RE.sub(replace_js_import, rewritten)
    if "text/html" in content_type_lower or "application/xhtml+xml" in content_type_lower:
        rewritten = _rewrite_live_preview_html_assets(run_id, rewritten)
    return rewritten


def _rewrite_live_preview_proxy_body(run_id, body, content_type):
    if not body or not _should_rewrite_live_preview_body(content_type):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    rewritten = _rewrite_live_preview_proxy_text(run_id, text, content_type)
    if "text/html" in str(content_type or "").lower():
        rewritten = _strip_live_preview_client_runtime(rewritten)
    if rewritten == text:
        return body
    return rewritten.encode("utf-8")


LIVE_PREVIEW_ACTIVE_STATUSES = {"queued", "pending", "preparing", "starting", "building", "running"}
LIVE_PREVIEW_FAILURE_STATUSES = {"failed", "blocked", "expired", "cancelled", "canceled", "timeout", "timed_out"}


def _live_preview_statuses(payload):
    return {
        str(payload.get("status") or "").strip().lower(),
        str(payload.get("platformStatus") or payload.get("platform_status") or "").strip().lower(),
    } - {""}


def _live_preview_exact_ready(payload):
    preview_url = str(payload.get("previewUrl") or payload.get("preview_url") or "").strip()
    return bool(preview_url and (payload.get("exactRender") is True or payload.get("exact_render") is True))


def _live_preview_fallback_ready(payload):
    preview_url = str(payload.get("previewUrl") or payload.get("preview_url") or "").strip()
    if not preview_url or _live_preview_exact_ready(payload):
        return False
    proof = payload.get("proof") if isinstance(payload.get("proof"), dict) else {}
    preview_build_mode = str(
        payload.get("previewBuildMode")
        or payload.get("preview_build_mode")
        or proof.get("previewBuildMode")
        or proof.get("preview_build_mode")
        or ""
    ).strip()
    full_site_build_skipped = bool(
        payload.get("fullSiteBuildSkipped")
        or payload.get("full_site_build_skipped")
        or proof.get("fullSiteBuildSkipped")
        or proof.get("full_site_build_skipped")
    )
    render_confidence = str(payload.get("renderConfidence") or payload.get("render_confidence") or "").strip().lower()
    explicit_non_exact = payload.get("exactRender") is False or payload.get("exact_render") is False
    return (
        preview_build_mode == "route_scoped_next_preview"
        or full_site_build_skipped
        or render_confidence == "fallback"
        or explicit_non_exact
    )


def _persist_live_preview_payload(run, payload):
    if isinstance(payload, dict) and payload:
        payload = _normalize_live_preview_payload(payload)
        payload = _rewrite_live_preview_payload_for_browser(run.run_id, payload)
        result = dict(run.result or {})
        result["livePreview"] = payload
        update_fields = ["result", "updated_at"]

        if run.workflow == "article_system_setup":
            preview_statuses = _live_preview_statuses(payload)
            preview_url = str(payload.get("previewUrl") or payload.get("preview_url") or "").strip()
            failed = bool(preview_statuses.intersection(LIVE_PREVIEW_FAILURE_STATUSES) or payload.get("error"))
            ready = _live_preview_exact_ready(payload)
            fallback_ready = _live_preview_fallback_ready(payload)
            active = bool(preview_statuses.intersection(LIVE_PREVIEW_ACTIVE_STATUSES))
            setup_payload = dict(result.get("article_system_setup") or {})

            if failed:
                error_code = payload.get("errorCode") or payload.get("error_code") or ""
                result["status"] = "preview_failed"
                result["preview_url"] = ""
                result.pop("approve_url", None)
                result.pop("deny_url", None)
                result["error"] = payload.get("error") or "Articles setup preview could not be prepared."
                result["error_code"] = error_code
                setup_payload["status"] = "preview_failed"
                setup_payload["error"] = result["error"]
                setup_payload["error_code"] = error_code
                setup_payload.pop("approve_url", None)
                setup_payload.pop("deny_url", None)
                setup_payload["retryable"] = payload.get("retryable", True)
                result["article_system_setup"] = setup_payload
                run.status = ContentFactoryRunStatus.BLOCKED
                run.current_step = "preview_failed"
                run.approval_state = ContentFactoryApprovalState.NOT_REQUIRED
                run.error = result["error"]
                update_fields.extend(["status", "current_step", "approval_state", "error"])
            elif ready:
                result["status"] = "preview_ready"
                result["preview_url"] = preview_url
                result.pop("fallback_preview_url", None)
                result.pop("error", None)
                setup_payload["status"] = "preview_ready"
                setup_payload["preview_url"] = preview_url
                setup_payload.pop("fallback_preview_url", None)
                setup_payload.pop("error", None)
                result["article_system_setup"] = setup_payload
                run.status = ContentFactoryRunStatus.AWAITING_APPROVAL
                run.current_step = "await_review"
                run.approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
                run.error = ""
                update_fields.extend(["status", "current_step", "approval_state", "error"])
            elif fallback_ready:
                warning = "Exact articles setup preview is unavailable; the hosted URL is a route-scoped fallback and cannot be approved."
                result["status"] = "fallback_ready"
                result["preview_url"] = ""
                result.pop("approve_url", None)
                result.pop("deny_url", None)
                result["fallback_preview_url"] = preview_url
                result["error"] = warning
                result["error_code"] = "article_system_setup_preview_not_exact"
                setup_payload["status"] = "fallback_ready"
                setup_payload["preview_url"] = ""
                setup_payload.pop("approve_url", None)
                setup_payload.pop("deny_url", None)
                setup_payload["fallback_preview_url"] = preview_url
                setup_payload["error"] = warning
                setup_payload["error_code"] = "article_system_setup_preview_not_exact"
                setup_payload["retryable"] = payload.get("retryable", True)
                result["article_system_setup"] = setup_payload
                run.status = ContentFactoryRunStatus.BLOCKED
                run.current_step = "fallback_ready"
                run.approval_state = ContentFactoryApprovalState.NOT_REQUIRED
                run.error = warning
                update_fields.extend(["status", "current_step", "approval_state", "error"])
            elif active:
                result["status"] = "preview_building"
                result["preview_url"] = ""
                result.pop("fallback_preview_url", None)
                result.pop("approve_url", None)
                result.pop("deny_url", None)
                result.pop("error", None)
                setup_payload["status"] = "preview_building"
                setup_payload["preview_url"] = ""
                setup_payload.pop("fallback_preview_url", None)
                setup_payload.pop("approve_url", None)
                setup_payload.pop("deny_url", None)
                setup_payload.pop("error", None)
                result["article_system_setup"] = setup_payload
                run.status = ContentFactoryRunStatus.RUNNING
                run.current_step = "start_hosted_preview"
                run.approval_state = ContentFactoryApprovalState.NOT_REQUIRED
                run.error = ""
                update_fields.extend(["status", "current_step", "approval_state", "error"])
        run.result = result
        run.save(update_fields=update_fields)
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


def _github_app_token_payload_for_domain(*, domain: str, github_repo: str, permission_mode: str = "write") -> dict:
    normalized_domain = normalize_company_domain(domain or "")
    repo = str(github_repo or "").strip()
    if not normalized_domain or not repo or not github_app_credentials_configured():
        return {}
    try:
        organization = Organization.objects.get(domain=normalized_domain)
        config = _get_config(organization)
    except Organization.DoesNotExist:
        return {}
    installation_id = str(config.github_installation_id or "").strip()
    if not installation_id:
        return {}
    token = create_installation_access_token(
        installation_id=installation_id,
        repository=repo,
        permission_mode=permission_mode,
    )
    return token.as_content_factory_payload(domain=normalized_domain)


def _github_token_for_repo_operation(*, domain: str, github_repo: str, permission_mode: str = "write") -> tuple[str, str]:
    try:
        payload = _github_app_token_payload_for_domain(
            domain=domain,
            github_repo=github_repo,
            permission_mode=permission_mode,
        )
        token = str(payload.get("github_token") or "").strip()
        if token:
            return token, str(payload.get("token_source") or "github_app_installation")
    except GitHubAppTokenError as exc:
        logger.warning(
            "github_app_installation_token_unavailable_falling_back domain=%s github_repo=%s error=%s",
            domain,
            github_repo,
            exc,
        )

    token = ensure_valid_org_token(domain)
    return token, "github_oauth_user_token"


def _live_preview_github_token_payload(run):
    domain = normalize_company_domain(getattr(run, "domain", "") or "")
    github_repo = str(getattr(run, "github_repo", "") or "").strip()
    if not domain or not github_repo:
        return {}
    try:
        token_payload = _github_app_token_payload_for_domain(
            domain=domain,
            github_repo=github_repo,
            permission_mode="write",
        )
        github_token = str(token_payload.get("github_token") or "").strip()
        if github_token:
            return {
                "github_token": github_token,
                "github_installation_id": token_payload.get("github_installation_id"),
                "token_source": token_payload.get("token_source") or "github_app_installation",
            }
    except GitHubAppTokenError as exc:
        logger.warning(
            "content_factory_live_preview_github_app_token_unavailable_falling_back run_id=%s domain=%s github_repo=%s error=%s",
            getattr(run, "run_id", ""),
            domain,
            github_repo,
            exc,
        )

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
    return {"github_token": github_token, "token_source": "github_oauth_user_token"} if github_token else {}


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
        "context": comment.context or None,
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


def _clean_comment_context_string(value, max_length):
    text = str(value or "").strip()
    return text[:max_length] if text else ""


def _clean_comment_context_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _clean_comment_context_number_map(value, allowed_keys):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in allowed_keys:
        number = _clean_comment_context_number(value.get(key))
        if number is not None:
            result[key] = number
    return result


def _comment_context_from_request(data):
    getter = data.get if hasattr(data, "get") else (lambda _key, default=None: default)
    context = getter("context")
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except json.JSONDecodeError:
            context = None
    if not isinstance(context, dict):
        return {}
    normalized = {}
    for key, max_length in {
        "domPath": 1200,
        "textHash": 120,
        "textExcerpt": 1200,
        "pageUrl": 1200,
        "previewMode": 120,
    }.items():
        value = _clean_comment_context_string(context.get(key), max_length)
        if value:
            normalized[key] = value
    rect = _clean_comment_context_number_map(context.get("rect"), ["left", "top", "right", "bottom", "width", "height"])
    click = _clean_comment_context_number_map(context.get("click"), ["x", "y", "pageX", "pageY"])
    viewport = _clean_comment_context_number_map(
        context.get("viewport"),
        ["width", "height", "scrollX", "scrollY", "devicePixelRatio"],
    )
    if rect:
        normalized["rect"] = rect
    if click:
        normalized["click"] = click
    if viewport:
        normalized["viewport"] = viewport
    return normalized


def _request_includes_comment_context(data):
    getter = data.get if hasattr(data, "get") else (lambda _key, default=None: default)
    return getter("context") not in (None, "")


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
        "context": _comment_context_from_request(data),
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
        "context": comment.context or {},
        "body": comment.body,
    }


def _article_system_remote_comment_payload(comment):
    context = comment.context if isinstance(comment.context, dict) else {}
    file_path = str(
        context.get("filePath")
        or context.get("file_path")
        or context.get("sourceFilePath")
        or context.get("source_file_path")
        or ""
    ).strip()
    return {
        "comment_id": str(comment.id),
        "file_path": file_path,
        "selector": comment.selector or _selector_for_component(comment.component_id),
        "anchor": comment.anchor or {},
        "context": context,
        "body": comment.body,
    }


def _article_system_remote_comment_from_request(data):
    if not isinstance(data, dict):
        return None
    body = str(_request_value(data, "body", "comment", default="") or "").strip()
    if not body:
        return None
    comment_id = str(_request_value(data, "commentId", "comment_id", "id", default="") or "").strip()
    component_id = str(_request_value(data, "componentId", "component_id", default="article-system-setup") or "").strip()
    return {
        "comment_id": comment_id or f"comment-{uuid.uuid4().hex[:12]}",
        "file_path": str(_request_value(data, "filePath", "file_path", default="") or "").strip(),
        "selector": str(_request_value(data, "selector", default="") or "").strip() or _selector_for_component(component_id),
        "anchor": _comment_anchor_from_request(data),
        "context": _comment_context_from_request(data),
        "body": body,
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
                    "context": comment.context or {},
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
                    "context": comment.context or {},
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
                        "context": comment.context or {},
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


def _publish_evidence_from_run(run, *, compact=False):
    if not run:
        return {}
    result = run.result or {}
    diagnostics = result.get("diagnostics") or run.verification_summary or {}
    evidence = {
        "runId": run.run_id,
        "status": run.status,
        "approvalState": run.approval_state,
        "previewUrl": result.get("preview_url") or result.get("article_url") or result.get("url"),
        "prUrl": result.get("pr_url") or result.get("pull_request_url") or result.get("draft_pr_url"),
        "prNumber": result.get("pr_number") or result.get("pull_request_number") or result.get("draft_pr_number"),
        "mergeStatus": result.get("merge_status"),
        "checksStatus": result.get("checks_status"),
        "routePath": result.get("route_path") or result.get("path"),
        "screenshots": result.get("screenshots") or diagnostics.get("screenshots") or [],
        "changedFiles": result.get("changed_files") or result.get("files") or diagnostics.get("changed_files") or [],
        "warnings": result.get("warnings") or run.acceptance_summary.get("warnings") or [],
        "diagnostics": diagnostics,
        "contentPackage": _content_package_from_run(run),
    }
    if compact:
        return {
            key: evidence.get(key)
            for key in (
                "runId",
                "status",
                "approvalState",
                "previewUrl",
                "prUrl",
                "prNumber",
                "mergeStatus",
                "checksStatus",
                "routePath",
                "warnings",
                "contentPackage",
            )
        }
    return evidence


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


def _run_has_publish_pr_or_preview_evidence(run):
    result = _run_mapping(run.result)
    evidence = _publish_evidence_from_run(run)
    live_preview = _live_preview_from_run(run)
    return bool(
        evidence.get("prUrl")
        or evidence.get("previewUrl")
        or result.get("pr_url")
        or result.get("pull_request_url")
        or result.get("draft_pr_url")
        or result.get("preview_url")
        or result.get("article_url")
        or result.get("url")
        or live_preview.get("previewUrl")
    )


def _payload_has_publish_approval_gate(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("approve_url") or payload.get("deny_url") or payload.get("requested_action"):
        return True
    if str(payload.get("approval_state") or payload.get("approvalState") or "").strip().lower() == ContentFactoryApprovalState.APPROVAL_REQUIRED:
        return True
    if str(payload.get("status") or "").strip().lower() in {
        ContentFactoryRunStatus.AWAITING_APPROVAL,
        ContentFactoryRunStatus.APPROVAL_REQUIRED,
    }:
        return True
    for nested_key in ("result", "article_system_setup", "approval", "publish_approval"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict) and _payload_has_publish_approval_gate(nested):
            return True
    return False


def _publish_child_has_real_approval_gate(run):
    if not run:
        return False
    if run.status in {ContentFactoryRunStatus.AWAITING_APPROVAL, ContentFactoryRunStatus.APPROVAL_REQUIRED}:
        return True
    if run.approval_state == ContentFactoryApprovalState.APPROVAL_REQUIRED:
        return True
    return _payload_has_publish_approval_gate(_run_mapping(run.result))


PUBLISH_CHILD_MISSING_REMOTE_WAIT_REASON = (
    "Publish job was queued but did not start. Retry will safely recreate the same PR job."
)


def _payload_status_code(payload):
    payload = _run_mapping(payload)
    diagnostics = _run_mapping(payload.get("diagnostics"))
    raw_status_code = payload.get("content_factory_status_code") or diagnostics.get("content_factory_status_code")
    try:
        return int(raw_status_code)
    except (TypeError, ValueError):
        return None


def _payload_mentions_missing_content_factory_run(payload, *, run=None):
    payload = _run_mapping(payload)
    diagnostics = _run_mapping(payload.get("diagnostics"))
    text = " ".join(
        str(value or "")
        for value in (
            payload.get("error"),
            payload.get("message"),
            diagnostics.get("technical_error"),
            getattr(run, "error", ""),
        )
    ).lower()
    return "content factory run" in text and "was not found" in text


def _remote_status_payload_missing_run(payload):
    return _payload_status_code(payload) == 404 and _payload_mentions_missing_content_factory_run(payload)


def _publish_child_missing_remote(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return False
    if _run_has_publish_pr_or_preview_evidence(run) or _publish_child_has_real_approval_gate(run):
        return False
    result = _run_mapping(run.result)
    return (
        _payload_status_code(result) == 404
        and _payload_mentions_missing_content_factory_run(result, run=run)
    )


def _publish_child_handoff_status(run, *, recoverable=False):
    if _publish_child_missing_remote(run):
        return "recoverable_missing_child"
    if recoverable:
        return "recoverable_wait"
    return "queued" if run and run.status in RUNNING_RUN_STATUSES else (run.status if run else "")


def _publish_child_run_recoverable(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return False
    if _publish_child_missing_remote(run):
        return True
    if run.status != ContentFactoryRunStatus.AWAITING_CONFIRMATION:
        return False
    if _run_has_publish_pr_or_preview_evidence(run) or _publish_child_has_real_approval_gate(run):
        return False
    return True


def _publish_child_wait_reason(run):
    if _publish_child_missing_remote(run):
        return PUBLISH_CHILD_MISSING_REMOTE_WAIT_REASON
    if _publish_child_run_recoverable(run):
        return "Publish child is waiting for confirmation instead of creating a PR."
    if _publish_child_has_real_approval_gate(run):
        return "Publish child is waiting for approval."
    return ""


def _publish_source_run_for_child(run, context):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return None
    source_run_id = _run_source_run_id(run)
    if not source_run_id:
        return None
    source_run = ContentFactoryRun.objects.filter(run_id=source_run_id).prefetch_related("steps").first()
    if not source_run or source_run.workflow not in ARTICLE_WORKFLOWS or not _run_belongs_to_context(source_run, context):
        return None
    return source_run


def _publish_child_for_run_state(run, *, context=None):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return None
    if run.workflow != "article_revision" and _publish_source_run_for_child(run, context) is not None:
        return run
    return _local_publish_child_for_run(run, context=context)


def _annotate_publish_child_state(run, *, context=None):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return run
    child = _publish_child_for_run_state(run, context=context)
    if not child:
        return run
    recoverable = _publish_child_run_recoverable(child)
    wait_reason = _publish_child_wait_reason(child)
    result = dict(run.result or {})
    updates = {
        "publish_child_run_id": child.run_id,
        "promoted_publish_job_id": child.run_id,
        "publish_child_status": child.status,
        "publish_child_recoverable": recoverable,
        "publish_child_wait_reason": wait_reason,
        "publish_handoff_status": _publish_child_handoff_status(child, recoverable=recoverable),
        "publish_handoff_pending": False,
    }
    changed = False
    for key, value in updates.items():
        if result.get(key) != value:
            result[key] = value
            changed = True
    if changed:
        run.result = result
        run.save(update_fields=["result", "updated_at"])
    return run


def _pull_request_number_from_run(run):
    result = _run_mapping(run.result)
    for key in ("pr_number", "pull_request_number", "draft_pr_number"):
        value = result.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    pr_url = str(
        result.get("pr_url")
        or result.get("pull_request_url")
        or result.get("draft_pr_url")
        or _publish_evidence_from_run(run).get("prUrl")
        or ""
    ).strip()
    match = re.search(r"/pull/(\d+)", pr_url)
    if match:
        return int(match.group(1))
    return None


def _github_api_request(method, path, *, token, body=None, expected=(200,)):
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = http_client.request(method, url, headers=headers, json=body, timeout=(3, 20))
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    if response.status_code not in expected:
        detail = payload.get("message") if isinstance(payload, dict) else ""
        raise ValueError(detail or f"GitHub returned {response.status_code}.")
    return payload


def _github_pull_checks_state(*, repo, pr_number, token):
    pull = _github_api_request("GET", f"/repos/{repo}/pulls/{pr_number}", token=token)
    if pull.get("merged"):
        return pull, {"state": "merged", "ready": False, "message": "Pull request is already merged."}
    if pull.get("state") != "open":
        return pull, {"state": "closed", "ready": False, "message": "Pull request is not open."}
    head_sha = ((pull.get("head") or {}).get("sha") or "").strip()
    if not head_sha:
        return pull, {"state": "unknown", "ready": False, "message": "Pull request head SHA is unavailable."}

    combined = _github_api_request("GET", f"/repos/{repo}/commits/{head_sha}/status", token=token)
    check_runs = _github_api_request("GET", f"/repos/{repo}/commits/{head_sha}/check-runs", token=token)
    status_count = int(combined.get("total_count") or 0)
    status_state = str(combined.get("state") or "").lower()
    runs = check_runs.get("check_runs") if isinstance(check_runs.get("check_runs"), list) else []
    incomplete_runs = [item for item in runs if item.get("status") != "completed"]
    failed_runs = [
        item
        for item in runs
        if item.get("status") == "completed"
        and item.get("conclusion") not in {"success", "neutral", "skipped"}
    ]
    if status_count and status_state != "success":
        return pull, {"state": status_state or "pending", "ready": False, "message": "Commit status checks have not passed."}
    if incomplete_runs:
        return pull, {"state": "pending", "ready": False, "message": "GitHub Actions checks are still running."}
    if failed_runs:
        return pull, {"state": "failed", "ready": False, "message": "One or more GitHub Actions checks failed."}
    return pull, {"state": "success", "ready": True, "message": "Checks are passing."}


def _merge_publish_pr_for_run(*, run, context):
    repo = run.github_repo or _get_config(context.organization).github_repo
    if not repo:
        return None, Response({"detail": "No GitHub repository is configured for this publish run."}, status=status.HTTP_400_BAD_REQUEST)
    pr_number = _pull_request_number_from_run(run)
    if not pr_number:
        return None, Response({"detail": "No publish pull request was found for this run."}, status=status.HTTP_409_CONFLICT)
    try:
        token, token_source = _github_token_for_repo_operation(
            domain=context.organization.domain,
            github_repo=repo,
            permission_mode="write",
        )
        logger.info(
            "vibe_marketing_publish_merge_token_source run_id=%s repo=%s token_source=%s",
            run.run_id,
            repo,
            token_source,
        )
        pull, checks = _github_pull_checks_state(repo=repo, pr_number=pr_number, token=token)
        if pull.get("merged"):
            result = run.result or {}
            result["merge_status"] = "merged"
            result["checks_status"] = "merged"
            run.result = result
            run.save(update_fields=["result", "updated_at"])
            return run, None
        if not checks.get("ready"):
            result = run.result or {}
            result["merge_status"] = str(checks.get("state") or "blocked")
            result["checks_status"] = str(checks.get("state") or "blocked")
            result["merge_blocked_reason"] = checks.get("message")
            run.result = result
            run.save(update_fields=["result", "updated_at"])
            return None, Response(
                {"detail": checks.get("message") or "Publish PR checks are not ready.", "checks": checks},
                status=status.HTTP_409_CONFLICT,
            )
        merge_payload = {
            "commit_title": f"Publish Content Factory article from {run.run_id}",
            "merge_method": "squash",
        }
        merged = _github_api_request(
            "PUT",
            f"/repos/{repo}/pulls/{pr_number}/merge",
            token=token,
            body=merge_payload,
            expected=(200, 201),
        )
    except (ArticleGenerationError, TokenRefreshError, ValueError, http_client.RequestException) as exc:
        return None, Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

    result = run.result or {}
    result["merge_status"] = "merged"
    result["checks_status"] = "success"
    result["merged_at"] = timezone.now().isoformat()
    result["merge_response"] = merged
    run.result = result
    run.status = ContentFactoryRunStatus.COMPLETED
    run.save(update_fields=["status", "result", "updated_at"])
    return run, None


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


def _cancel_local_scan_run(*, run, remote_data=None):
    remote_data = remote_data if isinstance(remote_data, dict) else {}
    now = timezone.now()
    result = dict(_run_mapping(run.result))
    nested = dict(_run_mapping(result.get("result")))
    nested.update(
        {
            "requested_action": None,
            "scaffold_status": "cancelled",
            "approve_url": None,
            "deny_url": None,
        }
    )
    setup = nested.get("article_system_setup")
    if isinstance(setup, dict):
        setup = dict(setup)
        setup.update(
            {
                "status": "cancelled",
                "requested_action": None,
                "approve_url": None,
                "deny_url": None,
            }
        )
        nested["article_system_setup"] = setup

    result.update(
        {
            "status": ContentFactoryRunStatus.CANCELLED,
            "cancelled": True,
            "cancelled_at": now.isoformat(),
            "requested_action": None,
            "scaffold_status": "cancelled",
            "approve_url": None,
            "deny_url": None,
            "remote_cancel": remote_data,
            "result": nested,
        }
    )
    setup = result.get("article_system_setup")
    if isinstance(setup, dict):
        setup = dict(setup)
        setup.update(
            {
                "status": "cancelled",
                "requested_action": None,
                "approve_url": None,
                "deny_url": None,
            }
        )
        result["article_system_setup"] = setup

    with transaction.atomic():
        locked_run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        locked_run.steps.exclude(status=ContentFactoryStepStatus.COMPLETED).update(
            status=ContentFactoryStepStatus.CANCELLED,
            message="Cancelled by user.",
            error="",
        )
        locked_run.status = ContentFactoryRunStatus.CANCELLED
        locked_run.current_step = "cancelled"
        locked_run.approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        locked_run.resume_available = False
        locked_run.error = ""
        locked_run.result = result
        locked_run.save(
            update_fields=[
                "status",
                "current_step",
                "approval_state",
                "resume_available",
                "error",
                "result",
                "updated_at",
            ]
        )
        return locked_run


def _scan_run_is_stale_retryable(run):
    result = run.result or {}
    return bool(
        run.workflow in SCAN_WORKFLOWS
        and run.status in {ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.BLOCKED}
        and (result.get("stale") or result.get("retry_available") or result.get("stale_reason") == "scan_queue_not_started")
    )


def _supersede_stale_scan_runs(*, context, request_user):
    stale_runs = list(
        ContentFactoryRun.objects.filter(
            domain=context.organization.domain,
            workflow__in=SCAN_WORKFLOWS,
            status__in=[ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.BLOCKED],
        ).order_by("-updated_at")[:5]
    )
    superseded = []
    for stale_run in stale_runs:
        if not _scan_run_is_stale_retryable(stale_run):
            continue
        payload = {
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "slack_user_id": founder_actor_id_for_user(request_user),
            "superseded_by_new_scan": True,
        }
        remote_data = _call_content_factory_run_action(
            run_id=stale_run.run_id,
            action="cancel",
            payload=payload,
            workflow=stale_run.workflow,
            timeout=(2, 5),
            transport_errors_are_pending=True,
        )
        _cancel_local_scan_run(run=stale_run, remote_data=remote_data)
        superseded.append(stale_run.run_id)
    return superseded


def _restart_article_payload_from_run(*, run, context, config, actor_id):
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    package = _content_package_from_run(run) or {}
    title, keyword = _article_draft_title_keyword(run)
    topic = str(
        run_request.get("topic")
        or run_request.get("custom_title")
        or run_request.get("customTitle")
        or run_request.get("selected_title")
        or run_request.get("selectedTitle")
        or title
        or keyword
        or ""
    ).strip()
    target_keyword = str(
        run_request.get("target_keyword")
        or run_request.get("targetKeyword")
        or package.get("targetKeyword")
        or result.get("target_keyword")
        or result.get("targetKeyword")
        or keyword
        or topic
        or ""
    ).strip()
    if not topic and target_keyword:
        topic = target_keyword
    if not topic and not target_keyword:
        return None

    requested_delivery_mode = str(
        run_request.get("delivery_mode")
        or run_request.get("deliveryMode")
        or result.get("delivery_mode")
        or result.get("deliveryMode")
        or ""
    ).strip()
    delivery_mode_explicit = _bool_from_request(
        run_request.get("delivery_mode_explicit")
        if "delivery_mode_explicit" in run_request
        else run_request.get("deliveryModeExplicit")
    )
    delivery_mode = _effective_article_delivery_mode(
        config,
        requested_mode=requested_delivery_mode or None,
        explicit=delivery_mode_explicit,
    )
    return {
        "domain": context.organization.domain,
        "slack_user_id": actor_id,
        "topic": topic,
        "target_keyword": target_keyword or topic,
        "context": str(run_request.get("context") or result.get("context") or ""),
        "github_repo": config.github_repo or run.github_repo or run_request.get("github_repo") or run_request.get("githubRepo") or "",
        "delivery_mode": delivery_mode,
        "delivery_mode_confirmed": _bool_from_request(
            run_request.get("delivery_mode_confirmed", run_request.get("deliveryModeConfirmed", True))
        ),
        "delivery_mode_explicit": delivery_mode_explicit,
        "source_run_id": run_request.get("source_run_id") or run_request.get("sourceRunId") or "",
        "custom_title": run_request.get("custom_title") or run_request.get("customTitle") or title or None,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        "restart_source_run_id": run.run_id,
    }


def _restart_article_run(*, run, context):
    if run.workflow not in RESTARTABLE_ARTICLE_WORKFLOWS:
        return None, Response(
            {"detail": "Only article generation drafts can be restarted from the marketing dashboard."},
            status=status.HTTP_409_CONFLICT,
        )
    if run.status not in {ContentFactoryRunStatus.FAILED, ContentFactoryRunStatus.BLOCKED, ContentFactoryRunStatus.DENIED}:
        return None, Response(
            {"detail": "Only failed or blocked article drafts can be restarted."},
            status=status.HTTP_409_CONFLICT,
        )
    if run.resume_available:
        return None, Response(
            {"detail": "This draft has a resumable step. Resume it instead of starting a replacement run."},
            status=status.HTTP_409_CONFLICT,
        )

    config = _get_config(context.organization)
    actor_id = founder_actor_id_for_user(context.profile.user)
    payload = _restart_article_payload_from_run(run=run, context=context, config=config, actor_id=actor_id)
    if not payload:
        return None, Response(
            {"detail": "This draft does not have enough stored request data to restart automatically."},
            status=status.HTTP_409_CONFLICT,
        )
    existing_article = _topic_is_already_written(
        context.organization,
        keyword=payload["target_keyword"],
        title=payload["topic"],
    )
    if existing_article:
        return None, Response(
            {
                "detail": "This topic has already been written. Open the published article instead of restarting the draft.",
                "writtenArticle": _serialize_written_article(existing_article),
            },
            status=status.HTTP_409_CONFLICT,
        )

    restarted_run = _queue_content_factory_run(
        endpoint="article",
        workflow="article_generation",
        context=context,
        config=config,
        payload=payload,
    )
    result = run.result or {}
    result["restart_child_run_id"] = restarted_run.run_id
    result["restart_requested_at"] = timezone.now().isoformat()
    run.result = result
    run.save(update_fields=["result", "updated_at"])
    if restarted_run.status not in FAILED_RUN_STATUSES:
        _mark_keyword_in_progress(context.organization, payload["target_keyword"])
    return restarted_run, None


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


def _compact_baseline_metric(metric):
    if not isinstance(metric, dict):
        return metric
    return {
        key: value
        for key, value in metric.items()
        if key in {"status", "score", "message", "verified", "last28Days", "last90Days"}
    }


def _serialize_baseline_snapshot(snapshot, config=None, *, compact=False):
    if not snapshot:
        return {
            "status": "missing",
            "passed": bool(config and config.baseline_skipped_at),
            "skipped": bool(config and config.baseline_skipped_at),
            "skippedAt": config.baseline_skipped_at.isoformat() if config and config.baseline_skipped_at else None,
            "skipReason": config.baseline_skip_reason if config else "",
        }
    metrics = snapshot.metrics
    recommendations = snapshot.recommendations
    if compact:
        metrics = {
            key: _compact_baseline_metric(value)
            for key, value in (snapshot.metrics or {}).items()
            if key in {"technical", "content", "authority", "traffic"}
        }
        recommendations = list(snapshot.recommendations or [])[:3]
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
        "metrics": metrics,
        "sourceStatus": snapshot.source_status,
        "recommendations": recommendations,
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
            "shortDescription": "",
            "problemSolved": "",
            "targetAudience": "",
            "founderNames": [],
            "stage": "",
            "organizationKind": "",
            "notes": "",
            "companyAliases": [],
            "domainAliases": [],
        }
    return {
        "shortDescription": profile.short_description,
        "problemSolved": profile.problem_solved,
        "targetAudience": profile.target_audience,
        "founderNames": list(profile.founder_names or []),
        "stage": profile.stage,
        "organizationKind": getattr(profile, "organization_kind", ""),
        "notes": profile.notes,
        "companyAliases": list(profile.company_aliases or []),
        "domainAliases": list(profile.domain_aliases or []),
        "competitorDomains": list(profile.competitor_domains or []),
        "positiveKeywords": list(profile.positive_keywords or []),
    }


def _autofill_startup_profile_payload(organization):
    try:
        profile = organization.startup_profile
    except Exception:
        return {
            "short_description": "",
            "problem_solved": "",
            "target_audience": "",
            "founder_names": [],
            "stage": "",
            "organization_kind": "",
            "notes": "",
        }
    return {
        "short_description": profile.short_description,
        "problem_solved": profile.problem_solved,
        "target_audience": profile.target_audience,
        "founder_names": list(profile.founder_names or []),
        "stage": profile.stage,
        "organization_kind": getattr(profile, "organization_kind", ""),
        "notes": profile.notes,
    }


def _autofill_profile_fields_payload(startup_profile):
    return {
        "shortDescription": str(startup_profile.get("short_description") or ""),
        "problemSolved": str(startup_profile.get("problem_solved") or ""),
        "targetAudience": str(startup_profile.get("target_audience") or ""),
        "location": "",
        "founderNames": list(startup_profile.get("founder_names") or []),
        "stage": str(startup_profile.get("stage") or ""),
        "organizationKind": str(startup_profile.get("organization_kind") or ""),
        "abn": "",
    }


def _run_mapping(value):
    return value if isinstance(value, dict) else {}


PUBLISH_HANDOFF_STALE_AFTER = timedelta(seconds=60)


def _deterministic_publish_child_run_id(source_run_id):
    source_run_id = str(source_run_id or "").strip()
    if not source_run_id:
        return ""
    return f"publish-{uuid.uuid5(uuid.NAMESPACE_URL, f'content-factory:publish-child:{source_run_id}')}"


def _parse_handoff_timestamp(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


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
    explicit_child_run_id = str(
        _run_mapping(article_run.result).get("publish_child_run_id")
        or _run_mapping(article_run.result).get("promoted_publish_job_id")
        or ""
    ).strip()
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


def _local_publish_child_for_run(run, *, context=None):
    if not run:
        return None
    explicit_child_run_id = _publish_child_run_id_for_run(run)
    if not explicit_child_run_id:
        return None
    child = ContentFactoryRun.objects.filter(run_id=explicit_child_run_id).prefetch_related("steps").first()
    if not child:
        return None
    if context is not None and not _run_belongs_to_context(child, context):
        return None
    return child


def _publish_child_run_id_for_run(run):
    if not run:
        return ""
    result = _run_mapping(run.result)
    return str(
        result.get("publish_child_run_id")
        or result.get("promoted_publish_job_id")
        or ""
    ).strip()


def _mark_publish_handoff_pending(*, run, remote_run=None, action="promote-bundle", remote_data=None):
    timestamp = timezone.now().isoformat()
    updated = []
    for candidate in (run, remote_run):
        if candidate is None or any(existing.pk == candidate.pk for existing in updated):
            continue
        result = dict(candidate.result or {})
        result["publish_handoff_pending"] = True
        result["publish_handoff_status"] = "pending"
        result["publish_handoff_action"] = action
        result.setdefault("publish_handoff_started_at", timestamp)
        result["publish_handoff_last_attempt_at"] = timestamp
        result["publish_handoff_stale"] = False
        result["promote_bundle_requested_at"] = timestamp
        if remote_data is not None:
            result["latest_control_response"] = remote_data
        candidate.result = result
        candidate.save(update_fields=["result", "updated_at"])
        updated.append(candidate)
    return timestamp


def _publish_handoff_pending_for_run(run):
    if not run:
        return False
    result = _run_mapping(run.result)
    return bool(result.get("publish_handoff_pending")) and not _publish_child_run_id_for_run(run)


def _publish_handoff_stale_from_result(result, *, now=None):
    result = _run_mapping(result)
    timestamp = (
        _parse_handoff_timestamp(result.get("publish_handoff_last_attempt_at"))
        or _parse_handoff_timestamp(result.get("promote_bundle_requested_at"))
        or _parse_handoff_timestamp(result.get("publish_handoff_started_at"))
    )
    if timestamp is None:
        return True
    return (now or timezone.now()) - timestamp >= PUBLISH_HANDOFF_STALE_AFTER


def _publish_handoff_stale_for_run(run, *, now=None):
    if not _publish_handoff_pending_for_run(run):
        return False
    return _publish_handoff_stale_from_result(run.result, now=now)


def _publish_handoff_fresh_for_known_child(run, *, now=None):
    if not run:
        return False
    result = _run_mapping(run.result)
    return bool(result.get("publish_handoff_pending")) and not _publish_handoff_stale_from_result(result, now=now)


def _annotate_publish_handoff_staleness(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return run
    stale = _publish_handoff_stale_for_run(run)
    result = dict(run.result or {})
    if bool(result.get("publish_handoff_stale")) == stale:
        return run
    result["publish_handoff_stale"] = stale
    if stale:
        result["publish_handoff_status"] = "stale"
    elif result.get("publish_handoff_status") == "stale":
        result["publish_handoff_status"] = "pending"
    run.result = result
    run.save(update_fields=["result", "updated_at"])
    return run


def _attach_publish_child_to_run(run, child_run_id):
    child_run_id = str(child_run_id or "").strip()
    if not run or not child_run_id:
        return run
    result = dict(run.result or {})
    result["publish_child_run_id"] = child_run_id
    result["promoted_publish_job_id"] = child_run_id
    result["publish_handoff_pending"] = False
    result["publish_handoff_stale"] = False
    result["publish_handoff_status"] = "queued"
    run.result = result
    run.save(update_fields=["result", "updated_at"])
    return run


def _mark_publish_child_missing_remote_on_run(run, child_run_id):
    child_run_id = str(child_run_id or "").strip()
    if not run or not child_run_id:
        return run
    result = dict(run.result or {})
    result["publish_child_run_id"] = child_run_id
    result["promoted_publish_job_id"] = child_run_id
    result["publish_child_status"] = "missing"
    result["publish_child_recoverable"] = True
    result["publish_child_wait_reason"] = PUBLISH_CHILD_MISSING_REMOTE_WAIT_REASON
    result["publish_handoff_status"] = "recoverable_missing_child"
    result["publish_handoff_pending"] = False
    result["publish_handoff_stale"] = True
    run.result = result
    run.save(update_fields=["result", "updated_at"])
    return run


def _sync_confirmed_remote_publish_child(
    *,
    child_run_id,
    source_run,
    request,
    context,
    remote_data,
    payload=None,
    review_source_run_id="",
):
    remote_data = dict(remote_data or {})
    remote_data.setdefault("run_id", child_run_id)
    remote_data.setdefault("source_run_id", source_run.run_id)
    return _ensure_local_publish_child_from_known_id(
        child_run_id=child_run_id,
        source_run=source_run,
        request=request,
        context=context,
        payload=payload or {},
        remote_data=remote_data,
        review_source_run_id=review_source_run_id,
    )


def _refresh_publish_child_remote_state(child_run, *, context=None):
    if not child_run or not _run_belongs_to_context(child_run, context):
        return child_run
    if _publish_child_missing_remote(child_run) or _publish_child_run_recoverable(child_run):
        return child_run
    if _run_has_publish_pr_or_preview_evidence(child_run) or _publish_child_has_real_approval_gate(child_run):
        return child_run
    if child_run.status not in RUNNING_RUN_STATUSES and child_run.status != ContentFactoryRunStatus.BLOCKED:
        return child_run
    remote_data = _call_content_factory_run_status(child_run.run_id, workflow=child_run.workflow)
    if _is_status_poll_unavailable_payload(remote_data) or not remote_data:
        return child_run
    return _sync_local_run_from_remote(child_run, remote_data)


def _ensure_local_publish_child_from_known_id(
    *,
    child_run_id,
    source_run,
    request,
    context,
    payload=None,
    remote_data=None,
    review_source_run_id="",
):
    child_run_id = str(child_run_id or "").strip()
    if not child_run_id or not source_run:
        return None
    child = ContentFactoryRun.objects.filter(run_id=child_run_id).prefetch_related("steps").first()
    if child:
        return child if _run_belongs_to_context(child, context) else None

    config = _get_config(context.organization)
    publish_payload = {
        **(payload or {}),
        "source_run_id": source_run.run_id,
        "delivery_mode": "publish_code",
        "delivery_mode_confirmed": True,
    }
    if review_source_run_id:
        publish_payload["review_source_run_id"] = review_source_run_id
    child_remote_data = {
        "run_id": child_run_id,
        "status": "queued",
        "current_step": "queued",
        "source_run_id": source_run.run_id,
        **(remote_data or {}),
    }
    return _create_local_run(
        workflow="article_generation",
        domain=context.organization.domain,
        github_repo=config.github_repo or source_run.github_repo or "",
        actor_id=founder_actor_id_for_user(request.user),
        payload=publish_payload,
        remote_data=child_remote_data,
    )


def _recover_publish_child_for_run(run, *, request, context):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return None
    publish_source_run = _accepted_component_revision_for_publish(run, context) or run
    known_child_id = _publish_child_run_id_for_run(run) or _publish_child_run_id_for_run(publish_source_run)
    if known_child_id:
        child = ContentFactoryRun.objects.filter(run_id=known_child_id).prefetch_related("steps").first()
        if child is not None and not _run_belongs_to_context(child, context):
            child = None
        if child is not None:
            _attach_publish_child_to_run(run, child.run_id)
            if publish_source_run.pk != run.pk:
                _attach_publish_child_to_run(publish_source_run, child.run_id)
            return child
        if _publish_handoff_fresh_for_known_child(run) or _publish_handoff_fresh_for_known_child(publish_source_run):
            return None
        remote_data = _call_content_factory_run_status(known_child_id, workflow="article_generation")
        if _remote_status_payload_missing_run(remote_data):
            _mark_publish_child_missing_remote_on_run(run, known_child_id)
            if publish_source_run.pk != run.pk:
                _mark_publish_child_missing_remote_on_run(publish_source_run, known_child_id)
            return None
        if remote_data and not _is_status_poll_unavailable_payload(remote_data):
            child = _sync_confirmed_remote_publish_child(
                child_run_id=known_child_id,
                source_run=publish_source_run,
                request=request,
                context=context,
                remote_data=remote_data,
                payload={},
                review_source_run_id=run.run_id if publish_source_run.run_id != run.run_id else "",
            )
            if child is not None:
                _attach_publish_child_to_run(run, child.run_id)
                if publish_source_run.pk != run.pk:
                    _attach_publish_child_to_run(publish_source_run, child.run_id)
                return child

    if not _publish_handoff_pending_for_run(run) and not _publish_handoff_pending_for_run(publish_source_run):
        return None

    candidate_ids = []
    for source in (publish_source_run, run):
        child_id = _deterministic_publish_child_run_id(source.run_id)
        if child_id and child_id not in candidate_ids:
            candidate_ids.append(child_id)
    for child_id in candidate_ids:
        child = ContentFactoryRun.objects.filter(run_id=child_id).prefetch_related("steps").first()
        if child is not None and _run_belongs_to_context(child, context):
            _attach_publish_child_to_run(run, child.run_id)
            if publish_source_run.pk != run.pk:
                _attach_publish_child_to_run(publish_source_run, child.run_id)
            return child
    return None


def _accepted_revision_source_run(run):
    if not run or run.workflow != "article_revision":
        return None
    source_run_id = _run_source_run_id(run)
    if not source_run_id:
        return None
    return ContentFactoryRun.objects.filter(run_id=source_run_id).prefetch_related("steps").first()


def _source_accepts_revision(source_run, revision_run):
    if not source_run or not revision_run:
        return False
    latest_batch = _component_feedback_from_run(source_run).get("latestBatch") or {}
    revision_run_id = str(
        latest_batch.get("revisionRunId")
        or latest_batch.get("revision_run_id")
        or ""
    ).strip()
    return latest_batch.get("status") == "accepted" and revision_run_id == revision_run.run_id


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


def _iso_or_none(value):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _article_setup_state_value(*mappings, keys) -> str:
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            value = mapping.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _scan_identity_from_run(run) -> dict:
    if not run:
        return {}
    result = _run_mapping(run.result)
    nested = _run_mapping(result.get("result"))
    run_request = _run_mapping(run.run_request)
    scan = _run_mapping(result.get("scan") or nested.get("scan"))
    repo = str(
        result.get("github_repo")
        or result.get("githubRepo")
        or nested.get("github_repo")
        or nested.get("githubRepo")
        or run_request.get("github_repo")
        or run_request.get("githubRepo")
        or getattr(run, "github_repo", "")
        or ""
    ).strip()
    default_branch = _article_setup_state_value(
        result,
        nested,
        scan,
        run_request,
        keys=("defaultBranch", "default_branch", "branch", "branchName", "branch_name"),
    )
    head_sha = _article_setup_state_value(
        result,
        nested,
        scan,
        run_request,
        keys=("defaultBranchSha", "default_branch_sha", "repoHeadSha", "repo_head_sha", "commitSha", "commit_sha"),
    )
    completed_at = (
        _article_setup_state_value(result, nested, scan, keys=("scan_completed_at", "scanCompletedAt", "completedAt", "completed_at"))
        or _iso_or_none(getattr(run, "updated_at", None))
    )
    return {
        "repo": repo,
        "defaultBranch": default_branch,
        "defaultBranchSha": head_sha,
        "scanRunId": getattr(run, "run_id", "") or "",
        "completedAt": completed_at,
    }


def _scan_identity_from_config(config, article_system: dict) -> dict:
    scan_state = article_system.get("scan") if isinstance(article_system.get("scan"), dict) else {}
    scan_summary = getattr(config, "scan_summary", None)
    if isinstance(scan_summary, str) and scan_summary.strip():
        try:
            scan_summary = json.loads(scan_summary)
        except json.JSONDecodeError:
            try:
                scan_summary = ast.literal_eval(scan_summary)
            except (SyntaxError, ValueError):
                scan_summary = {}
    scan_summary = scan_summary if isinstance(scan_summary, dict) else {}
    repo = str(
        scan_state.get("githubRepo")
        or scan_state.get("github_repo")
        or scan_summary.get("github_repo")
        or scan_summary.get("githubRepo")
        or getattr(config, "github_repo", "")
        or ""
    ).strip()
    default_branch = _article_setup_state_value(
        scan_state,
        scan_summary,
        keys=("defaultBranch", "default_branch", "branch", "branchName", "branch_name"),
    )
    head_sha = _article_setup_state_value(
        scan_state,
        scan_summary,
        keys=("defaultBranchSha", "default_branch_sha", "repoHeadSha", "repo_head_sha", "commitSha", "commit_sha"),
    ) or str(getattr(config, "last_scanned_sha", "") or "").strip()
    completed_at = (
        _article_setup_state_value(scan_state, scan_summary, keys=("completedAt", "completed_at", "scanCompletedAt", "scan_completed_at"))
        or _iso_or_none(getattr(config, "last_scanned_at", None))
    )
    return {
        "repo": repo,
        "defaultBranch": default_branch,
        "defaultBranchSha": head_sha,
        "scanRunId": _article_setup_state_value(scan_state, scan_summary, keys=("scanRunId", "scan_run_id", "runId", "run_id")),
        "completedAt": completed_at,
    }


def _latest_persisted_run_for_article_setup(config, workflows: set[str], *, run_id: str = ""):
    if not config or not getattr(config, "organization_id", None):
        return None
    domain = getattr(config.organization, "domain", "")
    run_id = str(run_id or "").strip()
    if run_id:
        run = ContentFactoryRun.objects.filter(run_id=run_id).prefetch_related("steps").first()
        if run is not None:
            return run
    queryset = (
        ContentFactoryRun.objects.filter(domain=domain, workflow__in=workflows)
        .exclude(status=ContentFactoryRunStatus.CANCELLED)
        .prefetch_related("steps")
        .order_by("-updated_at")
    )
    repo = str(getattr(config, "github_repo", "") or "").strip()
    if repo:
        repo_run = queryset.filter(github_repo__iexact=repo).first()
        if repo_run is not None:
            return repo_run
    return queryset.first()


def _dedupe_runs(*groups):
    seen = set()
    runs = []
    for group in groups:
        for run in group or []:
            if not run or run.run_id in seen:
                continue
            seen.add(run.run_id)
            runs.append(run)
    return runs


def _article_setup_state_run_from_latest(latest_runs, run_id: str):
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    return next((run for run in latest_runs or [] if run.run_id == run_id), None)


def _article_setup_state_for_config(config, *, latest_runs=None, run=None) -> dict:
    latest_runs = _dedupe_runs([run] if run else [], latest_runs or [])
    raw_article_system = config.article_system if isinstance(getattr(config, "article_system", None), dict) else {}
    article_system = resolve_article_system(config) if config else {}
    pending = _pending_article_system_setup_from_config(config) if config else {}
    pending_setup_run_id = str(
        pending.get("setupRunId") or pending.get("setup_run_id") or ""
    ).strip()
    pending_source_scan_run_id = str(
        pending.get("sourceScanRunId") or pending.get("source_scan_run_id") or ""
    ).strip()
    pending_rescan_run_id = str(
        pending.get("rescanRunId") or pending.get("rescan_run_id") or ""
    ).strip()

    explicit_runs = []
    for run_id in (pending_setup_run_id, pending_source_scan_run_id, pending_rescan_run_id):
        explicit_run = _article_setup_state_run_from_latest(latest_runs, run_id) or _latest_persisted_run_for_article_setup(
            config,
            VIBE_MARKETING_WORKFLOWS,
            run_id=run_id,
        )
        if explicit_run:
            explicit_runs.append(explicit_run)
    latest_scan = (
        (run if run and run.workflow in SCAN_WORKFLOWS else None)
        or _article_setup_state_run_from_latest(explicit_runs, pending_source_scan_run_id)
        or _latest_run_matching(latest_runs, SCAN_WORKFLOWS)
        or _latest_persisted_run_for_article_setup(config, SCAN_WORKFLOWS)
    )
    setup_run = (
        (run if run and run.workflow == "article_system_setup" else None)
        or _article_setup_state_run_from_latest(explicit_runs, pending_setup_run_id)
        or _latest_article_system_setup_run(latest_runs, setup_run_id=pending_setup_run_id)
        or _latest_persisted_run_for_article_setup(config, {"article_system_setup"}, run_id=pending_setup_run_id)
    )
    related_runs = _dedupe_runs(latest_runs, explicit_runs, [latest_scan, setup_run])
    setup_gate = _article_system_setup_gate(config, related_runs, article_system)
    if setup_gate.get("setupRunId") and (not setup_run or setup_run.run_id != setup_gate.get("setupRunId")):
        setup_run = (
            _article_setup_state_run_from_latest(related_runs, setup_gate.get("setupRunId"))
            or _latest_persisted_run_for_article_setup(config, {"article_system_setup"}, run_id=setup_gate.get("setupRunId"))
        )
        related_runs = _dedupe_runs(related_runs, [setup_run])

    scan_config_identity = _scan_identity_from_config(config, raw_article_system) if config else {}
    scan_run_identity = _scan_identity_from_run(latest_scan)
    scan_repo = scan_run_identity.get("repo") or scan_config_identity.get("repo") or str(getattr(config, "github_repo", "") or "").strip()
    scan_default_branch = scan_run_identity.get("defaultBranch") or scan_config_identity.get("defaultBranch")
    scan_head_sha = scan_run_identity.get("defaultBranchSha") or scan_config_identity.get("defaultBranchSha")
    last_scanned_sha = str(getattr(config, "last_scanned_sha", "") or "").strip()
    scan_completed_at = (
        scan_run_identity.get("completedAt")
        or scan_config_identity.get("completedAt")
        or _iso_or_none(getattr(config, "last_scanned_at", None))
    )
    has_persisted_scan = bool(
        scan_completed_at
        or last_scanned_sha
        or getattr(config, "scan_summary", None)
        or article_system
        or getattr(config, "publish_targets", None)
        or pending_source_scan_run_id
    )
    scan_status = ""
    if latest_scan:
        scan_status = latest_scan.status
    elif has_persisted_scan:
        scan_status = "completed"
    scan_stale = bool(last_scanned_sha and scan_head_sha and last_scanned_sha != scan_head_sha)

    setup_meta = _setup_metadata_from_run(setup_run) if setup_run else {}
    setup_status = (
        setup_meta.get("setupStatus")
        or setup_gate.get("setupStatus")
        or pending.get("setupStatus")
        or pending.get("setup_status")
        or pending.get("status")
        or (setup_run.status if setup_run else "")
        or ""
    )
    setup_run_id = (
        setup_meta.get("setupRunId")
        or setup_gate.get("setupRunId")
        or pending_setup_run_id
        or (setup_run.run_id if setup_run else "")
        or ""
    )
    route_path = _article_setup_state_value(
        pending,
        article_system,
        keys=("routePath", "route_path", "path", "publicPath", "public_path", "listingPath", "listing_path"),
    )
    preview_url = setup_meta.get("previewUrl") or setup_gate.get("previewUrl") or ""
    fallback_preview_url = setup_meta.get("fallbackPreviewUrl") or setup_gate.get("fallbackPreviewUrl") or ""
    live_preview_url = setup_meta.get("livePreviewUrl") or setup_gate.get("livePreviewUrl") or ""
    pr_url = setup_meta.get("prUrl") or setup_gate.get("prUrl") or ""
    live_preview = _live_preview_from_run(setup_run) if setup_run else None
    setup_result = _run_mapping(setup_run.result) if setup_run else {}
    setup_payload = _article_system_setup_payload_from_run(setup_run) if setup_run else {}
    error = str(
        (setup_run.error if setup_run else "")
        or setup_result.get("error")
        or setup_result.get("error_message")
        or setup_payload.get("error")
        or setup_payload.get("error_message")
        or ""
    ).strip()
    updated_values = [
        getattr(latest_scan, "updated_at", None),
        getattr(setup_run, "updated_at", None),
        getattr(config, "updated_at", None),
    ]
    updated_values = [value for value in updated_values if value]
    updated_at = max(updated_values).isoformat() if updated_values else None
    return {
        "repo": scan_repo,
        "githubRepo": scan_repo,
        "defaultBranch": scan_default_branch or None,
        "defaultBranchSha": scan_head_sha or last_scanned_sha or None,
        "lastScannedSha": last_scanned_sha or None,
        "scanRunId": (latest_scan.run_id if latest_scan else pending_source_scan_run_id or scan_config_identity.get("scanRunId")) or None,
        "scanStatus": scan_status or None,
        "scanCompletedAt": scan_completed_at or None,
        "scanUpdatedAt": _iso_or_none(getattr(latest_scan, "updated_at", None)),
        "scanStale": scan_stale,
        "scanNeedsRescan": scan_stale,
        "staleReason": "default_branch_changed" if scan_stale else None,
        "setupRunId": setup_run_id or None,
        "setupStatus": str(setup_status or "").strip() or None,
        "setupRunStatus": setup_run.status if setup_run else None,
        "setupCurrentStep": (setup_payload.get("currentStep") or setup_payload.get("current_step") or getattr(setup_run, "current_step", "") or None) if setup_run else None,
        "setupBlocked": bool(setup_gate.get("setupBlocked")),
        "published": bool(setup_gate.get("published")),
        "routePath": route_path or None,
        "previewUrl": preview_url or None,
        "fallbackPreviewUrl": fallback_preview_url or None,
        "livePreviewUrl": live_preview_url or None,
        "prUrl": pr_url or None,
        "livePreview": live_preview,
        "retryAvailable": bool((setup_run and (setup_run.resume_available or setup_result.get("retry_available"))) or (latest_scan and latest_scan.resume_available)),
        "error": error or None,
        "source": "setup_run" if setup_run else "scan_run" if latest_scan else "config" if has_persisted_scan or pending else "none",
        "updatedAt": updated_at,
        "articleSurfaceMode": pending.get("mode") or pending.get("articleSurfaceMode") or pending.get("article_surface_mode") or None,
        "articleSurfaceHint": pending.get("articleSurfaceHint") or pending.get("article_surface_hint") or None,
    }


def _article_setup_state(*, context=None, run=None, latest_runs=None) -> dict:
    organization = context.organization if context is not None else None
    if organization is None and run is not None and run.domain:
        organization = Organization.objects.filter(domain__iexact=normalize_company_domain(run.domain)).first()
    if organization is None:
        return {
            "repo": "",
            "githubRepo": "",
            "scanStatus": None,
            "scanRunId": None,
            "scanStale": False,
            "scanNeedsRescan": False,
            "setupRunId": None,
            "setupStatus": None,
            "setupBlocked": False,
            "published": False,
            "source": "none",
        }
    config = _get_config(organization) if context is not None else OrganizationContentConfig.objects.filter(organization=organization).first()
    if config is None:
        return {
            "repo": "",
            "githubRepo": "",
            "scanStatus": None,
            "scanRunId": None,
            "scanStale": False,
            "scanNeedsRescan": False,
            "setupRunId": None,
            "setupStatus": None,
            "setupBlocked": False,
            "published": False,
            "source": "none",
        }
    return _article_setup_state_for_config(config, latest_runs=latest_runs, run=run)


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


def _scan_run_is_pending_article_system_generation(run) -> bool:
    if not run or run.workflow not in SCAN_WORKFLOWS:
        return False
    run_request = _run_mapping(run.run_request)
    result = _run_mapping(run.result)
    nested = _run_mapping(result.get("result"))
    setup = _run_mapping(result.get("article_system_setup") or nested.get("article_system_setup"))
    if run_request.get("scan_purpose") == "setup" or result.get("pending_article_system_setup"):
        return True
    requested_action = str(
        result.get("requested_action")
        or result.get("setup_requested_action")
        or nested.get("requested_action")
        or nested.get("setup_requested_action")
        or setup.get("requested_action")
        or ""
    ).strip()
    return requested_action in {"article_system_setup", "scaffold_publish_route"}


def _setup_value(mapping: dict, *keys, default=""):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _article_system_setup_payload_from_run(run) -> dict:
    if not run:
        return {}
    result = _run_mapping(run.result)
    nested = _run_mapping(result.get("result"))
    return _run_mapping(result.get("article_system_setup") or nested.get("article_system_setup"))


def _setup_metadata_from_run(run) -> dict:
    if not run:
        return {}
    result = _run_mapping(run.result)
    setup = _article_system_setup_payload_from_run(run)
    status_value = str(
        _setup_value(result, "status")
        or _setup_value(setup, "status")
        or getattr(run, "current_step", "")
        or getattr(run, "status", "")
        or ""
    ).strip()
    setup_run_id = str(
        _setup_value(result, "setup_run_id", "setupRunId")
        or _setup_value(setup, "setup_run_id", "setupRunId")
        or (run.run_id if run.workflow == "article_system_setup" else "")
    ).strip()
    rescan_run_id = str(
        _setup_value(result, "rescan_run_id", "rescanRunId")
        or _setup_value(setup, "rescan_run_id", "rescanRunId")
        or ""
    ).strip()
    return {
        "setupRunId": setup_run_id,
        "setupStatus": status_value,
        "rescanRunId": rescan_run_id,
        "prUrl": str(_setup_value(result, "pr_url", "prUrl") or _setup_value(setup, "pr_url", "prUrl") or "").strip(),
        "previewUrl": str(
            _setup_value(result, "preview_url", "previewUrl")
            or _setup_value(setup, "preview_url", "previewUrl")
            or ""
        ).strip(),
        "fallbackPreviewUrl": str(
            _setup_value(result, "fallback_preview_url", "fallbackPreviewUrl")
            or _setup_value(setup, "fallback_preview_url", "fallbackPreviewUrl")
            or ""
        ).strip(),
        "livePreviewUrl": str(
            _setup_value(result, "live_preview_url", "livePreviewUrl")
            or _setup_value(setup, "live_preview_url", "livePreviewUrl")
            or ""
        ).strip(),
    }


def _latest_article_system_setup_run(latest_runs, *, setup_run_id: str = ""):
    setup_run_id = str(setup_run_id or "").strip()
    if setup_run_id:
        matched = next((run for run in latest_runs or [] if run.run_id == setup_run_id), None)
        if matched:
            return matched
    return _latest_run_matching(latest_runs or [], {"article_system_setup"})


def _verification_scan_for_setup(latest_runs, *, rescan_run_id: str = ""):
    rescan_run_id = str(rescan_run_id or "").strip()
    if not rescan_run_id:
        return None
    return next(
        (
            run
            for run in latest_runs or []
            if run.run_id == rescan_run_id and run.workflow in SCAN_WORKFLOWS
        ),
        None,
    )


def _article_system_is_published(config, article_system: dict) -> bool:
    state = str(article_system.get("state") or "").strip()
    if state in ARTICLE_SYSTEM_PUBLISHED_STATES:
        return True
    if state == "roo_scaffolded" and bool(getattr(config, "articles_scaffolded", False)):
        return True
    return bool(getattr(config, "publish_targets", None))


def _article_system_setup_gate(config, latest_runs, article_system: dict) -> dict:
    pending = _pending_article_system_setup_from_config(config)
    meta = {
        "published": False,
        "setupBlocked": False,
        "setupRunId": None,
        "setupStatus": None,
        "rescanRunId": None,
        "prUrl": None,
        "previewUrl": None,
        "livePreviewUrl": None,
        "fallbackPreviewUrl": None,
    }
    if not config:
        return meta

    for source_key, target_key in (
        ("setupRunId", "setupRunId"),
        ("setup_run_id", "setupRunId"),
        ("status", "setupStatus"),
        ("setupStatus", "setupStatus"),
        ("setup_status", "setupStatus"),
        ("rescanRunId", "rescanRunId"),
        ("rescan_run_id", "rescanRunId"),
        ("prUrl", "prUrl"),
        ("pr_url", "prUrl"),
        ("previewUrl", "previewUrl"),
        ("preview_url", "previewUrl"),
        ("fallbackPreviewUrl", "fallbackPreviewUrl"),
        ("fallback_preview_url", "fallbackPreviewUrl"),
        ("livePreviewUrl", "livePreviewUrl"),
        ("live_preview_url", "livePreviewUrl"),
    ):
        if pending.get(source_key):
            meta[target_key] = pending.get(source_key)

    latest_scan = _latest_run_matching(latest_runs or [], SCAN_WORKFLOWS)
    if latest_scan and _scan_run_is_pending_article_system_generation(latest_scan):
        scan_meta = _setup_metadata_from_run(latest_scan)
        for key, value in scan_meta.items():
            if value and not meta.get(key):
                meta[key] = value
        if not meta.get("setupStatus"):
            meta["setupStatus"] = latest_scan.status

    setup_run = _latest_article_system_setup_run(latest_runs or [], setup_run_id=str(meta.get("setupRunId") or ""))
    if setup_run:
        run_meta = _setup_metadata_from_run(setup_run)
        for key, value in run_meta.items():
            if value:
                meta[key] = value
        if not meta.get("setupStatus"):
            meta["setupStatus"] = setup_run.status

    rescan_run = _verification_scan_for_setup(latest_runs or [], rescan_run_id=str(meta.get("rescanRunId") or ""))
    verification_published = bool(
        rescan_run
        and rescan_run.status == ContentFactoryRunStatus.COMPLETED
        and _article_system_is_published(config, article_system)
    )
    published = bool(_article_system_is_published(config, article_system) and (not pending or verification_published))
    setup_status = str(meta.get("setupStatus") or "").strip().lower()
    setup_has_active_signal = bool(pending or setup_run or (latest_scan and _scan_run_is_pending_article_system_generation(latest_scan)))
    completed_with_pending_verification = bool(
        meta.get("rescanRunId") and not verification_published
    )
    setup_blocked = bool(
        setup_has_active_signal
        and not published
        and (
            bool(pending)
            or completed_with_pending_verification
            or setup_status in ARTICLE_SYSTEM_SETUP_BLOCKING_STATUSES
            or (setup_run and setup_run.status in RUNNING_RUN_STATUSES | FAILED_RUN_STATUSES | {
                ContentFactoryRunStatus.AWAITING_CONFIRMATION,
                ContentFactoryRunStatus.AWAITING_APPROVAL,
                ContentFactoryRunStatus.APPROVAL_REQUIRED,
            })
        )
    )
    if setup_status in {"published", "verified"}:
        setup_blocked = False
        published = _article_system_is_published(config, article_system)
    if verification_published:
        setup_blocked = False
        published = True

    meta["published"] = bool(published)
    meta["setupBlocked"] = bool(setup_blocked)
    for key in ("setupRunId", "setupStatus", "rescanRunId", "prUrl", "previewUrl", "fallbackPreviewUrl", "livePreviewUrl"):
        meta[key] = str(meta.get(key) or "").strip() or None
    return meta


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
    revision_source_run = _accepted_revision_source_run(article_run)
    publish_evidence_run = publish_child_run or article_run
    publish_evidence = _publish_evidence_from_run(publish_evidence_run)
    content_package = _content_package_from_run(article_run) if article_run else None
    content_package_ready = bool(content_package and content_package.get("contentPackaged"))
    component_feedback = _component_feedback_from_run(article_run) if article_run else {"comments": [], "latestBatch": None}
    if _source_accepts_revision(revision_source_run, article_run):
        component_feedback = _component_feedback_from_run(revision_source_run)
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
    article_result = _run_mapping(article_run.result) if article_run else {}
    publish_handoff_pending = bool(article_result.get("publish_handoff_pending"))
    publish_handoff_stale = _publish_handoff_stale_for_run(article_run)
    publish_child_recoverable = _publish_child_run_recoverable(publish_child_run) or bool(article_result.get("publish_child_recoverable"))
    publish_child_missing_remote = bool(
        article_result.get("publish_handoff_status") == "recoverable_missing_child"
        or "not found in content factory" in str(article_result.get("publish_child_wait_reason") or "").lower()
    )
    publish_running = bool(
        (publish_child_run and publish_child_run.status in RUNNING_RUN_STATUSES)
        or (publish_handoff_pending and not publish_handoff_stale and not publish_complete)
    )
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

    scaffold_check = checks.get("scaffold", {})
    setup_blocked = bool(scaffold_check.get("setupBlocked"))

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
        pending_setup_generation = _scan_run_is_pending_article_system_generation(scan_run)
        if pending_setup_generation:
            status_by_id["article_system"] = "complete"
            href_by_id["generate"] = _run_url(scan_run)
            run_by_id["generate"] = scan_run.run_id
            if scan_run.status in RUNNING_RUN_STATUSES:
                status_by_id["generate"] = "running"
                summary_by_id["generate"] = "Articles setup scan is preparing the setup plan."
            elif scan_run.status in FAILED_RUN_STATUSES:
                status_by_id["generate"] = "blocked"
                action_by_id["generate"] = _workflow_step_action("Open failed setup scan", href=_run_url(scan_run))
            elif scan_run.status in {ContentFactoryRunStatus.AWAITING_CONFIRMATION, ContentFactoryRunStatus.AWAITING_APPROVAL, ContentFactoryRunStatus.APPROVAL_REQUIRED}:
                status_by_id["generate"] = "needs_action"
                action_by_id["generate"] = _workflow_step_action("Generate articles setup preview", href=_run_url(scan_run), intent="build-article-system-preview")
            else:
                status_by_id["generate"] = "ready"
                action_by_id["generate"] = _workflow_step_action("Generate articles setup preview", href=_run_url(scan_run), intent="build-article-system-preview")
        elif scan_run.status in RUNNING_RUN_STATUSES:
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

    if discovery_run and not checks.get("research", {}).get("passed") and not setup_blocked:
        run_by_id["research"] = discovery_run.run_id
        href_by_id["research"] = _run_url(discovery_run)
        if discovery_run.status in RUNNING_RUN_STATUSES:
            status_by_id["research"] = "running"
            summary_by_id["research"] = "Topic discovery is running."
        elif discovery_run.status in FAILED_RUN_STATUSES:
            status_by_id["research"] = "blocked"
            action_by_id["research"] = _workflow_step_action("Open research run", href=_run_url(discovery_run))
    if checks.get("scaffold", {}).get("passed") and not setup_blocked and not checks.get("research", {}).get("passed") and status_by_id["research"] == "locked":
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
        href_by_id["publish"] = _run_url(publish_child_run or article_run)
        run_by_id["publish"] = (publish_child_run or article_run).run_id
        summary_by_id["publish"] = "Publishing child run is in progress." if publish_child_run else "Publish handoff is pending."
    elif publish_child_recoverable and content_package_ready and package_can_promote:
        status_by_id["publish"] = "ready"
        href_by_id["publish"] = _run_url(article_run)
        run_by_id["publish"] = article_run.run_id
        action_by_id["publish"] = _workflow_step_action(
            "Retry creating PR" if publish_child_missing_remote else "Resume publish PR",
            href=_run_url(article_run),
            intent="promote-bundle",
        )
        summary_by_id["publish"] = (
            "Publish child was not found in Content Factory. Retry safely."
            if publish_child_missing_remote
            else "Publish child is waiting for confirmation. Resume safely."
        )
    elif publish_handoff_stale and content_package_ready and package_can_promote:
        status_by_id["publish"] = "ready"
        href_by_id["publish"] = _run_url(article_run)
        run_by_id["publish"] = article_run.run_id
        action_by_id["publish"] = _workflow_step_action("Retry publish handoff", href=_run_url(article_run), intent="promote-bundle")
        summary_by_id["publish"] = "Previous publish handoff did not create a PR. Retry safely."
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

    if setup_blocked:
        setup_run_id = str(scaffold_check.get("setupRunId") or "").strip()
        setup_status = str(scaffold_check.get("setupStatus") or "").strip().lower()
        rescan_run_id = str(scaffold_check.get("rescanRunId") or "").strip()
        setup_run_url = f"/founder-tools/marketing/runs/{setup_run_id}" if setup_run_id else href_by_id["article_system"]
        rescan_run = _verification_scan_for_setup(latest_runs, rescan_run_id=rescan_run_id)
        for blocked_step_id in ("research", "choose_topic", "revise", "package", "automation"):
            action_by_id.pop(blocked_step_id, None)
            run_by_id.pop(blocked_step_id, None)
            status_by_id[blocked_step_id] = "locked"
        for setup_step_id in ("generate", "review", "publish"):
            action_by_id.pop(setup_step_id, None)
        status_by_id["article_system"] = "complete"
        href_by_id["generate"] = setup_run_url
        href_by_id["review"] = setup_run_url
        href_by_id["publish"] = setup_run_url
        if setup_run_id:
            run_by_id["generate"] = setup_run_id
            run_by_id["review"] = setup_run_id
            run_by_id["publish"] = setup_run_id
        if setup_status in {"manual_merge_required", "manual_blocked"}:
            status_by_id["generate"] = "complete"
            status_by_id["review"] = "complete"
            status_by_id["publish"] = "needs_action"
            summary_by_id["publish"] = "The setup PR must be merged manually before verification can unlock article generation."
            action_by_id["publish"] = _workflow_step_action(
                "Open setup PR",
                href=scaffold_check.get("prUrl") or setup_run_url,
                variant="secondary",
            )
        elif rescan_run_id or setup_status in {"completed", "merged", "merged_verifying", "verifying"}:
            status_by_id["generate"] = "complete"
            status_by_id["review"] = "complete"
            status_by_id["publish"] = "running"
            summary_by_id["publish"] = "The merged articles directory is being verified before topic research unlocks."
            if rescan_run:
                href_by_id["publish"] = _run_url(rescan_run)
                run_by_id["publish"] = rescan_run.run_id
        elif setup_status in {"preview_ready", "revision_ready", "awaiting_approval", "approval_required", "await_review"}:
            status_by_id["generate"] = "complete"
            status_by_id["review"] = "needs_action"
            status_by_id["publish"] = "locked"
            summary_by_id["review"] = "Review the hosted setup preview and approve the setup PR when ready."
            action_by_id["review"] = _workflow_step_action("Review articles setup preview", href=setup_run_url)
        elif setup_status == "fallback_ready":
            status_by_id["generate"] = "blocked"
            status_by_id["review"] = "needs_action"
            status_by_id["publish"] = "locked"
            summary_by_id["review"] = "Only a fallback setup preview is available; exact preview must be fixed before approval."
            action_by_id["review"] = _workflow_step_action("Open setup diagnostics", href=setup_run_url, variant="secondary")
        elif setup_status in {"preview_failed", "failed", "blocked"}:
            status_by_id["generate"] = "complete"
            status_by_id["review"] = "blocked"
            status_by_id["publish"] = "locked"
            summary_by_id["review"] = "Hosted setup preview failed. Open diagnostics, inspect the build logs, then retry."
            action_by_id["review"] = _workflow_step_action("Open setup diagnostics", href=setup_run_url, variant="secondary")
        else:
            status_by_id["generate"] = "running"
            status_by_id["review"] = "locked"
            status_by_id["publish"] = "locked"
            summary_by_id["generate"] = "Articles setup is preparing the preview and setup PR."

    if not setup_blocked and checks.get("dailyAutomation", {}).get("passed"):
        status_by_id["automation"] = "complete"
    elif not setup_blocked and status_by_id["publish"] == "complete":
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


COMPACT_RUN_RESULT_KEYS = {
    "article_surface_hint",
    "articleSurfaceHint",
    "article_surface_hint_status",
    "article_surface_mode",
    "articleSurfaceMode",
    "article_system_readiness",
    "article_system_setup",
    "detected_candidates",
    "delivery_mode",
    "deliveryMode",
    "draft_pr_url",
    "path",
    "matched_article_surface",
    "pending_article_system_setup",
    "preview_url",
    "previewUrl",
    "pr_url",
    "prUrl",
    "promote_bundle_requested_at",
    "promoted_publish_job_id",
    "publish_child_recoverable",
    "publish_child_run_id",
    "publish_child_status",
    "publish_child_wait_reason",
    "publish_handoff_pending",
    "pull_request_url",
    "requested_action",
    "resolved_delivery_mode",
    "route_path",
    "scan_purpose",
    "scanPurpose",
    "scaffold_job_id",
    "scaffoldJobId",
    "scaffold_plan",
    "scaffold_status",
    "setup_requested_action",
    "setup_run_id",
    "setupRunId",
    "stale",
    "stale_reason",
    "url",
}

COMPACT_DISCOVERY_RESULT_KEYS = {
    "candidates",
    "keyword_options",
    "keywords",
    "options",
    "selected",
    "selection",
    "selection_data",
    "topic_candidates",
    "topic_options",
    "topics",
}

COMPACT_AUTOFILL_RESULT_KEYS = {
    "adjacentOrganizations",
    "brandName",
    "companyContext",
    "companyLinkedInUrl",
    "competitorGroups",
    "competitorCount",
    "competitorSuggestions",
    "competitors",
    "directCompetitors",
    "keywordCandidateCount",
    "linkedinProfile",
    "partial",
    "profileFields",
    "researchDepth",
    "researchQuality",
    "researchSummary",
    "seedKeywords",
    "seedKeywordCount",
    "seoCompetitors",
    "sourceCount",
    "sources",
    "warnings",
}

COMPACT_AUTOFILL_LIST_LIMITS = {
    "adjacentOrganizations": 12,
    "competitorSuggestions": 12,
    "competitors": 12,
    "directCompetitors": 12,
    "seedKeywords": 24,
    "seoCompetitors": 12,
    "sources": 12,
    "warnings": 12,
}


def _compact_result_value(key, value):
    if isinstance(value, list):
        return value[: COMPACT_AUTOFILL_LIST_LIMITS.get(key, 12)]
    return value


def _source_looks_like_autofill_payload(source):
    if not isinstance(source, dict):
        return False
    return any(
        source.get(key) not in (None, "", [], {})
        for key in (
            "profileFields",
            "companyContext",
            "companyLinkedInUrl",
            "directCompetitors",
            "competitors",
            "seedKeywords",
            "researchQuality",
        )
    )


def _compact_autofill_payload_from_sources(sources):
    compact = {}
    autofill_sources = []
    for source in sources:
        nested = _run_mapping(source.get("autofill"))
        if nested:
            autofill_sources.append(nested)
        if _source_looks_like_autofill_payload(source):
            autofill_sources.append(source)

    for source in autofill_sources:
        for key in COMPACT_AUTOFILL_RESULT_KEYS:
            value = source.get(key)
            if value not in (None, "", [], {}):
                compact.setdefault(key, _compact_result_value(key, value))
    return compact


def _compact_result_for_run(run):
    result = _run_mapping(run.result)
    compact = {}
    sources = [result, _run_mapping(result.get("result")), _run_mapping(result.get("latest_control_response"))]
    keys = set(COMPACT_RUN_RESULT_KEYS)
    if run.workflow in DISCOVERY_WORKFLOWS:
        keys.update(COMPACT_DISCOVERY_RESULT_KEYS)
    if run.workflow == "startup_autofill":
        keys.update(COMPACT_AUTOFILL_RESULT_KEYS)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                compact.setdefault(key, _compact_result_value(key, value))
    if run.workflow == "startup_autofill":
        autofill = _compact_autofill_payload_from_sources(sources)
        if autofill:
            compact["autofill"] = autofill
            if autofill.get("warnings") and not compact.get("warnings"):
                compact["warnings"] = autofill["warnings"]
    run_request = _run_mapping(result.get("run_request") or result.get("request"))
    request_compact = {
        key: run_request.get(key)
        for key in ("scan_purpose", "scanPurpose", "article_surface_mode", "articleSurfaceMode")
        if run_request.get(key) not in (None, "", [], {})
    }
    if request_compact:
        compact["run_request"] = request_compact
    return compact


def _has_concrete_article_surface_hint(value):
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict):
        return False
    for key in (
        "route",
        "route_path",
        "routePath",
        "path",
        "public_url",
        "publicUrl",
        "listing_url",
        "listingUrl",
        "article_surface_url",
        "articleSurfaceUrl",
        "url",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return True
    return False


def _log_terminal_repo_scan_status(run, payload):
    if run.workflow not in SCAN_WORKFLOWS or run.status not in SCAN_LOCAL_AUTHORITATIVE_STATUSES:
        return
    result = _run_mapping(payload.get("result"))
    readiness = _run_mapping(result.get("article_system_readiness"))
    candidates = result.get("detected_candidates")
    if not isinstance(candidates, list):
        candidates = readiness.get("detected_candidates")
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    request_payload = _run_mapping(result.get("run_request") or result.get("request"))
    logger.info(
        "vibe_marketing_repo_scan_status_terminal run_id=%s status=%s scan_purpose=%s setup_hint_present=%s candidate_count=%s",
        run.run_id,
        run.status,
        result.get("scan_purpose") or result.get("scanPurpose") or request_payload.get("scan_purpose") or request_payload.get("scanPurpose") or "",
        _has_concrete_article_surface_hint(result.get("article_surface_hint"))
        or _has_concrete_article_surface_hint(result.get("articleSurfaceHint")),
        candidate_count,
    )


def _serialize_run_steps(run, *, compact=False):
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
                "artifacts": [] if compact else step.artifacts or [],
                "startedAt": step.started_at.isoformat() if step.started_at else None,
                "completedAt": step.completed_at.isoformat() if step.completed_at else None,
            }
        )
    return step_states


def _serialize_run(run, *, context=None, latest_runs=None, checks=None, mode="full"):
    compact = mode in {"summary", "status"}
    step_states = _serialize_run_steps(run, compact=compact)
    result = run.result or {}
    preview_url = result.get("preview_url") or result.get("article_url") or result.get("url")
    pr_url = result.get("pr_url") or result.get("pull_request_url") or result.get("draft_pr_url")
    live_preview = _live_preview_from_run(run)
    article_setup_state = _article_setup_state(context=context, run=run, latest_runs=latest_runs)
    if compact:
        return {
            "runId": run.run_id,
            "workflow": run.workflow,
            "domain": run.domain,
            "githubRepo": run.github_repo,
            "status": run.status,
            "currentStep": run.current_step,
            "approvalState": run.approval_state,
            "sourceRunId": _run_source_run_id(run) or None,
            "resumeAvailable": run.resume_available,
            "createdAt": run.created_at.isoformat(),
            "updatedAt": run.updated_at.isoformat(),
            "stepOrder": run.step_order or [],
            "steps": step_states,
            "warnings": result.get("warnings") or run.acceptance_summary.get("warnings") or [],
            "errors": [run.error] if run.error else result.get("errors") or [],
            "errorCode": result.get("error_code"),
            "artifacts": [],
            "previewUrl": preview_url,
            "prUrl": pr_url,
            "routePath": result.get("route_path") or result.get("path"),
            "diagnostics": {},
            "publishChildStatus": result.get("publish_child_status"),
            "publishChildRecoverable": result.get("publish_child_recoverable"),
            "publishChildWaitReason": result.get("publish_child_wait_reason"),
            "stale": bool(result.get("stale")),
            "staleReason": result.get("stale_reason"),
            "retryAvailable": bool(result.get("retry_available") or run.resume_available),
            "resumeGeneration": result.get("resume_generation"),
            "isCurrentAttempt": result.get("is_current_attempt"),
            "failureStep": result.get("failure_step"),
            "queueName": result.get("queue_name"),
            "queuedAt": result.get("queued_at"),
            "setupQueue": result.get("setup_queue"),
            "livePreview": live_preview,
            "articleSetupState": article_setup_state,
            "article_setup_state": article_setup_state,
            "workflowProgress": _workflow_progress(context=context, run=run, latest_runs=latest_runs, checks=checks),
            "result": _compact_result_for_run(run),
        }
    content_package = _content_package_from_run(run)
    component_manifest = _component_manifest_from_run(run)
    return {
        "runId": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "githubRepo": run.github_repo,
        "status": run.status,
        "currentStep": run.current_step,
        "approvalState": run.approval_state,
        "sourceRunId": _run_source_run_id(run) or None,
        "resumeAvailable": run.resume_available,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
        "stepOrder": run.step_order or [],
        "steps": step_states,
        "warnings": result.get("warnings") or run.acceptance_summary.get("warnings") or [],
        "errors": [run.error] if run.error else result.get("errors") or [],
        "errorCode": result.get("error_code"),
        "artifacts": result.get("artifacts") or [],
        "previewUrl": preview_url,
        "prUrl": pr_url,
        "routePath": result.get("route_path") or result.get("path"),
        "diagnostics": result.get("diagnostics") or run.verification_summary or {},
        "publishChildStatus": result.get("publish_child_status"),
        "publishChildRecoverable": result.get("publish_child_recoverable"),
        "publishChildWaitReason": result.get("publish_child_wait_reason"),
        "stale": bool(result.get("stale")),
            "staleReason": result.get("stale_reason"),
            "retryAvailable": bool(result.get("retry_available") or run.resume_available),
            "resumeGeneration": result.get("resume_generation"),
            "isCurrentAttempt": result.get("is_current_attempt"),
            "failureStep": result.get("failure_step"),
            "queueName": result.get("queue_name"),
        "queuedAt": result.get("queued_at"),
        "setupQueue": result.get("setup_queue"),
        "contentPackage": content_package,
        "componentManifest": component_manifest,
        "livePreview": live_preview,
        "articleSetupState": article_setup_state,
        "article_setup_state": article_setup_state,
        "componentFeedback": _component_feedback_from_run(run),
        "workflowProgress": _workflow_progress(context=context, run=run, latest_runs=latest_runs, checks=checks),
        "result": result,
    }


def _missing_mlai_featured_components(*, organization, config):
    domain = normalize_company_domain(organization.domain) if organization else ""
    repo = str(getattr(config, "github_repo", "") or "").strip().lower()
    if domain not in {"mlai.au", "www.mlai.au"} and repo != "mlai-aus-inc/mlai-au":
        return []
    existing = set(
        GeneratedComponent.objects.filter(
            organization=organization,
            name__in=MLAI_AU_FEATURED_REQUIRED_COMPONENTS,
        ).values_list("name", flat=True)
    )
    scan_summary = getattr(config, "scan_summary", None)
    if isinstance(scan_summary, str) and scan_summary.strip():
        try:
            scan_summary = json.loads(scan_summary)
        except json.JSONDecodeError:
            try:
                scan_summary = ast.literal_eval(scan_summary)
            except (SyntaxError, ValueError):
                scan_summary = None
    scan_summary = scan_summary if isinstance(scan_summary, dict) else {}
    for component in scan_summary.get("generated_components") or []:
        if isinstance(component, dict) and component.get("name"):
            existing.add(str(component["name"]))
    return sorted(MLAI_AU_FEATURED_REQUIRED_COMPONENTS - existing)


def _profile_checks(organization, config, latest_runs=None, baseline_snapshot=None):
    latest_runs = latest_runs or []
    domain_ok = bool(normalize_company_domain(organization.domain))
    context_ok = bool(str(config.company_context or "").strip()) or bool(str(config.brand_name or "").strip())
    keywords_ok = bool(organization.competitors or organization.seed_keywords)
    baseline_ready = _baseline_requirement_satisfied(config, baseline_snapshot)
    github_ready = config.github_connection_state == "connected" and bool(config.github_repo)
    article_system = resolve_article_system(config)
    setup_gate = _article_system_setup_gate(config, latest_runs, article_system)
    article_system_ready = bool(setup_gate.get("published") and not setup_gate.get("setupBlocked"))
    missing_featured_components = _missing_mlai_featured_components(organization=organization, config=config)
    component_catalog_ready = not missing_featured_components
    article_ready = article_system_ready and component_catalog_ready
    scan_ready = bool(config.last_scanned_at or config.scan_summary or config.article_system or config.publish_targets)
    discovery_run = _latest_run_matching(latest_runs, DISCOVERY_WORKFLOWS)
    article_run = _latest_run_matching(latest_runs, ARTICLE_WORKFLOWS)
    research_ready = bool(article_run) or bool(
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
    daily_prerequisites_ready = (
        domain_ok
        and context_ok
        and keywords_ok
        and baseline_ready
        and scan_ready
        and article_ready
        and bool(config.connected_slack_user_id)
        and (delivery_mode != "publish_code" or github_ready)
    )
    daily_ready = daily_prerequisites_ready and bool(config.daily_discovery_enabled)
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
        "scaffold": {
            "passed": article_ready,
            "published": bool(setup_gate.get("published")),
            "setupBlocked": bool(setup_gate.get("setupBlocked")),
            "setupRunId": setup_gate.get("setupRunId"),
            "setupStatus": setup_gate.get("setupStatus"),
            "rescanRunId": setup_gate.get("rescanRunId"),
            "prUrl": setup_gate.get("prUrl"),
            "previewUrl": setup_gate.get("previewUrl"),
            "articleSystem": article_system,
            "componentCatalogReady": component_catalog_ready,
            "missingComponents": missing_featured_components,
        },
        "research": {"passed": research_ready, "runId": discovery_run.run_id if discovery_run else None},
        "write": {"passed": write_ready, "runId": article_run.run_id if article_run else None},
        "contentPackage": {"passed": content_package_ready, "runId": article_run.run_id if article_run else None},
        "publish": {"passed": publish_ready, "runId": article_run.run_id if article_run else None},
        "dailyAutomation": {
            "passed": daily_ready,
            "ready": daily_prerequisites_ready,
            "enabled": bool(config.daily_discovery_enabled),
        },
    }


def _serialize_bootstrap(context, request=None, *, view="full"):
    compact = view == "summary"
    config = _get_config(context.organization)
    latest_runs = _latest_runs_for_org(context.organization)
    discovery_topic_runs = _recent_discovery_topic_runs_for_org(context.organization)
    topic_limit = 8 if compact else None
    declined_topic_feedback_all = list_topic_feedback(context.organization, feedback_type="declined", limit=100)
    declined_topic_feedback = declined_topic_feedback_all[:8] if compact else declined_topic_feedback_all
    declined_keyword_keys = {
        normalize_topic_feedback_keyword(item.keyword)
        for item in declined_topic_feedback_all
        if normalize_topic_feedback_keyword(item.keyword)
    }
    coverage_memory = build_topic_coverage_memory(context.organization, article_limit=100 if compact else None)
    written_memory = _written_topic_memory(context.organization, keyword_limit=500 if compact else None)
    topic_candidates = _topic_candidates_from_runs(
        discovery_topic_runs,
        organization=context.organization,
        limit=topic_limit,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        written_memory=written_memory,
    )
    hidden_topic_candidates = _topic_candidates_from_runs(
        discovery_topic_runs,
        organization=context.organization,
        include_written=True,
        limit=topic_limit,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        written_memory=written_memory,
    )
    topic_pillars = _topic_pillars_for_bootstrap(
        context.organization,
        config,
        declined_keyword_keys=declined_keyword_keys,
        coverage_memory=coverage_memory,
        compact=compact,
    )
    baseline_snapshot = _latest_baseline_snapshot(context.organization)
    checks = _profile_checks(context.organization, config, latest_runs, baseline_snapshot)
    article_setup_state = _article_setup_state_for_config(config, latest_runs=latest_runs)
    guided_steps, current_guided_step = _guided_steps(checks)
    latest_runs_by_workflow = {}
    run_mode = "summary" if compact else "full"
    for run in latest_runs:
        latest_runs_by_workflow.setdefault(
            run.workflow,
            _serialize_run(run, context=context, latest_runs=latest_runs, checks=checks, mode=run_mode),
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
            "avatarUrl": context.company.avatar_url,
            "avatar_url": context.company.avatar_url,
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
            "pendingArticleSystemSetup": _pending_article_system_setup_from_config(config),
            "pending_article_system_setup": _pending_article_system_setup_from_config(config),
            "dailyDiscoveryEnabled": config.daily_discovery_enabled,
            "dailyDiscoveryPriority": config.daily_discovery_priority,
            "defaultTimezone": config.default_timezone,
            "githubConnectionState": config.github_connection_state,
        },
        "startupProfile": _serialize_startup_profile(context.organization),
        "websiteBaseline": _serialize_baseline_snapshot(baseline_snapshot, config, compact=compact),
        "googleBaselineConnection": google_status,
        "checks": checks,
        "articleSetupState": article_setup_state,
        "article_setup_state": article_setup_state,
        "latestRuns": [_serialize_run(run, context=context, latest_runs=latest_runs, checks=checks, mode=run_mode) for run in latest_runs],
        "latestRunsByWorkflow": latest_runs_by_workflow,
        "topicCandidates": topic_candidates,
        "topicPillars": topic_pillars,
        "hiddenTopicCandidates": hidden_topic_candidates,
        "declinedTopicFeedback": [serialize_topic_feedback(item) for item in declined_topic_feedback],
        "draftArticles": _recent_article_drafts(context.organization, scan_limit=20 if compact else 50),
        "writtenTopics": _recent_written_topics(context.organization),
        "publishEvidence": _publish_evidence_from_run(latest_article_run, compact=compact),
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
        "scaffold": {
            "passed": False,
            "published": False,
            "setupBlocked": False,
            "setupRunId": None,
            "setupStatus": None,
            "rescanRunId": None,
            "prUrl": None,
            "previewUrl": None,
        },
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
            "avatarUrl": company.avatar_url,
            "avatar_url": company.avatar_url,
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
        "articleSetupState": {
            "repo": "",
            "githubRepo": "",
            "scanStatus": None,
            "scanRunId": None,
            "scanStale": False,
            "scanNeedsRescan": False,
            "setupRunId": None,
            "setupStatus": None,
            "setupBlocked": False,
            "published": False,
            "source": "none",
        },
        "article_setup_state": {
            "repo": "",
            "githubRepo": "",
            "scanStatus": None,
            "scanRunId": None,
            "scanStale": False,
            "scanNeedsRescan": False,
            "setupRunId": None,
            "setupStatus": None,
            "setupBlocked": False,
            "published": False,
            "source": "none",
        },
        "latestRuns": [],
        "latestRunsByWorkflow": {},
        "topicCandidates": [],
        "topicPillars": [],
        "hiddenTopicCandidates": [],
        "declinedTopicFeedback": [],
        "draftArticles": [],
        "writtenTopics": [],
        "publishEvidence": {},
        "guidedSteps": guided_steps,
        "currentGuidedStep": current_guided_step,
        "recommendedNextAction": {"key": "websiteProfile", "label": "Save website profile"},
        "workflowProgress": _workflow_progress(checks=checks),
        "hasCompletedArticleFlow": False,
        "startPageMode": "first_article_setup",
    }


def _timed_vibe_response(payload, *, started_at, metric_name, view=None, response_status=status.HTTP_200_OK):
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    payload_bytes = 0
    try:
        payload_bytes = len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        payload_bytes = 0
    logger.info(
        "vibe_marketing_response metric=%s view=%s status=%s bytes=%s duration_ms=%.1f",
        metric_name,
        view or "",
        response_status,
        payload_bytes,
        elapsed_ms,
    )
    headers = {
        "Server-Timing": f"{metric_name};dur={elapsed_ms:.1f}",
        "X-MLAI-Serialize-MS": f"{elapsed_ms:.1f}",
    }
    if view:
        headers["X-MLAI-View"] = str(view)
    if payload_bytes:
        headers["X-MLAI-Payload-Bytes"] = str(payload_bytes)
    return Response(payload, status=response_status, headers=headers)


def _setup_blocked_response_for_generation(context, config):
    if not (config and config.github_connection_state == "connected" and config.github_repo):
        return None
    latest_runs = _latest_runs_for_org(context.organization)
    checks = _profile_checks(
        context.organization,
        config,
        latest_runs,
        _latest_baseline_snapshot(context.organization),
    )
    scaffold_check = checks.get("scaffold", {})
    if not scaffold_check.get("setupBlocked"):
        return None
    return Response(
        {
            "detail": "Finish approving, merging, and verifying the articles directory setup before starting topic research or article generation.",
            "code": "article_system_setup_blocked",
            "check": "scaffold",
            "scaffold": scaffold_check,
        },
        status=status.HTTP_409_CONFLICT,
    )


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
        ("articleSystem", "Prepare articles location", "scaffold"),
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
        "preview_failed": ContentFactoryRunStatus.BLOCKED,
        "fallback_ready": ContentFactoryRunStatus.BLOCKED,
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


def _status_poll_unavailable_payload(*, workflow, technical_error="", status_code=None, response_payload=None, diagnostics=None):
    payload_diagnostics = {
        "technical_error": str(technical_error or ""),
        "retryable": True,
        "status_poll_unavailable": True,
    }
    if isinstance(diagnostics, dict):
        payload_diagnostics.update(diagnostics)
    if status_code is not None:
        payload_diagnostics["content_factory_status_code"] = status_code
    if response_payload is not None:
        payload_diagnostics["content_factory_response"] = response_payload
    return {
        "statusPollUnavailable": True,
        "error": str(technical_error or "Content Factory status polling is temporarily unavailable."),
        "errors": [str(technical_error or "Content Factory status polling is temporarily unavailable.")],
        "diagnostics": payload_diagnostics,
        "retryable": True,
        "workflow": workflow,
    }


def _is_status_poll_unavailable_payload(remote_data):
    return isinstance(remote_data, dict) and remote_data.get("statusPollUnavailable") is True


def _is_status_poll_transport_error(value):
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "httpconnectionpool",
            "httpsconnectionpool",
            "read timed out",
            "connect timed out",
            "connection aborted",
            "connection reset",
            "max retries exceeded",
            "status polling is temporarily unavailable",
        )
    )


def _article_run_has_completed_local_artifacts(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return False
    result = run.result or {}
    if str(result.get("status") or "").strip().lower() == ContentFactoryRunStatus.COMPLETED:
        return True
    if _content_package_from_run(run):
        return True
    if _component_manifest_from_run(run) and _live_preview_from_run(run).get("previewUrl"):
        return True
    if _local_publish_child_for_run(run):
        return True
    source_run = _accepted_revision_source_run(run)
    if _source_accepts_revision(source_run, run):
        return True
    return run.steps.filter(
        step_key__in=("finalize", "ready_for_review"),
        status=ContentFactoryStepStatus.COMPLETED,
    ).exists()


def _heal_stale_status_poll_timeout(run):
    if not run or run.workflow not in ARTICLE_WORKFLOWS:
        return run
    if run.status != ContentFactoryRunStatus.BLOCKED:
        return run
    result = run.result or {}
    result_errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    timeout_text = run.error or result.get("error") or " ".join(str(error) for error in result_errors)
    if not _is_status_poll_transport_error(timeout_text):
        return run
    if not _article_run_has_completed_local_artifacts(run):
        return run

    cleaned_result = dict(result)
    if _is_status_poll_transport_error(cleaned_result.get("error")):
        cleaned_result.pop("error", None)
    if result_errors and all(_is_status_poll_transport_error(error) for error in result_errors):
        cleaned_result.pop("errors", None)
    cleaned_result["status"] = ContentFactoryRunStatus.COMPLETED

    run.status = ContentFactoryRunStatus.COMPLETED
    run.error = ""
    run.result = cleaned_result
    if not run.current_step:
        run.current_step = "finalize"
    run.save(update_fields=["status", "error", "result", "current_step", "updated_at"])
    logger.info(
        "content_factory_status_poll_timeout_healed run_id=%s workflow=%s",
        run.run_id,
        run.workflow,
    )
    return run


def _block_startup_autofill_on_status_poll_unavailable(run, remote_data):
    if not run or run.workflow != "startup_autofill":
        return run
    payload = _blocked_worker_payload(
        workflow=run.workflow,
        technical_error=str((remote_data or {}).get("error") or ""),
        diagnostics=(remote_data or {}).get("diagnostics") if isinstance(remote_data, dict) else None,
    )
    result = dict(run.result or {})
    result.update(
        {
            "error": payload["error"],
            "errors": payload["errors"],
            "diagnostics": payload["diagnostics"],
            "retryable": payload["retryable"],
        }
    )
    run.status = ContentFactoryRunStatus.BLOCKED
    run.error = payload["error"]
    run.result = result
    run.save(update_fields=["status", "error", "result", "updated_at"])
    return run


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
        "error_code",
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
        "publish_child_status",
        "publish_child_recoverable",
        "publish_child_wait_reason",
        "publish_handoff_status",
        "source_run_id",
        "sourceRunId",
        "review_source_run_id",
        "reviewSourceRunId",
        "repaired",
        "repair_requested",
        "previous_status",
        "stale",
        "stale_reason",
        "retry_available",
        "queue_name",
        "queued_at",
        "scan_queue",
        "setup_queue",
        "scan_purpose",
        "setup_run_id",
        "setupRunId",
        "scaffold_job_id",
        "scaffold_status",
        "article_system_setup",
        "pending_article_system_setup",
        "requested_action",
        "setup_requested_action",
        "content_island",
        "contentIsland",
        "content_island_slug",
        "contentIslandSlug",
        "content_island_name",
        "contentIslandName",
        "content_island_keyword",
        "contentIslandKeyword",
        "content_island_icon_key",
        "contentIslandIconKey",
        "content_island_color_key",
        "contentIslandColorKey",
        "article_surface_mode",
        "article_surface_hint",
        "article_surface_hint_status",
        "article_surface_url",
        "article_surface_resolution",
        "matched_article_surface",
        "detected_candidates",
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


def _preview_payload_is_terminal_failure(payload):
    if not isinstance(payload, dict) or not payload:
        return False
    statuses = _live_preview_statuses(payload)
    return bool(statuses.intersection(LIVE_PREVIEW_FAILURE_STATUSES) or payload.get("error"))


def _result_has_current_failure(result):
    if not isinstance(result, dict) or not result:
        return False
    setup = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    status = str(result.get("status") or setup.get("status") or "").strip().lower()
    return bool(
        result.get("error")
        or result.get("error_code")
        or result.get("errors")
        or setup.get("error")
        or setup.get("error_code")
        or status in {"failed", "blocked", "preview_failed"}
    )


def _terminal_article_system_setup_has_local_failure(run) -> bool:
    if not run or run.workflow not in ARTICLE_SYSTEM_SETUP_WORKFLOWS:
        return False
    result = run.result if isinstance(run.result, dict) else {}
    result_failure = _result_has_current_failure(result)
    current_step = str(run.current_step or "").strip().lower()
    terminal_status = run.status in FAILED_RUN_STATUSES or current_step in {"preview_failed", "fallback_ready"}
    return bool((terminal_status or result_failure) and (run.error or result_failure))


def _merge_preserved_live_preview(local_result, remote_result):
    if not isinstance(remote_result, dict):
        return remote_result
    if _result_has_current_failure(remote_result):
        return remote_result
    local_preview = _preview_payload_from_result(local_result)
    if _empty_preview_payload(local_preview):
        return remote_result
    remote_preview = _preview_payload_from_result(remote_result)
    if not _empty_preview_payload(remote_preview):
        return remote_result
    if _preview_payload_is_terminal_failure(local_preview):
        return remote_result
    merged = dict(remote_result)
    merged["livePreview"] = local_preview
    merged.pop("live_preview", None)
    return merged


def _clear_article_system_setup_retry_state(result):
    cleaned = dict(result or {})
    for key in ("livePreview", "live_preview", "error", "error_code", "errors", "stale", "stale_reason"):
        cleaned.pop(key, None)
    if str(cleaned.get("status") or "").strip().lower() in {"failed", "blocked", "preview_failed"}:
        cleaned["status"] = "queued"
    setup_payload = cleaned.get("article_system_setup") if isinstance(cleaned.get("article_system_setup"), dict) else {}
    if setup_payload:
        setup_payload = dict(setup_payload)
        for key in ("error", "error_code", "livePreview", "live_preview"):
            setup_payload.pop(key, None)
        if str(setup_payload.get("status") or "").strip().lower() in {"failed", "blocked", "preview_failed"}:
            setup_payload["status"] = "queued"
        setup_payload["retry_available"] = False
        cleaned["article_system_setup"] = setup_payload
    return cleaned


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
        logger.warning(
            "content_factory_status_poll_unavailable run_id=%s workflow=%s reason=request_exception error=%s",
            run_id,
            workflow,
            exc,
        )
        return _status_poll_unavailable_payload(
            workflow=workflow,
            technical_error=str(exc),
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
        )

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
    if response.status_code >= 500:
        logger.warning(
            "content_factory_status_poll_unavailable run_id=%s workflow=%s status_code=%s",
            run_id,
            workflow,
            response.status_code,
        )
        return _status_poll_unavailable_payload(
            workflow=workflow,
            technical_error=str(detail or f"Content Factory returned {response.status_code}."),
            status_code=response.status_code,
            response_payload=response_payload,
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow=workflow),
        )
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

    original_snapshot = {
        "workflow": run.workflow,
        "status": run.status,
        "current_step": run.current_step,
        "artifact_root": run.artifact_root,
        "step_order": list(run.step_order or []),
        "acceptance_summary": copy.deepcopy(run.acceptance_summary),
        "verification_summary": copy.deepcopy(run.verification_summary),
        "approval_state": run.approval_state,
        "resume_available": run.resume_available,
        "result": copy.deepcopy(run.result),
        "error": run.error,
    }
    result = _run_result_from_remote(remote_data)
    remote_status = _normalize_remote_run_status(remote_data.get("status") or result.get("status") or run.status)
    if (
        run.workflow in SCAN_WORKFLOWS
        and run.status in SCAN_LOCAL_AUTHORITATIVE_STATUSES
        and remote_status in RUNNING_RUN_STATUSES
    ):
        logger.info(
            "content_factory_scan_status_poll_preserved_local_terminal_state "
            "run_id=%s workflow=%s local_status=%s remote_status=%s remote_current_step=%s",
            run.run_id,
            run.workflow,
            run.status,
            remote_status,
            remote_data.get("current_step") or remote_data.get("step") or "",
        )
        return run
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
        if run.workflow in SCAN_WORKFLOWS:
            local_result = run.result if isinstance(run.result, dict) else {}
            run_request = run.run_request if isinstance(run.run_request, dict) else {}

            def preserve_from_local_or_request(result_key, *request_keys):
                if result.get(result_key) not in (None, "", {}, []):
                    return
                local_value = local_result.get(result_key)
                if local_value not in (None, "", {}, []):
                    result[result_key] = local_value
                    return
                for request_key in request_keys or (result_key,):
                    request_value = run_request.get(request_key)
                    if request_value not in (None, "", {}, []):
                        result[result_key] = request_value
                        return

            preserve_from_local_or_request("scan_purpose", "scan_purpose", "scanPurpose")
            preserve_from_local_or_request("scanPurpose", "scanPurpose", "scan_purpose")
            preserve_from_local_or_request("article_surface_mode", "article_surface_mode", "articleSurfaceMode")
            preserve_from_local_or_request("articleSurfaceMode", "articleSurfaceMode", "article_surface_mode")
            for key in (
                "article_surface_hint",
                "articleSurfaceHint",
                "article_surface_hint_status",
                "article_system_readiness",
                "matched_article_surface",
                "detected_candidates",
                "scaffold_status",
            ):
                preserve_from_local_or_request(key)

            request_compact = {
                key: run_request.get(key)
                for key in ("scan_purpose", "scanPurpose", "article_surface_mode", "articleSurfaceMode")
                if run_request.get(key) not in (None, "", [], {})
            }
            if request_compact and result.get("run_request") in (None, "", {}, []):
                result["run_request"] = request_compact
        if run.workflow == "article_system_setup" and isinstance(run.result, dict):
            for key in (
                "scan_purpose",
                "setup_run_id",
                "setupRunId",
                "error_code",
                "stale",
                "stale_reason",
                "retry_available",
                "retryable",
                "queue_name",
                "queued_at",
                "setup_queue",
                "article_surface_mode",
                "article_surface_hint",
                "article_system_setup",
                "pending_article_system_setup",
            ):
                if result.get(key) in (None, "", {}, []) and run.result.get(key) not in (None, "", {}, []):
                    result[key] = run.result[key]
        run.result = result
    _sync_steps_from_remote(run, remote_data)
    next_snapshot = {
        "workflow": run.workflow,
        "status": run.status,
        "current_step": run.current_step,
        "artifact_root": run.artifact_root,
        "step_order": list(run.step_order or []),
        "acceptance_summary": run.acceptance_summary,
        "verification_summary": run.verification_summary,
        "approval_state": run.approval_state,
        "resume_available": run.resume_available,
        "result": run.result,
        "error": run.error,
    }
    if next_snapshot == original_snapshot:
        return run
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


def _run_start_payload(run):
    result = run.result if isinstance(run.result, dict) else {}
    error = str(run.error or result.get("error") or result.get("message") or "").strip()
    raw_errors = result.get("errors")
    errors = [str(item) for item in raw_errors if item] if isinstance(raw_errors, list) else []
    if error and error not in errors:
        errors.insert(0, error)
    payload = {
        "run_id": run.run_id,
        "runId": run.run_id,
        "status": run.status,
    }
    if error:
        payload["error"] = error
    if errors:
        payload["errors"] = errors
    if result.get("retryable") is not None:
        payload["retryable"] = bool(result.get("retryable"))
    if isinstance(result.get("diagnostics"), dict):
        payload["diagnostics"] = result["diagnostics"]
    return payload


def _autofill_start_payload(run):
    result = run.result if isinstance(run.result, dict) else {}
    error = str(run.error or result.get("error") or result.get("message") or "").strip()
    raw_errors = result.get("errors")
    errors = [str(item) for item in raw_errors if item] if isinstance(raw_errors, list) else []
    if error and error not in errors:
        errors.insert(0, error)
    return {
        "run_id": run.run_id,
        "runId": run.run_id,
        "status": run.status,
        "error": error,
        "errors": errors,
    }


def _autofill_start_response(run, *, reused_active_run=False):
    payload = _autofill_start_payload(run)
    logger.info(
        "vibe_marketing_autofill_start_response run_id=%s domain=%s workflow=%s status=%s reused_active_run=%s has_error=%s",
        run.run_id,
        run.domain,
        run.workflow,
        run.status,
        bool(reused_active_run),
        bool(payload["error"] or payload["errors"]),
    )
    return Response(payload, status=status.HTTP_202_ACCEPTED)


def _call_content_factory_run_action(
    *,
    run_id,
    action,
    payload,
    workflow="article_generation",
    timeout=(3, 15),
    transport_errors_are_pending=False,
):
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
            timeout=timeout,
        )
    except http_client.RequestException as exc:
        if transport_errors_are_pending:
            return {
                "status": "action_pending",
                "action": action,
                "error": str(exc),
                "errors": [str(exc)],
                "content_factory_transport_error": True,
                "retryable": True,
                "diagnostics": _content_factory_diagnostics(
                    remote_config,
                    run_id=run_id,
                    workflow=workflow,
                    action=action,
                ),
            }
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


def _content_factory_action_transport_pending(remote_data):
    return isinstance(remote_data, dict) and bool(remote_data.get("content_factory_transport_error"))


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


def _call_content_factory_article_system_revision(*, run_id, payload):
    remote_config = _content_factory_remote_config()
    if not remote_config["enabled"]:
        technical_error = _content_factory_unavailable_message(remote_config)
        logger.warning("content_factory_article_system_revision_blocked run_id=%s reason=%s", run_id, technical_error)
        return _blocked_worker_payload(
            workflow="article_system_setup",
            technical_error=technical_error,
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow="article_system_setup"),
            retryable=True,
        )

    try:
        response = http_client.post(
            f"{remote_config['base_url']}/api/runs/{run_id}/article-system-revisions",
            json=payload or {},
            headers=_content_factory_headers(),
            timeout=(5, 90),
        )
    except http_client.RequestException as exc:
        return _blocked_worker_payload(
            workflow="article_system_setup",
            technical_error=str(exc),
            diagnostics=_content_factory_diagnostics(remote_config, run_id=run_id, workflow="article_system_setup"),
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
        started_at = time.perf_counter()
        view = "summary" if str(request.query_params.get("view") or "").strip().lower() == "summary" else "full"
        profile = get_or_create_founder_profile(request.user)
        company = resolve_active_company(profile)
        if company is None:
            return Response(
                {"detail": "Create or select a founder company first.", "redirect": "/founder-tools/company-setup"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not normalize_company_domain(company.domain):
            return _timed_vibe_response(
                _serialize_bootstrap_without_domain(company),
                started_at=started_at,
                metric_name="vibe_bootstrap",
                view=view,
            )
        context, error_response = _resolve_context_or_response(request, require_domain=True)
        if error_response:
            return error_response
        payload = _serialize_bootstrap(context, request=request, view=view)
        return _timed_vibe_response(payload, started_at=started_at, metric_name="vibe_bootstrap", view=view)


class VibeMarketingCompanyAvatarView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    max_avatar_size_bytes = 10 * 1024 * 1024

    def post(self, request):
        if not request.user or not request.user.is_authenticated:
            return Response({"detail": "Authentication credentials were not provided."}, status=status.HTTP_401_UNAUTHORIZED)

        profile = get_or_create_founder_profile(request.user)
        company = resolve_active_company(profile)
        if company is None:
            return Response(
                {"detail": "Create or select a founder company first.", "redirect": "/founder-tools/company-setup"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        avatar_file = request.FILES.get("avatar")
        if not avatar_file:
            return Response({"detail": "Upload an avatar image."}, status=status.HTTP_400_BAD_REQUEST)
        if getattr(avatar_file, "size", 0) > self.max_avatar_size_bytes:
            return Response({"detail": "Avatar image must be 10MB or smaller."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError:
            logger.exception("Pillow is unavailable for company avatar upload")
            return Response({"detail": "Avatar upload is temporarily unavailable."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            image = Image.open(avatar_file)
            image = ImageOps.exif_transpose(image)
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            image.thumbnail((512, 512), resampling)
            if image.mode in {"RGBA", "LA", "P"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode == "P":
                    image = image.convert("RGBA")
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                background.paste(image.convert("RGB"), mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            output_buffer = BytesIO()
            image.save(output_buffer, format="JPEG", quality=90, optimize=True)
            output_buffer.seek(0)
        except (UnidentifiedImageError, OSError, ValueError):
            return Response({"detail": "Upload a valid image file."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from core.firebase_utils import upload_file_to_storage

            destination_path = f"company-avatars/{company.id}_{int(timezone.now().timestamp())}.jpg"
            avatar_url = upload_file_to_storage(output_buffer, destination_path, content_type="image/jpeg")
        except Exception:
            logger.exception("company_avatar_upload_failed company_id=%s user_id=%s", company.id, request.user.id)
            return Response({"detail": "Avatar upload failed. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)

        company.avatar_url = avatar_url
        company.save(update_fields=["avatar_url", "updated_at"])

        try:
            if not normalize_company_domain(company.domain):
                return Response(_serialize_bootstrap_without_domain(company), status=status.HTTP_200_OK)
            context, error_response = _resolve_context_or_response(request, require_domain=True)
            if error_response:
                return error_response
            return Response(_serialize_bootstrap(context, request=request, view="summary"), status=status.HTTP_200_OK)
        except Exception:
            logger.exception("company_avatar_bootstrap_refresh_failed company_id=%s user_id=%s", company.id, request.user.id)
            return Response(
                {
                    "company": {
                        "id": str(company.id),
                        "name": company.name,
                        "domain": company.domain,
                        "avatarUrl": company.avatar_url,
                        "avatar_url": company.avatar_url,
                    }
                },
                status=status.HTTP_200_OK,
            )


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

        with transaction.atomic():
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
        active_run = _active_startup_autofill_run_for_domain(organization.domain)
        if active_run is not None:
            return _autofill_start_response(active_run, reused_active_run=True)

        startup_profile = _autofill_startup_profile_payload(organization)
        profile_fields = _autofill_profile_fields_payload(startup_profile)
        profile_fields["location"] = company.location
        profile_fields["abn"] = company.abn or ""
        existing_fields = {
            "brandName": config.brand_name or organization.name,
            "companyContext": config.company_context or "",
            "competitors": _camel_list(organization.competitors),
            "seedKeywords": _camel_list(organization.seed_keywords),
            "companyLinkedInUrl": organization.company_linkedin_url,
            "profileFields": profile_fields,
        }
        payload = {
            "domain": organization.domain,
            "company_id": str(company.id),
            "organization_id": str(organization.id),
            "company_name": company.name,
            "brand_name": config.brand_name or organization.name,
            "company_linkedin_url": organization.company_linkedin_url,
            "location": company.location,
            "abn": company.abn,
            "existing_fields": existing_fields,
            "startup_profile": startup_profile,
            "research_depth": "deep",
            "strict_deep_research": True,
            "min_direct_competitors": 3,
            "min_seed_keywords": 8,
            "target_seed_keywords": 12,
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
        return _autofill_start_response(run, reused_active_run=False)


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
        force_reconnect = _bool_from_request(
            _request_value(request.data, "force_reconnect", "forceReconnect", default=False)
        )
        requested_repo = _clean_github_repo(request.data.get("github_repo") or request.data.get("githubRepo"))
        if requested_repo and not force_reconnect:
            config.github_repo = requested_repo
            config_update_fields.append("github_repo")
        config_update_fields.append("updated_at")
        config.save(update_fields=list(dict.fromkeys(config_update_fields)))

        if not force_reconnect:
            existing_connection = _connect_with_existing_github_credentials(
                config,
                domain=context.organization.domain,
                actor_id=actor_id,
                requested_repo=requested_repo,
            )
            if existing_connection:
                return Response(existing_connection, status=status.HTTP_200_OK)

        try:
            auth_url = build_github_auth_url(actor_id, domain=context.organization.domain, request=request)
        except Exception as exc:
            logger.exception(
                "vibe_marketing_github_auth_url_failed domain=%s actor_id=%s",
                context.organization.domain,
                actor_id,
            )
            return Response(
                {
                    "status": "auth_unavailable",
                    "connection_state": "auth_required",
                    "github_repo": config.github_repo,
                    "detail": "GitHub authorization could not be opened. Check GitHub App configuration.",
                    "error": "github_auth_url_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "status": "auth_required",
                "connection_state": "auth_required",
                "github_repo": config.github_repo,
                "auth_url": auth_url,
            },
            status=status.HTTP_200_OK,
        )


class VibeMarketingGitHubReposView(APIView):
    def get(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response

        config = _get_config(context.organization)
        actor_id = founder_actor_id_for_user(request.user)
        token = ""
        installation_id = str(config.github_installation_id or "").strip()
        credential_source = "none"
        connection_state = config.github_connection_state or "auth_required"
        error_message = ""

        if config.github_token_encrypted:
            try:
                token = ensure_valid_org_token(context.organization.domain)
                config.refresh_from_db()
                installation_id = str(config.github_installation_id or installation_id or "").strip()
                credential_source = "org"
                connection_state = "connected"
            except (ArticleGenerationError, TokenRefreshError) as exc:
                error_message = str(exc)

        if not token:
            integration = UserIntegration.objects.filter(slack_user_id=actor_id).first()
            if integration and integration.github_access_token:
                try:
                    token = ensure_valid_token(actor_id)
                    installation_id = str(integration.github_installation_id or installation_id or "").strip()
                    credential_source = "user"
                    connection_state = "connected"
                except (ScanError, TokenRefreshError) as exc:
                    error_message = str(exc)

        if not token:
            return Response(
                {
                    "status": "auth_required",
                    "connectionState": connection_state,
                    "connection_state": connection_state,
                    "githubRepo": config.github_repo,
                    "github_repo": config.github_repo,
                    "selectedRepo": config.github_repo,
                    "selected_repo": config.github_repo,
                    "repos": [],
                    "repositories": [],
                    "error": error_message,
                },
                status=status.HTTP_200_OK,
            )

        try:
            repos = _list_github_repositories_for_token(token=token, installation_id=installation_id)
        except Exception as exc:
            logger.warning(
                "vibe_marketing_github_repo_list_failed domain=%s source=%s installation_id=%s error=%s",
                context.organization.domain,
                credential_source,
                installation_id,
                exc,
            )
            return Response(
                {
                    "status": "unavailable",
                    "connectionState": connection_state,
                    "connection_state": connection_state,
                    "githubRepo": config.github_repo,
                    "github_repo": config.github_repo,
                    "selectedRepo": config.github_repo,
                    "selected_repo": config.github_repo,
                    "repos": [],
                    "repositories": [],
                    "error": str(exc),
                },
                status=status.HTTP_200_OK,
            )

        selected_repo = str(config.github_repo or "").strip()
        if not selected_repo and len(repos) == 1:
            selected_repo = str(repos[0].get("fullName") or "").strip()
        return Response(
            {
                "status": "connected",
                "connectionState": connection_state,
                "connection_state": connection_state,
                "credentialSource": credential_source,
                "credential_source": credential_source,
                "githubRepo": config.github_repo,
                "github_repo": config.github_repo,
                "selectedRepo": selected_repo,
                "selected_repo": selected_repo,
                "installationId": installation_id,
                "installation_id": installation_id,
                "repos": repos,
                "repositories": repos,
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
        explicit_scan_purpose = _request_value(request.data, "scanPurpose", "scan_purpose", default=None)
        scan_purpose = str(
            explicit_scan_purpose
            or ("setup" if _request_value(request.data, "articleSurfaceUrl", "article_surface_url", default="") else "inventory")
        ).strip().lower()
        if scan_purpose not in {"inventory", "setup"}:
            return Response({"detail": "scanPurpose must be inventory or setup."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            article_surface_hint, article_surface_mode, article_surface_url = _article_surface_hint_from_request(
                request.data,
                domain=context.organization.domain,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        auto_setup_preview = _bool_from_request(
            _request_value(request.data, "autoSetupPreview", "auto_setup_preview", default=False)
        )
        if auto_setup_preview and not article_surface_hint:
            return Response(
                {"detail": "Article/blog URL or path is required before drafting an articles setup preview."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if scan_purpose == "setup" and not article_surface_hint:
            return Response(
                {"detail": "Choose or create an article/blog route before generating the articles setup."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scaffold_if_missing = scan_purpose == "setup"
        payload = {
            "domain": context.organization.domain,
            "github_repo": config.github_repo,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "scaffold_if_missing": scaffold_if_missing,
            "auto_setup_preview": auto_setup_preview,
            "generate_components": True,
            "article_surface_mode": article_surface_mode,
            "scan_purpose": scan_purpose,
        }
        if article_surface_url:
            payload["article_surface_url"] = article_surface_url
        if article_surface_hint:
            payload["article_surface_hint"] = article_surface_hint
        superseded_scan_run_ids = _supersede_stale_scan_runs(context=context, request_user=request.user)
        run = _queue_content_factory_run(
            endpoint="scan",
            workflow="repo_scan",
            context=context,
            config=config,
            payload=payload,
        )
        result = run.result or {}
        result["scan_purpose"] = scan_purpose
        result["article_surface_mode"] = article_surface_mode
        if article_surface_hint:
            result["article_surface_hint"] = article_surface_hint
        run.result = result
        run.save(update_fields=["result", "updated_at"])
        if superseded_scan_run_ids:
            result = run.result or {}
            result["superseded_scan_run_ids"] = superseded_scan_run_ids
            run.result = result
            run.save(update_fields=["result", "updated_at"])
        if scan_purpose == "setup" and article_surface_hint:
            route_path = str(article_surface_hint.get("route_path") or article_surface_url or "").strip()
            source_scan_run_id = str(
                _request_value(request.data, "sourceScanRunId", "source_scan_run_id", default="") or ""
            ).strip() or run.run_id
            pending = _store_pending_article_system_setup(
                config,
                mode=article_surface_mode,
                route_path=route_path,
                source_scan_run_id=source_scan_run_id,
                article_surface_hint=article_surface_hint,
            )
            result = run.result or {}
            result["pending_article_system_setup"] = pending
            run.result = result
            run.save(update_fields=["result", "updated_at"])
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingArticleSystemSetupView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        if request.data.get("github_repo") or request.data.get("githubRepo"):
            config.github_repo = request.data.get("github_repo") or request.data.get("githubRepo")
            config.save(update_fields=["github_repo", "updated_at"])
        try:
            article_surface_hint, article_surface_mode, article_surface_url = _article_surface_hint_from_request(
                request.data,
                domain=context.organization.domain,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if not article_surface_hint:
            return Response(
                {"detail": "Choose or create an article/blog route before building the articles setup preview."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        route_path = str(article_surface_hint.get("route_path") or article_surface_url or "").strip()
        pending = _store_pending_article_system_setup(
            config,
            mode=article_surface_mode,
            route_path=route_path,
            source_scan_run_id=str(_request_value(request.data, "sourceScanRunId", "source_scan_run_id", default="") or ""),
            article_surface_hint=article_surface_hint,
        )
        payload = {
            "domain": context.organization.domain,
            "github_repo": config.github_repo,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "requested_by_slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "scan_purpose": "setup",
            "source_scan_run_id": pending.get("source_scan_run_id") or "",
            "scan_run_id": pending.get("source_scan_run_id") or "",
            "article_surface_mode": article_surface_mode,
            "article_surface_hint": article_surface_hint,
        }
        run = _queue_content_factory_run(
            endpoint="article-system-setup",
            workflow="article_system_setup",
            context=context,
            config=config,
            payload=payload,
        )
        result = run.result or {}
        result["scan_purpose"] = "setup"
        result["article_surface_mode"] = article_surface_mode
        result["article_surface_hint"] = article_surface_hint
        result["pending_article_system_setup"] = pending
        result.setdefault("setup_run_id", run.run_id)
        setup_payload = dict(result.get("article_system_setup") or {})
        setup_payload.setdefault("setup_run_id", result["setup_run_id"])
        setup_payload.setdefault("status", run.status)
        setup_payload["requested_action"] = None
        result["article_system_setup"] = setup_payload
        run.result = result
        run.save(update_fields=["result", "updated_at"])
        return Response(
            {
                "run_id": run.run_id,
                "runId": run.run_id,
                "setup_run_id": result["setup_run_id"],
                "setupRunId": result["setup_run_id"],
                "status": run.status,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VibeMarketingDiscoveryView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        blocked_response = _setup_blocked_response_for_generation(context, config)
        if blocked_response:
            return blocked_response
        payload = {
            "domain": context.organization.domain,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        content_island_slug = str(
            _request_value(request.data, "contentIslandSlug", "content_island_slug", default="") or ""
        ).strip()
        if content_island_slug:
            content_island_name = str(
                _request_value(request.data, "contentIslandName", "content_island_name", default="") or ""
            ).strip()
            content_island_keyword = str(
                _request_value(request.data, "contentIslandKeyword", "content_island_keyword", default="") or ""
            ).strip()
            content_island_icon_key = str(
                _request_value(request.data, "contentIslandIconKey", "content_island_icon_key", default="") or ""
            ).strip()
            content_island_color_key = str(
                _request_value(request.data, "contentIslandColorKey", "content_island_color_key", default="") or ""
            ).strip()
            try:
                requested_topic_count = int(
                    _request_value(request.data, "requestedTopicCount", "requested_topic_count", default=4) or 4
                )
            except (TypeError, ValueError):
                requested_topic_count = 4
            payload.update(
                {
                    "content_island_slug": content_island_slug,
                    "content_island_name": content_island_name,
                    "content_island_keyword": content_island_keyword or content_island_name,
                    "content_island_icon_key": content_island_icon_key,
                    "content_island_color_key": content_island_color_key,
                    "requested_topic_count": max(1, min(requested_topic_count, 8)),
                }
            )
        run = _queue_content_factory_run(
            endpoint="discovery",
            workflow="auto_discovery",
            context=context,
            config=config,
            payload=payload,
        )
        response_payload = _run_start_payload(run)
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE if run.status == ContentFactoryRunStatus.BLOCKED else status.HTTP_202_ACCEPTED
        return Response(response_payload, status=response_status)


class VibeMarketingArticleView(APIView):
    def post(self, request):
        context, error_response = _resolve_context_or_response(request)
        if error_response:
            return error_response
        config = _get_config(context.organization)
        blocked_response = _setup_blocked_response_for_generation(context, config)
        if blocked_response:
            return blocked_response
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
        started_at = time.perf_counter()
        view = "status" if str(request.query_params.get("view") or "").strip().lower() == "status" else "full"
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        if run.workflow in ARTICLE_WORKFLOWS:
            _recover_publish_child_for_run(run, request=request, context=context)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
            run = _annotate_publish_handoff_staleness(run)
            run = _annotate_publish_child_state(run, context=context)
        skip_remote_status = bool(
            run.workflow in ARTICLE_WORKFLOWS
            and run.status == ContentFactoryRunStatus.COMPLETED
            and _article_run_has_completed_local_artifacts(run)
            and not _local_publish_child_for_run(run, context=context)
            and not _publish_handoff_pending_for_run(run)
        )
        if _terminal_article_system_setup_has_local_failure(run):
            skip_remote_status = True
        skipped_missing_publish_child = bool(run.workflow in ARTICLE_WORKFLOWS and _publish_child_missing_remote(run))
        if skipped_missing_publish_child:
            skip_remote_status = True
        remote_data = {} if skip_remote_status else _call_content_factory_run_status(run.run_id, workflow=run.workflow)
        if skip_remote_status:
            logger.info(
                "content_factory_status_poll_skipped run_id=%s workflow=%s status=%s reason=%s",
                run.run_id,
                run.workflow,
                run.status,
                "terminal_article_system_setup_failure"
                if _terminal_article_system_setup_has_local_failure(run)
                else "missing_publish_child_recoverable"
                if skipped_missing_publish_child
                else "terminal_article",
            )
        if _is_status_poll_unavailable_payload(remote_data):
            if run.workflow == "startup_autofill":
                run = _block_startup_autofill_on_status_poll_unavailable(run, remote_data)
            else:
                run = _heal_stale_status_poll_timeout(run)
            logger.warning(
                "content_factory_status_poll_preserved_local_state run_id=%s workflow=%s status=%s error=%s",
                run.run_id,
                run.workflow,
                run.status,
                remote_data.get("error"),
            )
        elif remote_data:
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
                _recover_publish_child_for_run(run, request=request, context=context)
                run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
        if run.workflow in ARTICLE_WORKFLOWS:
            run = _ensure_article_live_preview(run)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
            run = _annotate_publish_handoff_staleness(run)
            run = _annotate_publish_child_state(run, context=context)
        payload = _serialize_run(run, context=context, mode=view)
        if view == "status":
            _log_terminal_repo_scan_status(run, payload)
        return _timed_vibe_response(payload, started_at=started_at, metric_name="vibe_run", view=view)


class VibeMarketingArticleSystemRevisionsView(APIView):
    def post(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        if run.workflow != "article_system_setup":
            return Response({"detail": "Articles setup comments can only be sent for setup preview runs."}, status=status.HTTP_400_BAD_REQUEST)

        feedback_batch_id = str(
            _request_value(request.data, "feedbackBatchId", "feedback_batch_id", default="") or ""
        ).strip()
        raw_comments = request.data.get("comments") if hasattr(request.data, "get") else None
        explicit_remote_comments = []
        if isinstance(raw_comments, list):
            explicit_remote_comments = [
                payload
                for payload in (_article_system_remote_comment_from_request(item) for item in raw_comments)
                if payload
            ]
        body = str(
            _request_value(request.data, "body", "comment", "reviewComments", default="") or ""
        ).strip()

        draft_comments = list(
            VibeMarketingComponentComment.objects.filter(
                run=run,
                status=VibeMarketingComponentCommentStatus.DRAFT,
            )
            .order_by("created_at", "id")
        )
        draft_comments = [comment for comment in draft_comments if str(comment.body or "").strip()]
        retry_existing_batch = False
        submitted_local_comments = []
        if draft_comments:
            feedback_batch_id = feedback_batch_id or f"article-system-{uuid.uuid4().hex[:12]}"
            with transaction.atomic():
                VibeMarketingComponentComment.objects.filter(
                    id__in=[comment.id for comment in draft_comments],
                    status=VibeMarketingComponentCommentStatus.DRAFT,
                ).update(status=VibeMarketingComponentCommentStatus.SUBMITTED, batch_id=feedback_batch_id, updated_at=timezone.now())
                submitted_local_comments = list(
                    VibeMarketingComponentComment.objects.filter(id__in=[comment.id for comment in draft_comments])
                    .order_by("created_at", "id")
                )
            remote_comments = [_article_system_remote_comment_payload(comment) for comment in submitted_local_comments]
        elif explicit_remote_comments:
            feedback_batch_id = feedback_batch_id or f"article-system-{uuid.uuid4().hex[:12]}"
            remote_comments = explicit_remote_comments
        elif body:
            feedback_batch_id = feedback_batch_id or f"article-system-{uuid.uuid4().hex[:12]}"
            remote_comment = _article_system_remote_comment_from_request(
                {
                    "body": body,
                    "commentId": _request_value(request.data, "commentId", "comment_id", default=""),
                    "filePath": _request_value(request.data, "filePath", "file_path", default=""),
                    "selector": _request_value(request.data, "selector", default=""),
                    "anchor": request.data.get("anchor") if hasattr(request.data, "get") else None,
                    "context": request.data.get("context") if hasattr(request.data, "get") else None,
                }
            )
            remote_comments = [remote_comment] if remote_comment else []
        else:
            latest_submitted = (
                VibeMarketingComponentComment.objects.filter(
                    run=run,
                    status=VibeMarketingComponentCommentStatus.SUBMITTED,
                )
                .exclude(batch_id="")
                .order_by("-updated_at", "-created_at", "-id")
                .first()
            )
            if not latest_submitted:
                return Response({"detail": "Add at least one draft setup comment before requesting setup changes."}, status=status.HTTP_400_BAD_REQUEST)
            feedback_batch_id = latest_submitted.batch_id
            submitted_local_comments = list(
                VibeMarketingComponentComment.objects.filter(
                    run=run,
                    status=VibeMarketingComponentCommentStatus.SUBMITTED,
                    batch_id=feedback_batch_id,
                ).order_by("created_at", "id")
            )
            retry_existing_batch = True
            remote_comments = [_article_system_remote_comment_payload(comment) for comment in submitted_local_comments]

        if not feedback_batch_id or not remote_comments:
            return Response({"detail": "Add a review comment before requesting setup changes."}, status=status.HTTP_400_BAD_REQUEST)

        remote_payload = {
            "source_run_id": run.run_id,
            "feedback_batch_id": feedback_batch_id,
            "request_source": "founder_tools_article_system_feedback",
            "comments": remote_comments,
        }
        remote_data = _call_content_factory_article_system_revision(run_id=run.run_id, payload=remote_payload)
        if remote_data.get("error") and int(remote_data.get("content_factory_status_code") or 0) in {400, 404, 409, 422}:
            result = dict(run.result or {})
            result["latest_article_system_revision_response"] = remote_data
            result["component_feedback_latest_batch"] = {
                "id": feedback_batch_id,
                "sourceRunId": run.run_id,
                "status": "failed",
                "error": remote_data.get("error"),
                "retryable": bool(remote_data.get("retryable")),
            }
            run.result = result
            run.save(update_fields=["result", "updated_at"])
            return Response({"detail": remote_data["error"], "remote": remote_data}, status=status.HTTP_409_CONFLICT)
        if remote_data.get("error"):
            result = dict(run.result or {})
            retryable = bool(remote_data.get("retryable"))
            result["latest_article_system_revision_response"] = remote_data
            result["component_feedback_latest_batch"] = {
                "id": feedback_batch_id,
                "sourceRunId": run.run_id,
                "status": "submitted" if retryable else "failed",
                "error": remote_data.get("error"),
                "retryable": retryable,
            }
            run.result = result
            run.save(update_fields=["result", "updated_at"])
            if retryable:
                return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)
            return Response(
                {"detail": remote_data.get("error") or "Content Factory could not queue setup changes.", "remote": remote_data},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        result = dict(run.result or {})
        comments = list(result.get("article_system_review_comments") or [])
        submitted_at = timezone.now().isoformat()
        for comment in remote_comments:
            comments.append(
                {
                    "id": comment.get("comment_id"),
                    "feedbackBatchId": feedback_batch_id,
                    "body": comment.get("body"),
                    "selector": comment.get("selector"),
                    "anchor": comment.get("anchor"),
                    "context": comment.get("context"),
                    "submittedAt": submitted_at,
                }
            )
        result["article_system_review_comments"] = comments
        result["latest_article_system_revision_response"] = remote_data
        result["component_feedback_latest_batch"] = {
            "id": feedback_batch_id,
            "sourceRunId": run.run_id,
            "status": "running",
            "retry": retry_existing_batch,
        }
        if remote_data.get("livePreview") or remote_data.get("live_preview"):
            result["livePreview"] = remote_data.get("livePreview") or remote_data.get("live_preview")
        if remote_data.get("live_preview_url"):
            result["live_preview_url"] = remote_data.get("live_preview_url")
        run.result = result
        if run.status in {ContentFactoryRunStatus.AWAITING_APPROVAL, ContentFactoryRunStatus.APPROVAL_REQUIRED, ContentFactoryRunStatus.COMPLETED}:
            run.status = ContentFactoryRunStatus.RUNNING
            run.current_step = "revision_preview_building"
        run.save(update_fields=["status", "current_step", "result", "updated_at"])
        return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)


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
        if not _request_includes_comment_context(request.data or {}):
            payload["context"] = comment.context or {}
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
            "context",
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
        safe_path = quote(str(proxy_path or "").lstrip("/"), safe="/:@!$&'()*+,;=-._~")
        if _is_live_preview_client_runtime_request_path(safe_path):
            return _empty_live_preview_client_runtime_response(include_body=request.method != "HEAD")
        remote_config = _content_factory_remote_config()
        if not remote_config["enabled"]:
            return Response({"detail": _content_factory_unavailable_message(remote_config)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

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

        content_type = response.headers.get("Content-Type") or "application/octet-stream"
        response_body = response.content if request.method != "HEAD" else b""
        response_body = _rewrite_live_preview_proxy_body(run_id, response_body, content_type)
        django_response = HttpResponse(
            response_body,
            status=response.status_code,
            content_type=content_type,
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


@method_decorator(xframe_options_exempt, name="dispatch")
class VibeMarketingRunLivePreviewResourceView(APIView):
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

    def _proxy(self, request, run_id):
        _context, _run, error_response = self._resolve_run(request, run_id)
        if error_response is not None:
            return error_response
        resource_url = str(request.query_params.get("url") or "").strip()
        if not _is_allowed_live_preview_resource_url(resource_url):
            return HttpResponse(
                "External preview resource is not allowed.",
                status=403,
                content_type="text/plain; charset=utf-8",
            )
        remote_config = _content_factory_remote_config()
        if not remote_config["enabled"]:
            return Response({"detail": _content_factory_unavailable_message(remote_config)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        remote_url = (
            f"{remote_config['base_url']}/api/runs/{run_id}/live-preview/resource"
            f"?{urlencode({'url': resource_url})}"
        )
        forwarded_headers = {
            "Accept": request.headers.get("Accept", "*/*"),
            "User-Agent": request.headers.get("User-Agent", "mlai-backend-live-preview-resource-proxy"),
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
                f"Preview resource proxy failed: {exc}",
                status=502,
                content_type="text/plain; charset=utf-8",
            )
        content_type = response.headers.get("Content-Type") or "application/octet-stream"
        django_response = HttpResponse(
            response.content if request.method != "HEAD" else b"",
            status=response.status_code,
            content_type=content_type,
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

    def get(self, request, run_id):
        return self._proxy(request, run_id)

    def head(self, request, run_id):
        return self._proxy(request, run_id)


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
            if run.workflow in SCAN_WORKFLOWS:
                remote_data = _call_content_factory_run_action(
                    run_id=run_id,
                    action=action,
                    payload=payload,
                    workflow=run.workflow,
                    timeout=(2, 8),
                    transport_errors_are_pending=True,
                )
                run = _cancel_local_scan_run(run=run, remote_data=remote_data)
                run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
                return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)

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

        if action == "restart":
            restarted_run, restart_error = _restart_article_run(run=run, context=context)
            if restart_error:
                return restart_error
            return Response(_serialize_run(restarted_run, context=context), status=status.HTTP_202_ACCEPTED)

        if action == "enable-daily-automation":
            config = _get_config(context.organization)
            _assign_config_actor(config, request.user)
            if request.data.get("default_timezone") or request.data.get("defaultTimezone"):
                config.default_timezone = request.data.get("default_timezone") or request.data.get("defaultTimezone")
            config.daily_discovery_enabled = True
            checks = _profile_checks(
                context.organization,
                config,
                _latest_runs_for_org(context.organization),
                _latest_baseline_snapshot(context.organization),
            )
            if not checks["dailyAutomation"]["passed"]:
                return Response(
                    {"detail": "Daily generation prerequisites are not complete.", "checks": checks},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            config.save(update_fields=["daily_discovery_enabled", "default_timezone", "updated_at"])
            result = run.result or {}
            result["daily_automation_enabled_at"] = timezone.now().isoformat()
            result["daily_automation_timezone"] = config.default_timezone
            run.result = result
            run.save(update_fields=["result", "updated_at"])
            return Response(_serialize_run(run, context=context), status=status.HTTP_200_OK)

        if action == "merge-publish-pr":
            merged_run, merge_error = _merge_publish_pr_for_run(run=run, context=context)
            if merge_error:
                return merge_error
            return Response(_serialize_run(merged_run or run, context=context), status=status.HTTP_200_OK)

        remote_run = run
        if action in {"promote-bundle", "publish-pr"}:
            publish_child_source = _publish_source_run_for_child(run, context)
            if publish_child_source is not None:
                remote_run = publish_child_source
                payload.setdefault("source_run_id", publish_child_source.run_id)
                payload.setdefault("publish_child_run_id", run.run_id)
                run = publish_child_source
            accepted_revision = _accepted_component_revision_for_publish(run, context)
            if accepted_revision is not None:
                remote_run = accepted_revision
                payload.setdefault("review_source_run_id", run.run_id)
                payload.setdefault("source_run_id", accepted_revision.run_id)
            existing_publish_run = _local_publish_child_for_run(run, context=context) or _local_publish_child_for_run(remote_run, context=context)
            if existing_publish_run is not None:
                existing_publish_run = _refresh_publish_child_remote_state(existing_publish_run, context=context)
                if not _publish_child_run_recoverable(existing_publish_run):
                    return Response(_serialize_run(existing_publish_run, context=context), status=status.HTTP_202_ACCEPTED)
            known_child_run_id = _publish_child_run_id_for_run(run) or _publish_child_run_id_for_run(remote_run)
            if known_child_run_id:
                recovered_publish_run = ContentFactoryRun.objects.filter(run_id=known_child_run_id).prefetch_related("steps").first()
                if recovered_publish_run is not None and _run_belongs_to_context(recovered_publish_run, context):
                    recovered_publish_run = _refresh_publish_child_remote_state(recovered_publish_run, context=context)
                    if not _publish_child_run_recoverable(recovered_publish_run):
                        return Response(_serialize_run(recovered_publish_run, context=context), status=status.HTTP_202_ACCEPTED)
            pending_runs = [
                candidate
                for candidate in (run, remote_run)
                if candidate is not None and _publish_handoff_pending_for_run(candidate)
            ]
            if pending_runs and any(not _publish_handoff_stale_for_run(candidate) for candidate in pending_runs):
                for candidate in pending_runs:
                    _annotate_publish_handoff_staleness(candidate)
                return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)
            _mark_publish_handoff_pending(run=run, remote_run=remote_run, action=action)
        remote_data = _call_content_factory_run_action(
            run_id=remote_run.run_id,
            action=action,
            payload=payload,
            workflow=remote_run.workflow,
            timeout=(3, 20) if action in {"promote-bundle", "publish-pr"} else (3, 15),
            transport_errors_are_pending=action in {"promote-bundle", "publish-pr"},
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
                publish_result = dict(publish_run.result or {})
                publish_result["source_run_id"] = remote_run.run_id
                publish_result["sourceRunId"] = remote_run.run_id
                if remote_run.run_id != run.run_id:
                    publish_result["review_source_run_id"] = run.run_id
                    publish_result["reviewSourceRunId"] = run.run_id
                publish_run.result = publish_result
                publish_run.save(update_fields=["result", "updated_at"])
                result = run.result or {}
                result["publish_child_run_id"] = publish_run.run_id
                result["latest_control_response"] = remote_data
                result["promote_bundle_requested_at"] = timezone.now().isoformat()
                result["publish_handoff_pending"] = False
                result["publish_child_status"] = publish_run.status
                result["publish_child_recoverable"] = _publish_child_run_recoverable(publish_run)
                result["publish_child_wait_reason"] = _publish_child_wait_reason(publish_run)
                result["publish_handoff_status"] = _publish_child_handoff_status(
                    publish_run,
                    recoverable=result["publish_child_recoverable"],
                )
                run.result = result
                run.save(update_fields=["result", "updated_at"])
                if remote_run.run_id != run.run_id:
                    remote_result = remote_run.result or {}
                    remote_result["publish_child_run_id"] = publish_run.run_id
                    remote_result["latest_control_response"] = remote_data
                    remote_result["promote_bundle_requested_at"] = result["promote_bundle_requested_at"]
                    remote_result["publish_handoff_pending"] = False
                    remote_result["publish_child_status"] = publish_run.status
                    remote_result["publish_child_recoverable"] = result["publish_child_recoverable"]
                    remote_result["publish_child_wait_reason"] = result["publish_child_wait_reason"]
                    remote_result["publish_handoff_status"] = result["publish_handoff_status"]
                    remote_run.result = remote_result
                    remote_run.save(update_fields=["result", "updated_at"])
                return Response(_serialize_run(publish_run, context=context), status=status.HTTP_202_ACCEPTED)
            if _content_factory_action_transport_pending(remote_data):
                _mark_publish_handoff_pending(run=run, remote_run=remote_run, action=action, remote_data=remote_data)
                return Response(_serialize_run(run, context=context), status=status.HTTP_202_ACCEPTED)

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
            if run.workflow == "article_system_setup":
                run.result = _clear_article_system_setup_retry_state(run.result or {})
                run.error = ""
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
            if action == "approve" and run.workflow in SCAN_WORKFLOWS:
                setup_run_id = str(remote_data.get("setup_run_id") or "").strip()
                if setup_run_id:
                    result["setup_run_id"] = setup_run_id
                    result["scaffold_job_id"] = remote_data.get("scaffold_job_id") or setup_run_id
                    result["scaffold_status"] = remote_data.get("scaffold_status") or "queued"
                    setup_payload = remote_data.get("article_system_setup")
                    setup_payload = dict(setup_payload) if isinstance(setup_payload, dict) else dict(result.get("article_system_setup") or {})
                    setup_payload.setdefault("setup_run_id", setup_run_id)
                    setup_payload.setdefault("parent_run_id", run.run_id)
                    setup_payload["status"] = setup_payload.get("status") or "queued"
                    setup_payload["requested_action"] = None
                    result["article_system_setup"] = setup_payload

                    nested_result = dict(result.get("result") or {})
                    nested_result["setup_run_id"] = setup_run_id
                    nested_result["scaffold_job_id"] = remote_data.get("scaffold_job_id") or setup_run_id
                    nested_result["scaffold_status"] = remote_data.get("scaffold_status") or "queued"
                    nested_result["article_system_setup"] = setup_payload
                    result["result"] = nested_result

                    if not ContentFactoryRun.objects.filter(run_id=setup_run_id).exists():
                        ContentFactoryRun.objects.create(
                            run_id=setup_run_id,
                            workflow="article_system_setup",
                            domain=run.domain,
                            github_repo=run.github_repo,
                            slack_user_id=run.slack_user_id,
                            status=ContentFactoryRunStatus.QUEUED,
                            current_step="queued",
                            approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
                            step_order=[
                                "load_context",
                                "validate_plan",
                                "prepare_branch",
                                "create_pull_request",
                                "start_hosted_preview",
                                "await_review",
                            ],
                            run_request={
                                "workflow": "article_system_setup",
                                "domain": run.domain,
                                "github_repo": run.github_repo,
                                "parent_run_id": run.run_id,
                                "scan_run_id": run.run_id,
                            },
                            result=setup_payload,
                            error="",
                        )
            if remote_data.get("status") and not _content_factory_action_transport_pending(remote_data):
                run.status = remote_data["status"]
            if remote_data.get("current_step"):
                run.current_step = remote_data["current_step"]
            run.result = result
        run.save(update_fields=["approval_state", "status", "current_step", "resume_available", "result", "error", "updated_at"])
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
