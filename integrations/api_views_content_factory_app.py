from __future__ import annotations

import logging
from datetime import date

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.article_system import (
    article_system_ready,
    best_registry_driven_publish_target,
    recommended_next_action as derive_article_system_next_action,
    registry_target_publish_ready,
    resolve_article_system,
)
from core.content_factory_auth import content_factory_github_connection_state
from core.models import (
    ContentFactoryJob,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    Organization,
    OrganizationContentConfig,
)
from content_factory.service_views import _serialize_content_factory_run, _sync_content_factory_run_snapshot
from integrations import http_client as http_requests
from integrations.content_factory_contract import CONTENT_FACTORY_REQUEST_SOURCE
from integrations.services.article_generation import (
    ArticleGenerationError,
    ContentFactoryBackendUnavailableError,
    InsufficientRooPointsError,
    confirm_topic,
    publish_article_as_pr,
    set_article_delivery_mode,
    trigger_article_generation,
)
from integrations.services.daily_discovery import (
    count_enabled_daily_discovery_configs,
    enqueue_scheduled_discovery,
    get_daily_discovery_max_targets,
)
from integrations.services.github_connections import build_github_oauth_url, get_owned_org_configs
from integrations.utils import normalize_domain

logger = logging.getLogger(__name__)


ACTIVE_RUN_STATUSES = {
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
    ContentFactoryRunStatus.BLOCKED,
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
    ContentFactoryRunStatus.AWAITING_APPROVAL,
    ContentFactoryRunStatus.APPROVAL_REQUIRED,
}


def _ensure_actor_id(user) -> str:
    existing = str(getattr(user, "slack_id", "") or "").strip()
    if existing:
        return existing

    actor_id = f"web_{user.pk}"
    user.slack_id = actor_id
    user.save(update_fields=["slack_id"])
    return actor_id


def _serialize_user(user, actor_id: str) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": user.full_name,
        "avatar_url": user.avatar_url,
        "is_superuser": user.is_superuser,
        "content_factory_actor_id": actor_id,
    }


def _serialize_config(config: OrganizationContentConfig | None) -> dict | None:
    if not config or not getattr(config, "organization", None):
        return None

    org = config.organization
    article_system = resolve_article_system(config)
    registry_target = best_registry_driven_publish_target(config.publish_targets, article_system)
    registry_ready = registry_target_publish_ready(registry_target)
    return {
        "org_id": org.id,
        "org_name": org.name,
        "domain": org.domain,
        "competitors": org.competitors or [],
        "seed_keywords": org.seed_keywords or [],
        "connected_slack_user_id": config.connected_slack_user_id,
        "default_timezone": config.default_timezone or "",
        "daily_discovery_enabled": config.daily_discovery_enabled,
        "daily_discovery_priority": config.daily_discovery_priority,
        "article_template": config.article_template,
        "design_guide": config.design_guide,
        "resource_prompt": config.resource_prompt,
        "company_context": config.company_context,
        "github_repo": config.github_repo,
        "article_delivery_mode": config.article_delivery_mode,
        "brand_name": config.brand_name,
        "scan_summary": config.scan_summary,
        "tech_stack": config.tech_stack or {},
        "installed_packages": config.installed_packages or {},
        "pillar_strategy": config.pillar_strategy or {},
        "build_healing_hints": config.build_healing_hints or [],
        "repo_execution_contract": config.repo_execution_contract or {},
        "article_path_pattern": config.article_path_pattern,
        "registry_path": config.registry_path,
        "publish_targets": config.publish_targets or [],
        "default_publish_target_id": config.default_publish_target_id,
        "article_system": article_system,
        "article_system_ready": article_system_ready(article_system) or registry_ready,
        "registry_driven_seo_ready": registry_ready,
        "articles_scaffolded": config.articles_scaffolded,
        "articles_scaffold_pr_url": config.articles_scaffold_pr_url,
        "articles_scaffold_preview_url": config.articles_scaffold_preview_url,
        "last_scanned_at": config.last_scanned_at.isoformat() if config.last_scanned_at else None,
        "last_scanned_sha": config.last_scanned_sha,
    }


def _serialize_github_status(config: OrganizationContentConfig | None, domain: str | None) -> dict:
    connection_state = content_factory_github_connection_state(config)
    return {
        "connected": connection_state == "connected",
        "domain": domain,
        "github_repo": getattr(config, "github_repo", None) if config else None,
        "github_user_name": getattr(config, "github_user_name", None) if config else None,
        "token_valid": connection_state == "connected",
        "connection_state": connection_state,
        "credential_source": "org" if config and getattr(config, "github_token_encrypted", None) else "none",
        "expires_at": (
            config.github_token_expires_at.isoformat()
            if config and config.github_token_expires_at
            else None
        ),
    }


def _serialize_run_summary(run: ContentFactoryRun) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "github_repo": run.github_repo,
        "status": run.status,
        "current_step": run.current_step,
        "approval_state": run.approval_state,
        "resume_available": run.resume_available,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _get_config_for_domain(domain: str | None) -> OrganizationContentConfig | None:
    normalized = normalize_domain(domain or "")
    if not normalized:
        return None
    org = Organization.objects.filter(domain=normalized).first()
    return getattr(org, "content_config", None) if org else None


def _assert_domain_access(actor_id: str, domain: str | None, *, allow_unowned: bool = False):
    normalized = normalize_domain(domain or "")
    if not normalized:
        return None, normalized

    config = _get_config_for_domain(normalized)
    owner = str(getattr(config, "connected_slack_user_id", "") or "").strip() if config else ""
    if owner and owner != actor_id:
        return Response(
            {"error": "You do not have access to this Content Factory domain."},
            status=status.HTTP_403_FORBIDDEN,
        ), normalized
    if config is None and not allow_unowned:
        return Response({"error": "Organization not found."}, status=status.HTTP_404_NOT_FOUND), normalized
    return None, normalized


def _content_factory_base_url() -> str:
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip()
    if base_url:
        return base_url.rstrip("/")
    if getattr(settings, "IS_LOCAL_ENV", False):
        return "http://localhost:8001"
    raise ArticleGenerationError("CONTENT_FACTORY_URL is not configured.")


def _content_factory_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = getattr(settings, "CONTENT_FACTORY_API_KEY", None)
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


def _content_factory_request(method: str, path: str, *, payload: dict | None = None):
    url = f"{_content_factory_base_url()}{path}"
    request = getattr(http_requests, method.lower())
    kwargs = {"headers": _content_factory_headers(), "timeout": (3, 30)}
    if payload is not None:
        kwargs["json"] = payload
    return request(url, **kwargs)


def _response_json(response) -> dict:
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {"error": response.text}
    return data if isinstance(data, dict) else {"data": data}


def _proxy_error(response):
    return Response(_response_json(response), status=response.status_code)


def _sync_remote_run_payload(run_id: str, payload: dict) -> ContentFactoryRun | None:
    if not isinstance(payload, dict) or not payload.get("workflow") or not payload.get("status"):
        return None

    step_states = payload.get("step_states") or payload.get("steps") or {}
    if not isinstance(step_states, dict):
        step_states = {}

    sync_payload = dict(payload)
    sync_payload["run_id"] = run_id
    run, _created = _sync_content_factory_run_snapshot(
        run_id=run_id,
        data=sync_payload,
        step_states=step_states,
    )
    return run


def _create_local_run_placeholder(*, run_id: str, workflow: str, domain: str, github_repo: str, actor_id: str, payload: dict):
    run, _created = ContentFactoryRun.objects.update_or_create(
        run_id=run_id,
        defaults={
            "workflow": workflow or payload.get("workflow") or "unknown",
            "domain": domain or payload.get("domain") or "",
            "github_repo": github_repo or payload.get("github_repo") or "",
            "slack_user_id": actor_id,
            "status": payload.get("status") or ContentFactoryRunStatus.QUEUED,
            "current_step": payload.get("current_step") or "",
            "run_request": payload.get("run_request") or {},
            "result": payload,
            "resume_available": bool(payload.get("resume_available")),
        },
    )
    return run


def _create_job_tracking(*, run_id: str, domain: str, actor_id: str, status_value: str, request_meta: dict):
    ContentFactoryJob.objects.update_or_create(
        job_id=run_id,
        defaults={
            "domain": domain,
            "slack_user_id": actor_id,
            "status": status_value or "queued",
            "request_meta": request_meta,
        },
    )


def _list_from_payload(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value).replace("\n", ",").split(",")
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _recommended_next_action(config: OrganizationContentConfig | None, github_status: dict) -> str:
    if not config:
        return "save_profile"
    if github_status.get("connection_state") != "connected" or not github_status.get("github_repo"):
        return "connect_github"

    article_system = resolve_article_system(config)
    scan_completed = bool(config.scan_summary or config.last_scanned_at)
    next_action = derive_article_system_next_action(scan_completed, article_system)
    if next_action == "research_article" and not (
        article_system_ready(article_system)
        or registry_target_publish_ready(best_registry_driven_publish_target(config.publish_targets, article_system))
    ):
        return "scaffold"
    return next_action


class ContentFactoryAppBootstrapView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        actor_id = _ensure_actor_id(request.user)
        requested_domain = normalize_domain(request.query_params.get("domain") or "")

        access_error, normalized_domain = _assert_domain_access(
            actor_id,
            requested_domain,
            allow_unowned=False,
        )
        if access_error and requested_domain:
            return access_error

        owned_configs = list(get_owned_org_configs(actor_id))
        active_config = _get_config_for_domain(normalized_domain) if normalized_domain else (
            owned_configs[0] if len(owned_configs) == 1 else None
        )
        active_domain = active_config.organization.domain if active_config else normalized_domain or None
        github_status = _serialize_github_status(active_config, active_domain)

        owned_domains = [_serialize_config(config) for config in owned_configs if getattr(config, "organization", None)]
        run_domain_filter = [config.organization.domain for config in owned_configs if getattr(config, "organization", None)]
        run_filter = Q(slack_user_id=actor_id)
        if run_domain_filter:
            run_filter |= Q(domain__in=run_domain_filter)
        recent_runs = [
            _serialize_run_summary(run)
            for run in ContentFactoryRun.objects.filter(run_filter).order_by("-updated_at")[:10]
        ]

        return Response(
            {
                "user": _serialize_user(request.user, actor_id),
                "actor_id": actor_id,
                "active_domain": active_domain,
                "domains": owned_domains,
                "org_config": _serialize_config(active_config),
                "github_status": github_status,
                "recent_runs": recent_runs,
                "recommended_next_action": _recommended_next_action(active_config, github_status),
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryAppSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        actor_id = _ensure_actor_id(request.user)
        data = request.data or {}
        domain = normalize_domain(data.get("domain") or "")
        if not domain:
            return Response({"error": "domain is required"}, status=status.HTTP_400_BAD_REQUEST)

        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=True)
        if access_error:
            return access_error

        competitors = _list_from_payload(data.get("competitors"))
        seed_keywords = _list_from_payload(data.get("seed_keywords"))
        daily_enabled = bool(data.get("daily_discovery_enabled"))
        article_delivery_mode = str(data.get("article_delivery_mode") or "review_draft").strip()
        if article_delivery_mode not in {"review_draft", "publish_code", "content_only", "publish_webflow"}:
            return Response({"error": "Invalid article_delivery_mode"}, status=status.HTTP_400_BAD_REQUEST)
        if daily_enabled and not (competitors or seed_keywords):
            return Response(
                {"error": "Add at least one competitor or seed keyword before enabling daily generation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_config = _get_config_for_domain(normalized_domain)
        if daily_enabled and not getattr(existing_config, "daily_discovery_enabled", False):
            enabled_count = count_enabled_daily_discovery_configs(
                exclude_config_id=getattr(existing_config, "id", None),
            )
            max_targets = get_daily_discovery_max_targets()
            if enabled_count >= max_targets:
                return Response(
                    {"error": f"No more than {max_targets} organizations may have daily generation enabled."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        priority = data.get("daily_discovery_priority", 0)
        try:
            priority = max(0, int(priority or 0))
        except (TypeError, ValueError):
            return Response({"error": "daily_discovery_priority must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            org, _created = Organization.objects.update_or_create(
                domain=normalized_domain,
                defaults={
                    "name": str(data.get("name") or data.get("brand_name") or normalized_domain).strip(),
                    "competitors": competitors,
                    "seed_keywords": seed_keywords,
                },
            )
            config, _created = OrganizationContentConfig.objects.get_or_create(organization=org)
            config.connected_slack_user_id = actor_id
            config.default_timezone = str(data.get("default_timezone") or config.default_timezone or "Australia/Melbourne")
            config.daily_discovery_enabled = daily_enabled
            config.daily_discovery_priority = priority
            config.article_delivery_mode = article_delivery_mode
            config.brand_name = str(data.get("brand_name") or "").strip() or config.brand_name
            config.company_context = str(data.get("company_context") or "").strip() or config.company_context
            github_repo = str(data.get("github_repo") or "").strip()
            if github_repo:
                config.github_repo = github_repo
            config.save()

        return Response(
            {
                "status": "updated",
                "org_config": _serialize_config(config),
                "github_status": _serialize_github_status(config, normalized_domain),
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryAppGitHubConnectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor_id = _ensure_actor_id(request.user)
        domain = normalize_domain((request.data or {}).get("domain") or "")
        if not domain:
            return Response({"error": "domain is required"}, status=status.HTTP_400_BAD_REQUEST)

        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=True)
        if access_error:
            return access_error

        org, _created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={"name": normalized_domain},
        )
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=org)
        if not config.connected_slack_user_id:
            config.connected_slack_user_id = actor_id
            config.save(update_fields=["connected_slack_user_id", "updated_at"])

        return_url = str((request.data or {}).get("return_url") or "").strip() or None
        auth_url = build_github_oauth_url(normalized_domain, actor_id, return_url=return_url)
        return Response(
            {
                "status": "auth_started",
                "auth_url": auth_url,
                "domain": normalized_domain,
                "github_repo": config.github_repo,
                "connection_state": content_factory_github_connection_state(config),
                "credential_source": "org" if config.github_token_encrypted else "none",
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryAppScanView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor_id = _ensure_actor_id(request.user)
        data = request.data or {}
        domain = normalize_domain(data.get("domain") or "")
        if not domain:
            return Response({"error": "domain is required"}, status=status.HTTP_400_BAD_REQUEST)

        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=False)
        if access_error:
            return access_error

        config = _get_config_for_domain(normalized_domain)
        github_repo = str(data.get("github_repo") or getattr(config, "github_repo", "") or "").strip()
        if not github_repo:
            return Response({"error": "github_repo is required"}, status=status.HTTP_400_BAD_REQUEST)

        existing_artifacts = {}
        if config:
            for source_field in (
                "article_template",
                "design_guide",
                "resource_prompt",
                "company_context",
                "tech_stack",
                "installed_packages",
                "pillar_strategy",
                "build_healing_hints",
                "repo_execution_contract",
                "article_path_pattern",
                "registry_path",
                "publish_targets",
                "default_publish_target_id",
                "article_system",
            ):
                value = getattr(config, source_field, None)
                if value:
                    existing_artifacts[source_field] = value

        payload = {
            "domain": normalized_domain,
            "github_repo": github_repo,
            "slack_user_id": actor_id,
            "requested_by_slack_user_id": actor_id,
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "existing_artifacts": existing_artifacts,
            "scaffold_if_missing": bool(data.get("scaffold_if_missing", True)),
            "generate_components": bool(data.get("generate_components", True)),
        }

        try:
            response = _content_factory_request("post", "/api/runs/scan", payload=payload)
        except http_requests.exceptions.RequestException as exc:
            logger.warning("Content Factory scan queue failed for %s: %s", normalized_domain, exc)
            return Response(
                {"error": "Content Factory is unavailable right now.", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ArticleGenerationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if response.status_code not in {200, 202}:
            return _proxy_error(response)

        result = _response_json(response)
        run_id = str(result.get("run_id") or result.get("job_id") or "").strip()
        if not run_id:
            return Response(
                {"error": "Content Factory did not return a run id."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        _create_job_tracking(
            run_id=run_id,
            domain=normalized_domain,
            actor_id=actor_id,
            status_value=result.get("status") or "queued",
            request_meta={**payload, "type": "scan"},
        )
        run = _create_local_run_placeholder(
            run_id=run_id,
            workflow=result.get("workflow") or "repo_scan",
            domain=normalized_domain,
            github_repo=github_repo,
            actor_id=actor_id,
            payload=result,
        )
        result["run"] = _serialize_run_summary(run)
        return Response(result, status=status.HTTP_202_ACCEPTED)


class ContentFactoryAppDiscoveryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor_id = _ensure_actor_id(request.user)
        data = request.data or {}
        domain = normalize_domain(data.get("domain") or "")
        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=False)
        if access_error:
            return access_error

        article_request = {
            "domain": normalized_domain,
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
            "requested_by_slack_user_id": actor_id,
            "client_request_id": data.get("client_request_id")
            or f"web-discovery:{actor_id}:{normalized_domain}:{timezone.now().date().isoformat()}:{timezone.now().timestamp()}",
            "user_email": request.user.email,
            "user_first_name": request.user.first_name,
            "user_last_name": request.user.last_name,
            "user_avatar_url": request.user.avatar_url,
        }
        try:
            result = trigger_article_generation(actor_id, article_request)
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ContentFactoryBackendUnavailableError as exc:
            return Response(exc.payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InsufficientRooPointsError as exc:
            return Response(exc.payload, status=status.HTTP_402_PAYMENT_REQUIRED)
        except ArticleGenerationError as exc:
            payload = getattr(exc, "payload", None)
            if isinstance(payload, dict):
                return Response(payload, status=status.HTTP_412_PRECONDITION_FAILED)
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)


class ContentFactoryAppArticleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor_id = _ensure_actor_id(request.user)
        data = request.data or {}
        domain = normalize_domain(data.get("domain") or "")
        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=False)
        if access_error:
            return access_error

        target_keyword = str(data.get("target_keyword") or data.get("confirmed_keyword") or "").strip()
        topic = str(data.get("topic") or data.get("custom_title") or target_keyword).strip()
        if not target_keyword:
            return Response({"error": "target_keyword is required"}, status=status.HTTP_400_BAD_REQUEST)

        source_run_id = str(data.get("source_run_id") or "").strip()
        delivery_mode = str(data.get("delivery_mode") or "review_draft").strip()
        try:
            if source_run_id:
                result = confirm_topic(
                    domain=normalized_domain,
                    confirmed_keyword=target_keyword,
                    slack_user_id=actor_id,
                    requested_by_slack_user_id=actor_id,
                    custom_title=str(data.get("custom_title") or topic or "").strip() or None,
                    skip_alternatives=data.get("skip_alternatives") or [],
                    source_run_id=source_run_id,
                    delivery_mode=delivery_mode,
                    delivery_mode_confirmed=True,
                    request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                )
            else:
                article_request = {
                    "domain": normalized_domain,
                    "topic": topic,
                    "target_keyword": target_keyword,
                    "context": data.get("context") or "",
                    "custom_title": data.get("custom_title") or "",
                    "delivery_mode": delivery_mode,
                    "delivery_mode_confirmed": True,
                    "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                    "requested_by_slack_user_id": actor_id,
                    "client_request_id": data.get("client_request_id")
                    or f"web-article:{actor_id}:{normalized_domain}:{target_keyword}:{timezone.now().timestamp()}",
                    "user_email": request.user.email,
                    "user_first_name": request.user.first_name,
                    "user_last_name": request.user.last_name,
                    "user_avatar_url": request.user.avatar_url,
                }
                result = trigger_article_generation(actor_id, article_request)
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ContentFactoryBackendUnavailableError as exc:
            return Response(exc.payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InsufficientRooPointsError as exc:
            return Response(exc.payload, status=status.HTTP_402_PAYMENT_REQUIRED)
        except ArticleGenerationError as exc:
            payload = getattr(exc, "payload", None)
            if isinstance(payload, dict):
                return Response(payload, status=status.HTTP_412_PRECONDITION_FAILED)
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)


class ContentFactoryAppRunView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id: str):
        actor_id = _ensure_actor_id(request.user)
        refresh = str(request.query_params.get("refresh") or "").lower() in {"1", "true", "yes"}
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()

        if run and not _run_belongs_to_actor(run, actor_id):
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        should_fetch_remote = refresh or run is None or (run.status in ACTIVE_RUN_STATUSES)
        if should_fetch_remote:
            try:
                response = _content_factory_request("get", f"/api/runs/{run_id}")
                if response.status_code == 200:
                    remote_payload = _response_json(response)
                    remote_run = _sync_remote_run_payload(run_id, remote_payload)
                    if remote_run and _run_belongs_to_actor(remote_run, actor_id):
                        run = remote_run
                elif response.status_code != 404 or run is None:
                    return _proxy_error(response)
            except (http_requests.exceptions.RequestException, ArticleGenerationError) as exc:
                if run is None:
                    return Response(
                        {"error": "Content Factory run is unavailable.", "detail": str(exc)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_content_factory_run(run), status=status.HTTP_200_OK)


def _run_belongs_to_actor(run: ContentFactoryRun, actor_id: str) -> bool:
    if run.slack_user_id == actor_id:
        return True
    config = _get_config_for_domain(run.domain)
    return bool(config and config.connected_slack_user_id == actor_id)


class ContentFactoryAppRunArtifactsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id: str):
        actor_id = _ensure_actor_id(request.user)
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run and not _run_belongs_to_actor(run, actor_id):
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            response = _content_factory_request("get", f"/api/runs/{run_id}/artifacts")
            if response.status_code == 200:
                return Response(_response_json(response), status=status.HTTP_200_OK)
            if response.status_code != 404 or run is None:
                return _proxy_error(response)
        except (http_requests.exceptions.RequestException, ArticleGenerationError):
            pass

        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)
        payload = _serialize_content_factory_run(run)
        return Response(
            {
                "run_id": payload["run_id"],
                "workflow": payload["workflow"],
                "artifact_root": payload["artifact_root"],
                "steps": payload["step_states"],
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryAppRunControlView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, run_id: str, action: str):
        actor_id = _ensure_actor_id(request.user)
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run and not _run_belongs_to_actor(run, actor_id):
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        if action == "delivery-mode":
            delivery_mode = str((request.data or {}).get("delivery_mode") or "").strip() or None
            try:
                return Response(set_article_delivery_mode(run_id, delivery_mode), status=status.HTTP_200_OK)
            except ArticleGenerationError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        if action in {"promote-bundle", "publish-pr"}:
            try:
                result = publish_article_as_pr(
                    run_id,
                    slack_user_id=actor_id,
                    requested_by_slack_user_id=actor_id,
                    domain=(run.domain if run else None),
                )
                return Response(result, status=status.HTTP_200_OK)
            except ArticleGenerationError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        if action not in {"approve", "deny", "resume"}:
            return Response({"error": "Unsupported action"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            response = _content_factory_request("post", f"/api/runs/{run_id}/{action}", payload={})
        except (http_requests.exceptions.RequestException, ArticleGenerationError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if response.status_code not in {200, 202}:
            return _proxy_error(response)

        payload = _response_json(response)
        remote_run = _sync_remote_run_payload(run_id, payload)
        if remote_run:
            return Response(_serialize_content_factory_run(remote_run), status=status.HTTP_200_OK)

        if run:
            if action == "deny":
                run.status = ContentFactoryRunStatus.DENIED
            elif action in {"approve", "resume"}:
                run.status = ContentFactoryRunStatus.RUNNING
            run.resume_available = action == "resume"
            run.save(update_fields=["status", "resume_available", "updated_at"])

        return Response(payload, status=status.HTTP_200_OK)


class ContentFactoryAppDailyReplayView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        actor_id = _ensure_actor_id(request.user)
        data = request.data or {}
        domain = normalize_domain(data.get("domain") or "")
        access_error, normalized_domain = _assert_domain_access(actor_id, domain, allow_unowned=False)
        if access_error:
            return access_error

        local_date = None
        if data.get("local_date"):
            parsed = parse_date(str(data.get("local_date")))
            if not isinstance(parsed, date):
                return Response({"error": "local_date must be YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
            local_date = parsed

        result = enqueue_scheduled_discovery(
            slack_user_id=actor_id,
            domain=normalized_domain,
            local_date=local_date,
            force=bool(data.get("force", True)),
        )
        http_status = status.HTTP_202_ACCEPTED if result.get("status") == "queued" else status.HTTP_200_OK
        return Response(result, status=http_status)
